# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""Application-level shared KV store over the fixed external shared-memory
API (spec §5-§6; docs/fixed-shared-memory-api.md).

The provider library, its allocator, locks and ABI are not ours. This module
only *calls* the documented operation families (init/connect/finalize,
put/get/destroy, shmalloc/shfree, lock alloc/acquire/release/free,
get_offset/get_ptr) and keeps our own metadata record next to each payload.

Offline it never loads a library: ``CtypesProviderApi`` binds a function only
when the operator has confirmed that function's C signature, and the
``EmulatedProviderApi`` used by tests is an in-process bytearray labelled
``is_emulated=True``. Emulated runs never count as a CXL gate (spec §9).

Not implemented here (reported as blockers, not worked around): crash
recovery / fencing of a dead writer's slot, the cross-host visibility
primitive (a CPU flush is not GPU-DMA coherence), and the offset origin.
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import struct
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .keys import JobId, LayoutDescriptor, LeaseId, PayloadRef, PrefixKey
from .kv_transfer_adapter import (
    CommitResult,
    CompletionProof,
    JobState,
    LeaseGrant,
    Miss,
    OffsetError,
    OffsetMapper,
    Reservation,
    ReserveResult,
    ReserveStatus,
    SharedKVStore,
    StoreCapacity,
    TransferEvent,
    TransferEventKind,
    TransferKind,
    TransferPriority,
)
from .offload_policy import CxlState

# --------------------------------------------------------------------------
# provider API surface (fixed; names from the integration note)
# --------------------------------------------------------------------------


class ProviderApi(ABC):
    """The subset of the fixed API we call. Pointers and lock handles are
    opaque ints/objects owned by the provider."""

    is_emulated: bool = False

    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def finalize(self) -> None: ...

    @abstractmethod
    def is_initialized(self) -> bool: ...

    @abstractmethod
    def shmalloc(self, nbytes: int) -> int: ...

    @abstractmethod
    def shfree(self, ptr: int) -> None: ...

    @abstractmethod
    def put(self, key: str, ptr: int) -> None: ...

    @abstractmethod
    def get(self, key: str) -> int | None: ...

    @abstractmethod
    def destroy(self, key: str) -> None: ...

    @abstractmethod
    def lock_alloc(self) -> Any: ...

    @abstractmethod
    def lock_free(self, handle: Any) -> None: ...

    @abstractmethod
    def lock_acquire(self, handle: Any) -> None: ...

    @abstractmethod
    def lock_release(self, handle: Any) -> None: ...

    @abstractmethod
    def get_offset(self, ptr: int) -> int: ...

    @abstractmethod
    def get_ptr(self, offset: int) -> int: ...

    @abstractmethod
    def read(self, ptr: int, nbytes: int) -> bytes: ...

    @abstractmethod
    def write(self, ptr: int, data: bytes | memoryview) -> None: ...


class EmulatedProviderApi(ProviderApi):
    """In-process emulation for tests. Pointer = base + offset so that a
    process-local pointer is never equal to a shared offset."""

    is_emulated = True
    BASE = 0x7F00_0000_0000

    def __init__(self, arena_bytes: int):
        self.arena = bytearray(arena_bytes)
        self._free: list[tuple[int, int]] = [(0, arena_bytes)]
        self._allocs: dict[int, int] = {}
        self._keys: dict[str, int] = {}
        self._locks: dict[int, threading.Lock] = {}
        self._next_lock = 1
        self._initialized = False
        self.calls: list[str] = []

    def connect(self) -> None:
        self._initialized = True

    def finalize(self) -> None:
        self._initialized = False

    def is_initialized(self) -> bool:
        return self._initialized

    def shmalloc(self, nbytes: int) -> int:
        nbytes = max(1, nbytes)
        for i, (off, size) in enumerate(self._free):
            if size >= nbytes:
                if size == nbytes:
                    del self._free[i]
                else:
                    self._free[i] = (off + nbytes, size - nbytes)
                self._allocs[off] = nbytes
                self.calls.append("shmalloc")
                return self.BASE + off
        raise MemoryError("emulated arena exhausted")

    def _release(self, ptr: int) -> None:
        off = ptr - self.BASE
        size = self._allocs.pop(off)
        self._free.append((off, size))
        self._free.sort()

    def shfree(self, ptr: int) -> None:
        self._release(ptr)
        self.calls.append("shfree")

    def put(self, key: str, ptr: int) -> None:
        if key in self._keys:
            raise KeyError(f"duplicate key {key}")
        self._keys[key] = ptr
        self.calls.append("put")

    def get(self, key: str) -> int | None:
        return self._keys.get(key)

    def destroy(self, key: str) -> None:
        # provider contract: destroy owns freeing the keyed object
        ptr = self._keys.pop(key)
        self._release(ptr)
        self.calls.append("destroy")

    def lock_alloc(self) -> int:
        h = self._next_lock
        self._next_lock += 1
        self._locks[h] = threading.Lock()
        return h

    def lock_free(self, handle: int) -> None:
        del self._locks[handle]

    def lock_acquire(self, handle: int) -> None:
        self._locks[handle].acquire()

    def lock_release(self, handle: int) -> None:
        self._locks[handle].release()

    def get_offset(self, ptr: int) -> int:
        return ptr - self.BASE

    def get_ptr(self, offset: int) -> int:
        return self.BASE + offset

    def read(self, ptr: int, nbytes: int) -> bytes:
        off = ptr - self.BASE
        return bytes(self.arena[off : off + nbytes])

    def write(self, ptr: int, data: bytes | memoryview) -> None:
        off = ptr - self.BASE
        self.arena[off : off + len(data)] = data


# --------------------------------------------------------------------------
# ctypes binding: signature proposals are inert until confirmed
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CSignature:
    name: str
    restype: str
    argtypes: tuple[str, ...]

    def digest(self) -> str:
        return hashlib.sha256(
            f"{self.name}({','.join(self.argtypes)})->{self.restype}".encode()
        ).hexdigest()[:16]


# Proposed signatures read from the provided interface note. They are NOT
# trusted until the operator confirms each one (AbiConfirmation), because
# the note says lock handles are passed by value and offsets are uint64 but
# does not pin every parameter type.
PROPOSED_SIGNATURES: dict[str, CSignature] = {
    "cxl_shm_connect": CSignature("cxl_shm_connect", "int", ()),
    "cxl_shm_finalize": CSignature("cxl_shm_finalize", "int", ()),
    "cxl_shm_is_initialized": CSignature("cxl_shm_is_initialized", "int", ()),
    "shmalloc": CSignature("shmalloc", "void*", ("size_t",)),
    "shfree": CSignature("shfree", "void", ("void*",)),
    "cxl_shm_put": CSignature("cxl_shm_put", "int", ("char*", "void*")),
    "cxl_shm_get": CSignature("cxl_shm_get", "int", ("char*", "void**")),
    "cxl_shm_destroy": CSignature("cxl_shm_destroy", "int", ("char*",)),
    "cxl_shm_get_offset": CSignature("cxl_shm_get_offset", "uint64", ("void*",)),
    "cxl_shm_get_ptr": CSignature("cxl_shm_get_ptr", "void*", ("uint64",)),
}

_CTYPES = {
    "int": ctypes.c_int,
    "void": None,
    "void*": ctypes.c_void_p,
    "void**": ctypes.POINTER(ctypes.c_void_p),
    "char*": ctypes.c_char_p,
    "size_t": ctypes.c_size_t,
    "uint64": ctypes.c_uint64,
}


@dataclass(frozen=True)
class AbiConfirmation:
    """Operator-supplied: function name -> confirmed signature digest."""

    confirmed: dict[str, str]
    library_sha256: str | None = None

    def covers(self, sig: CSignature) -> bool:
        return self.confirmed.get(sig.name) == sig.digest()


class AbiNotConfirmed(RuntimeError):
    pass


class CtypesProviderApi(ProviderApi):
    """Loads the provider library from ``CXL_SHM_LIBRARY`` only, binds only
    confirmed signatures, and never initialises the manager (connect only).
    Lock functions are intentionally unbound until their handle ABI is
    confirmed; ``lock_*`` raise AbiNotConfirmed."""

    is_emulated = False

    def __init__(self, confirmation: AbiConfirmation, library_path: str | None = None):
        path = library_path or os.environ.get("CXL_SHM_LIBRARY")
        if not path:
            raise AbiNotConfirmed("CXL_SHM_LIBRARY not set; offline mode loads nothing")
        if confirmation.library_sha256 is not None:
            if not os.path.isfile(path):
                raise AbiNotConfirmed(f"{path} is not a file; cannot verify build hash")
            with open(path, "rb") as fh:
                actual = hashlib.sha256(fh.read()).hexdigest()
            if actual != confirmation.library_sha256:
                raise AbiNotConfirmed("library hash differs from the confirmed build")
        self.confirmation = confirmation
        self.lib = ctypes.CDLL(path)
        self._bound: dict[str, Any] = {}

    def _fn(self, name: str) -> Any:
        if name in self._bound:
            return self._bound[name]
        sig = PROPOSED_SIGNATURES[name]
        if not self.confirmation.covers(sig):
            raise AbiNotConfirmed(f"{name} signature not confirmed ({sig.digest()})")
        fn = getattr(self.lib, name)
        fn.restype = _CTYPES[sig.restype]
        fn.argtypes = [_CTYPES[a] for a in sig.argtypes]
        self._bound[name] = fn
        return fn

    def connect(self) -> None:
        if self._fn("cxl_shm_connect")() != 0:
            raise RuntimeError("cxl_shm_connect failed")

    def finalize(self) -> None:
        self._fn("cxl_shm_finalize")()

    def is_initialized(self) -> bool:
        return bool(self._fn("cxl_shm_is_initialized")())

    def shmalloc(self, nbytes: int) -> int:
        ptr = self._fn("shmalloc")(nbytes)
        if not ptr:
            raise MemoryError("shmalloc returned NULL")
        return int(ptr)

    def shfree(self, ptr: int) -> None:
        self._fn("shfree")(ctypes.c_void_p(ptr))

    def put(self, key: str, ptr: int) -> None:
        if self._fn("cxl_shm_put")(key.encode(), ctypes.c_void_p(ptr)) != 0:
            raise RuntimeError(f"cxl_shm_put({key}) failed")

    def get(self, key: str) -> int | None:
        out = ctypes.c_void_p()
        rc = self._fn("cxl_shm_get")(key.encode(), ctypes.byref(out))
        if rc != 0 or not out.value:
            return None
        return int(out.value)

    def destroy(self, key: str) -> None:
        if self._fn("cxl_shm_destroy")(key.encode()) != 0:
            raise RuntimeError(f"cxl_shm_destroy({key}) failed")

    def lock_alloc(self) -> Any:
        raise AbiNotConfirmed("lock handle ABI not confirmed")

    def lock_free(self, handle: Any) -> None:
        raise AbiNotConfirmed("lock handle ABI not confirmed")

    def lock_acquire(self, handle: Any) -> None:
        raise AbiNotConfirmed("lock handle ABI not confirmed")

    def lock_release(self, handle: Any) -> None:
        raise AbiNotConfirmed("lock handle ABI not confirmed")

    def get_offset(self, ptr: int) -> int:
        return int(self._fn("cxl_shm_get_offset")(ctypes.c_void_p(ptr)))

    def get_ptr(self, offset: int) -> int:
        return int(self._fn("cxl_shm_get_ptr")(offset))

    def read(self, ptr: int, nbytes: int) -> bytes:
        return ctypes.string_at(ptr, nbytes)

    def write(self, ptr: int, data: bytes | memoryview) -> None:
        buf = (ctypes.c_char * len(data)).from_buffer_copy(bytes(data))
        ctypes.memmove(ptr, buf, len(data))


# --------------------------------------------------------------------------
# our metadata record (application level; not provider metadata)
# --------------------------------------------------------------------------

MAGIC = b"VAMPKV01"
# magic, version, state, generation, payload_offset, payload_len, rounded_len,
# reader_count, checksum, model digest, prefix digest, writer id
_ENTRY = struct.Struct("!8sIIQQQQQ64s64s64s32s")
_DIR = struct.Struct("!8sQQ32s")  # magic, next_generation, entries, allocator_id


class EntryState(int, Enum):
    ABSENT = 0
    RESERVED = 1
    WRITING = 2
    READY = 3
    EVICTING = 4
    ABORTING = 5

    def to_cxl(self) -> CxlState:
        return CxlState[self.name]


@dataclass
class EntryRecord:
    state: EntryState
    generation: int
    payload_offset: int
    payload_len: int
    rounded_len: int
    reader_count: int
    checksum: str
    model_digest: str
    prefix_digest: str
    writer_id: str

    def pack(self) -> bytes:
        return _ENTRY.pack(
            MAGIC,
            1,
            int(self.state),
            self.generation,
            self.payload_offset,
            self.payload_len,
            self.rounded_len,
            self.reader_count,
            self.checksum.encode()[:64],
            self.model_digest.encode()[:64],
            self.prefix_digest.encode()[:64],
            self.writer_id.encode()[:32],
        )

    @classmethod
    def unpack(cls, raw: bytes) -> EntryRecord:
        magic, version, state, gen, poff, plen, rlen, readers, chk, mdl, pfx, wid = (
            _ENTRY.unpack(raw)
        )
        if magic != MAGIC or version != 1:
            raise ValueError("not a VAMP entry record")
        return cls(
            EntryState(state),
            gen,
            poff,
            plen,
            rlen,
            readers,
            chk.rstrip(b"\0").decode(),
            mdl.rstrip(b"\0").decode(),
            pfx.rstrip(b"\0").decode(),
            wid.rstrip(b"\0").decode(),
        )


def prefix_digest(prefix: PrefixKey) -> str:
    return hashlib.sha256(
        (
            "|".join(prefix.hash_chain) + f"|{prefix.complete_tokens}|{prefix.salt}"
        ).encode()
    ).hexdigest()


def model_digest(prefix: PrefixKey) -> str:
    m = prefix.model
    return hashlib.sha256(
        f"{m.model_revision}|{m.tokenizer_revision}|{m.kv_dtype}|{m.layout_version}|{m.tp}|{m.pp}".encode()
    ).hexdigest()


# --------------------------------------------------------------------------
# store
# --------------------------------------------------------------------------


@dataclass
class _Entry:
    key: str
    header_ptr: int
    payload_ptr: int
    lock: Any
    reservation_id: str | None = None
    local_leases: dict[LeaseId, str] = field(default_factory=dict)


class CxlSharedKVStore(SharedKVStore):
    """SharedKVStore whose state lives in provider shared memory.

    Single-writer and reader-lease rules are enforced under the per-entry
    provider lock. Lock ordering: directory lock, then entry lock; never the
    reverse. ``namespace`` isolates runs (RunKey) inside one pool.
    """

    def __init__(
        self,
        api: ProviderApi,
        namespace: str,
        capacity: StoreCapacity,
        mapper: OffsetMapper,
        layout: LayoutDescriptor,
        writer_id: str,
    ):
        self.api = api
        self.namespace = namespace
        self.capacity = capacity
        self.mapper = mapper
        self.layout = layout
        self.writer_id = writer_id
        self.allocator_id = f"cxl:{namespace}"
        self._entries: dict[PrefixKey, _Entry] = {}
        self._leases: dict[LeaseId, PrefixKey] = {}
        self._lease_seq = 0
        self._res_seq = 0
        self._occupied = 0
        if not api.is_initialized():
            api.connect()
        self._dir_lock = api.lock_alloc()
        self._dir_key = f"{namespace}:vamp:directory"
        ptr = api.get(self._dir_key)
        if ptr is None:
            ptr = api.shmalloc(_DIR.size)
            api.write(ptr, _DIR.pack(MAGIC, 1, 0, self.allocator_id.encode()[:32]))
            api.put(self._dir_key, ptr)
        self._dir_ptr = ptr

    # -- helpers ------------------------------------------------------------

    def _key(self, prefix: PrefixKey) -> str:
        ident = hashlib.sha256(
            (model_digest(prefix) + prefix_digest(prefix)).encode()
        ).hexdigest()
        return f"{self.namespace}:kv:{ident[:32]}"

    def _read_entry(self, entry: _Entry) -> EntryRecord:
        return EntryRecord.unpack(self.api.read(entry.header_ptr, _ENTRY.size))

    def _write_entry(self, entry: _Entry, rec: EntryRecord) -> None:
        self.api.write(entry.header_ptr, rec.pack())

    def _next_generation(self) -> int:
        self.api.lock_acquire(self._dir_lock)
        try:
            magic, nxt, count, alloc = _DIR.unpack(
                self.api.read(self._dir_ptr, _DIR.size)
            )
            self.api.write(self._dir_ptr, _DIR.pack(magic, nxt + 1, count + 1, alloc))
            return nxt + 1
        finally:
            self.api.lock_release(self._dir_lock)

    def _lookup(self, prefix: PrefixKey) -> _Entry | None:
        entry = self._entries.get(prefix)
        if entry is not None:
            return entry
        key = self._key(prefix)
        ptr = self.api.get(key)
        if ptr is None:
            return None
        # entry created by another writer in this pool: adopt read-only view
        rec = EntryRecord.unpack(self.api.read(ptr, _ENTRY.size))
        entry = _Entry(key, ptr, self.api.get_ptr(rec.payload_offset), lock=None)
        self._entries[prefix] = entry
        return entry

    # -- SharedKVStore -------------------------------------------------------

    def reserve(self, prefix: PrefixKey, nbytes: int, writer_id: str) -> ReserveResult:
        existing = self._lookup(prefix)
        if existing is not None:
            rec = self._read_entry(existing)
            if rec.state == EntryState.READY:
                return ReserveResult(
                    ReserveStatus.ALREADY_READY, existing_generation=rec.generation
                )
            if rec.state in (EntryState.RESERVED, EntryState.WRITING):
                return ReserveResult(
                    ReserveStatus.ALREADY_WRITING, existing_generation=rec.generation
                )
            return ReserveResult(ReserveStatus.BUSY, existing_generation=rec.generation)
        rounded = self.capacity.round_up(nbytes)
        if self._occupied + rounded > self.capacity.payload_capacity_bytes:
            return ReserveResult(ReserveStatus.REJECTED_CAPACITY)
        try:
            payload_ptr = self.api.shmalloc(rounded)
        except MemoryError:
            return ReserveResult(ReserveStatus.REJECTED_CAPACITY)
        rel = self.api.get_offset(payload_ptr)
        try:
            self.mapper.check_range(rel, rounded)
        except OffsetError:
            self.api.shfree(payload_ptr)
            return ReserveResult(ReserveStatus.REJECTED_BOUNDS)
        generation = self._next_generation()
        header_ptr = self.api.shmalloc(_ENTRY.size)
        lock = self.api.lock_alloc()
        self._res_seq += 1
        reservation_id = f"{self.writer_id}-res-{self._res_seq}"
        rec = EntryRecord(
            EntryState.RESERVED,
            generation,
            rel,
            nbytes,
            rounded,
            0,
            "",
            model_digest(prefix),
            prefix_digest(prefix),
            writer_id,
        )
        self.api.write(header_ptr, rec.pack())
        self.api.put(self._key(prefix), header_ptr)
        self._entries[prefix] = _Entry(
            self._key(prefix), header_ptr, payload_ptr, lock, reservation_id
        )
        self._occupied += rounded
        return ReserveResult(
            ReserveStatus.RESERVED,
            reservation=Reservation(
                reservation_id,
                prefix,
                self.allocator_id,
                generation,
                rel,
                nbytes,
                rounded,
                writer_id,
            ),
        )

    def _own(self, reservation: Reservation) -> _Entry | None:
        entry = self._entries.get(reservation.prefix)
        if entry is None or entry.reservation_id != reservation.reservation_id:
            return None
        if self._read_entry(entry).generation != reservation.generation:
            return None
        return entry

    def mark_writing(self, reservation: Reservation) -> None:
        entry = self._own(reservation)
        if entry is None:
            return
        self.api.lock_acquire(entry.lock)
        try:
            rec = self._read_entry(entry)
            if rec.state == EntryState.RESERVED:
                rec.state = EntryState.WRITING
                self._write_entry(entry, rec)
        finally:
            self.api.lock_release(entry.lock)

    def payload_ptr(self, reservation: Reservation) -> int:
        entry = self._own(reservation)
        if entry is None:
            raise KeyError("stale reservation")
        return entry.payload_ptr

    def commit_ready(
        self, reservation: Reservation, proof: CompletionProof
    ) -> CommitResult:
        entry = self._own(reservation)
        if entry is None:
            return CommitResult(False, "stale reservation (generation/id mismatch)")
        if (
            proof.allocator_id != self.allocator_id
            or proof.generation != reservation.generation
            or proof.reservation_id != reservation.reservation_id
        ):
            return CommitResult(
                False, "completion proof does not match slot generation"
            )
        self.api.lock_acquire(entry.lock)
        try:
            rec = self._read_entry(entry)
            if rec.state != EntryState.WRITING:
                return CommitResult(False, f"slot not WRITING ({rec.state.name})")
            if proof.nbytes != rec.payload_len:
                return CommitResult(False, "byte count mismatch")
            # visibility: the provider's fence primitive is not exposed; the
            # caller must have observed the transport's VISIBLE event. We do
            # not add a guessed flush here (fixed-shared-memory-api.md).
            rec.state = EntryState.READY
            rec.checksum = proof.checksum
            self._write_entry(entry, rec)
            return CommitResult(True, "READY")
        finally:
            self.api.lock_release(entry.lock)

    def acquire_ready(
        self, prefix: PrefixKey, reader_id: str, expected_generation: int | None = None
    ) -> LeaseGrant | Miss:
        entry = self._lookup(prefix)
        if entry is None:
            return Miss(CxlState.ABSENT.value)
        if entry.lock is None:
            return Miss("FOREIGN_ENTRY_LOCK_UNAVAILABLE")
        self.api.lock_acquire(entry.lock)
        try:
            rec = self._read_entry(entry)
            if rec.state != EntryState.READY:
                return Miss(rec.state.name)
            if (
                expected_generation is not None
                and expected_generation != rec.generation
            ):
                return Miss("GENERATION_MISMATCH")
            if rec.model_digest != model_digest(
                prefix
            ) or rec.prefix_digest != prefix_digest(prefix):
                return Miss("PREFIX_OR_MODEL_MISMATCH")
            rec.reader_count += 1
            self._write_entry(entry, rec)
            self._lease_seq += 1
            lease = LeaseId(f"{self.writer_id}-reader-lease-{self._lease_seq}")
            entry.local_leases[lease] = reader_id
            self._leases[lease] = prefix
            return LeaseGrant(
                lease,
                PayloadRef(
                    prefix,
                    self.allocator_id,
                    rec.generation,
                    rec.payload_offset,
                    rec.payload_len,
                    self.layout,
                ),
                rec.checksum,
            )
        finally:
            self.api.lock_release(entry.lock)

    def release(self, lease_id: LeaseId) -> None:
        prefix = self._leases.pop(lease_id)
        entry = self._entries[prefix]
        self.api.lock_acquire(entry.lock)
        try:
            del entry.local_leases[lease_id]
            rec = self._read_entry(entry)
            rec.reader_count -= 1
            self._write_entry(entry, rec)
            free_now = rec.state == EntryState.EVICTING and rec.reader_count == 0
        finally:
            self.api.lock_release(entry.lock)
        if free_now:
            self._free_entry(entry, prefix)

    def abort(self, reservation: Reservation) -> None:
        entry = self._own(reservation)
        if entry is None:
            return
        self.api.lock_acquire(entry.lock)
        try:
            rec = self._read_entry(entry)
            if rec.state in (EntryState.RESERVED, EntryState.WRITING):
                rec.state = EntryState.ABORTING
                self._write_entry(entry, rec)
        finally:
            self.api.lock_release(entry.lock)

    def settle(self, reservation: Reservation, terminal: TransferEvent) -> None:
        if terminal.kind not in (
            TransferEventKind.COMPLETED,
            TransferEventKind.FAILED,
            TransferEventKind.CANCELLED,
        ):
            raise ValueError("settle requires a terminal transport event")
        entry = self._own(reservation)
        if entry is None:
            return
        if self._read_entry(entry).state == EntryState.ABORTING:
            self._free_entry(entry, reservation.prefix)

    def request_evict(self, prefix: PrefixKey) -> str:
        entry = self._entries.get(prefix)
        if entry is None:
            return "ABSENT"
        self.api.lock_acquire(entry.lock)
        try:
            rec = self._read_entry(entry)
            if rec.state in (
                EntryState.RESERVED,
                EntryState.WRITING,
                EntryState.ABORTING,
            ):
                return "REFUSED_WRITER_PROTECTED"
            if rec.reader_count > 0:
                rec.state = EntryState.EVICTING
                self._write_entry(entry, rec)
                return "DEFERRED_READERS_ACTIVE"
        finally:
            self.api.lock_release(entry.lock)
        self._free_entry(entry, prefix)
        return "EVICTED"

    def _free_entry(self, entry: _Entry, prefix: PrefixKey) -> None:
        rec = self._read_entry(entry)
        # provider contract: free object-associated locks first, then destroy
        # the keyed object (destroy owns its memory: no shfree of the header).
        # The payload buffer is a separate shmalloc and is ours to free.
        self.api.lock_free(entry.lock)
        self.api.destroy(entry.key)
        self.api.shfree(entry.payload_ptr)
        self._occupied -= rec.rounded_len
        del self._entries[prefix]

    def state_of(self, prefix: PrefixKey) -> tuple[CxlState, int | None]:
        entry = self._lookup(prefix)
        if entry is None:
            return CxlState.ABSENT, None
        rec = self._read_entry(entry)
        return rec.state.to_cxl(), rec.generation

    def occupied_bytes(self) -> int:
        return self._occupied


# --------------------------------------------------------------------------
# chunked copy into/out of provider memory (same event surface as the fakes)
# --------------------------------------------------------------------------


@dataclass
class CxlCopyJob:
    job_id: JobId
    source: memoryview | None  # write: bytes to copy into the slot
    dest_ptr: int | None  # write target (provider pointer)
    read_ptr: int | None  # read source (provider pointer)
    sink: bytearray | memoryview | None  # read target
    nbytes: int
    chunk_bytes: int
    generation: int
    reservation_id: str | None
    allocator_id: str
    checksum: str
    kind: TransferKind
    priority: TransferPriority
    source_name: str = "local"
    submitted_ns: int = 0
    started_ns: int | None = None
    finished_ns: int | None = None
    state: JobState = JobState.QUEUED
    chunks_done: int = 0
    cancel_requested: bool = False
    fail_at_chunk: int | None = None
    terminal_emitted: bool = False
    visible_emitted: bool = False
    thread: threading.Thread | None = field(default=None, repr=False)

    @property
    def chunks_total(self) -> int:
        return max(1, -(-self.nbytes // self.chunk_bytes))

    def is_terminal(self) -> bool:
        return self.state in (JobState.DONE, JobState.FAILED, JobState.CANCELLED)


class CxlCopyTransport:
    """Chunked memcpy through the provider read/write surface. No visibility
    primitive is exposed by the fixed API, so VISIBLE is emitted only when a
    ``fence`` callable is supplied by the operator; otherwise READY can never
    be committed through this transport (fail closed)."""

    def __init__(
        self,
        api: ProviderApi,
        fence: Callable[[int, int], None] | None = None,
        clock_ns: Callable[[], int] = time.monotonic_ns,
    ):
        self.api = api
        self.fence = fence
        self.clock_ns = clock_ns
        self.jobs: dict[JobId, CxlCopyJob] = {}
        self._events: list[TransferEvent] = []
        self._lock = threading.Lock()

    def submit(self, job: CxlCopyJob) -> JobId:
        if job.job_id in self.jobs:
            raise ValueError("duplicate job")
        job.submitted_ns = self.clock_ns()
        self.jobs[job.job_id] = job
        return job.job_id

    def start(self, job_id: JobId) -> None:
        job = self.jobs[job_id]
        job.state = JobState.RUNNING
        job.started_ns = self.clock_ns()
        self._emit(TransferEvent(TransferEventKind.STARTED, job_id, job.started_ns))
        job.thread = threading.Thread(target=self._run, args=(job,), daemon=True)
        job.thread.start()

    def cancel(self, job_id: JobId) -> None:
        job = self.jobs[job_id]
        if job.is_terminal():
            return
        job.cancel_requested = True
        if job.state == JobState.QUEUED:
            self._terminate(job, JobState.CANCELLED, "cancelled before start")

    def inject_failure(self, job_id: JobId, at_chunk: int) -> None:
        self.jobs[job_id].fail_at_chunk = at_chunk

    def _emit(self, ev: TransferEvent) -> None:
        with self._lock:
            self._events.append(ev)

    def _terminate(self, job: CxlCopyJob, state: JobState, error: str | None) -> None:
        assert not job.terminal_emitted
        job.terminal_emitted = True
        job.state = state
        job.finished_ns = self.clock_ns()
        kind = {
            JobState.DONE: TransferEventKind.COMPLETED,
            JobState.FAILED: TransferEventKind.FAILED,
            JobState.CANCELLED: TransferEventKind.CANCELLED,
        }[state]
        proof = None
        if state == JobState.DONE:
            proof = CompletionProof(
                job.job_id,
                job.allocator_id,
                job.generation,
                job.reservation_id,
                job.checksum,
                job.nbytes,
            )
        self._emit(
            TransferEvent(
                kind,
                job.job_id,
                job.finished_ns,
                proof=proof,
                error=error,
                bytes_done=min(job.nbytes, job.chunks_done * job.chunk_bytes),
            )
        )
        if (
            state == JobState.DONE
            and self.fence is not None
            and job.dest_ptr is not None
        ):
            self.fence(job.dest_ptr, job.nbytes)
            job.visible_emitted = True
            self._emit(
                TransferEvent(
                    TransferEventKind.VISIBLE, job.job_id, self.clock_ns(), proof=proof
                )
            )

    def _run(self, job: CxlCopyJob) -> None:
        try:
            for idx in range(job.chunks_total):
                if job.cancel_requested:
                    self._terminate(
                        job, JobState.CANCELLED, "cancelled at chunk boundary"
                    )
                    return
                if job.fail_at_chunk is not None and idx + 1 >= job.fail_at_chunk:
                    self._terminate(job, JobState.FAILED, "injected copy error")
                    return
                start = idx * job.chunk_bytes
                length = min(job.chunk_bytes, job.nbytes - start)
                if job.dest_ptr is not None and job.source is not None:
                    self.api.write(
                        job.dest_ptr + start, job.source[start : start + length]
                    )
                elif job.read_ptr is not None and job.sink is not None:
                    memoryview(job.sink)[start : start + length] = self.api.read(
                        job.read_ptr + start, length
                    )
                job.chunks_done = idx + 1
            self._terminate(job, JobState.DONE, None)
        except Exception as exc:  # noqa: BLE001 - reported as a terminal event
            if not job.terminal_emitted:
                self._terminate(job, JobState.FAILED, f"copy error: {exc}")

    def poll(self) -> list[TransferEvent]:
        with self._lock:
            out, self._events = self._events, []
        return out

    def active_jobs(self) -> int:
        return sum(1 for j in self.jobs.values() if not j.is_terminal())

    def wait(self, job_id: JobId, timeout_s: float = 30.0) -> None:
        job = self.jobs[job_id]
        if job.thread is not None:
            job.thread.join(timeout=timeout_s)
