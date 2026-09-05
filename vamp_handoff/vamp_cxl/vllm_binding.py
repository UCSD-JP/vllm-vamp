# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""Binding of the VAMP adapter contracts to the pinned vLLM v0.19.0 offload
hooks (spec §12 step 3; capability-audit.md "minimum backend changes").

No vLLM source file is modified. Everything here plugs in through the
existing ``spec_module_path`` / ``spec_name`` extension point::

    --kv-transfer-config '{"kv_connector": "OffloadingConnector",
                           "kv_role": "kv_both",
                           "kv_connector_extra_config": {
                               "spec_module_path": "vamp_cxl.vllm_binding",
                               "spec_name": "VampOffloadingSpec",
                               "cpu_bytes_to_use": 34359738368}}'

What it adds, and where it runs:

* ``VampCPUOffloadingManager`` (scheduler process): CPU READY / eviction
  notifications, an export-lease API, and an import reservation API. All
  three reuse the manager's own ref-count discipline (``prepare_load`` /
  ``complete_load`` / ``prepare_store`` / ``complete_store``); the LRU/ARC
  policies never evict a block with ``ref_cnt != 0``, so a lease is a pin.
  Other threads never touch the manager directly: they post commands to a
  mailbox that is drained on the scheduler thread inside ``lookup`` and
  ``take_events`` (the scheduler calls ``take_events`` every step).

* ``CpuPayloadBridge`` (worker process): zero-copy views of the CPU KV
  tensors owned by ``CpuGpuOffloadingHandlers`` for export, and in-place
  import into reserved CPU block IDs. It never allocates KV memory.

* ``VampOffloadingSpec``: creates the manager and, on the worker side, the
  base CUDA<->CPU handlers plus the bridge. Instances register themselves so
  an in-process agent can find them (``current_manager()`` /
  ``current_bridge()``).

Scheduler-side classes are unit-tested CPU-only against the real
``CPUOffloadingManager``. The worker-side handlers need CUDA and are not
exercised here; the bridge is tested on CPU tensors of the same layout.
"""

from __future__ import annotations

import hashlib
import queue
import threading
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any, Protocol

from vllm.v1.core.kv_cache_utils import BlockHash
from vllm.v1.kv_offload.abstract import (
    LoadStoreSpec,
    OffloadingEvent,
    OffloadingManager,
    PrepareStoreOutput,
)
from vllm.v1.kv_offload.cpu.manager import CPUOffloadingManager
from vllm.v1.kv_offload.cpu.spec import CPUOffloadingSpec
from vllm.v1.kv_offload.mediums import CPULoadStoreSpec
from vllm.v1.kv_offload.spec import CanonicalKVCaches
from vllm.v1.kv_offload.worker.worker import OffloadingHandler

# --------------------------------------------------------------------------
# scheduler side
# --------------------------------------------------------------------------


class ReadyListener(Protocol):
    def on_blocks_ready(self, block_hashes: list[BlockHash]) -> None: ...

    def on_blocks_evicted(self, block_hashes: list[BlockHash]) -> None: ...


@dataclass(frozen=True)
class ExportLease:
    """Pinned READY CPU blocks. ``block_ids`` are local slot indices for the
    worker-side bridge; they are never a cross-host identifier."""

    lease_id: int
    block_hashes: tuple[BlockHash, ...]
    block_ids: tuple[int, ...]


@dataclass(frozen=True)
class ImportReservation:
    """CPU slots allocated for an incoming payload; not READY until
    ``commit_import``."""

    reservation_id: int
    block_hashes: tuple[BlockHash, ...]
    block_ids: tuple[int, ...]
    evicted: tuple[BlockHash, ...]


class VampCPUOffloadingManager(CPUOffloadingManager):
    def __init__(
        self,
        block_size: int,
        num_blocks: int,
        cache_policy: str = "lru",
        enable_events: bool = False,
        listener: ReadyListener | None = None,
    ):
        super().__init__(
            block_size=block_size,
            num_blocks=num_blocks,
            cache_policy=cache_policy,  # type: ignore[arg-type]
            enable_events=enable_events,
        )
        self.listener = listener
        self._owner_thread: int | None = None
        self._mailbox: queue.Queue[tuple[Callable[[], Any], Future]] = queue.Queue()
        self._next_id = 0
        self._leases: dict[int, ExportLease] = {}
        self._imports: dict[int, ImportReservation] = {}
        self.counters: dict[str, int] = {}

    # -- ownership -----------------------------------------------------------

    def _count(self, key: str) -> None:
        self.counters[key] = self.counters.get(key, 0) + 1

    def _on_owner_thread(self) -> None:
        ident = threading.get_ident()
        if self._owner_thread is None:
            self._owner_thread = ident
        elif self._owner_thread != ident:
            raise RuntimeError(
                "VampCPUOffloadingManager called from a non-owner thread; "
                "use the *_async mailbox API"
            )

    def _drain_mailbox(self) -> None:
        while True:
            try:
                fn, fut = self._mailbox.get_nowait()
            except queue.Empty:
                return
            # A caller that gave up (fut.cancel()) must not have its command
            # executed: running it would leak a lease and set_result would
            # raise InvalidStateError into the scheduler step.
            if not fut.set_running_or_notify_cancel():
                self._count("mailbox_cancelled")
                continue
            try:
                fut.set_result(fn())
            except Exception as exc:  # noqa: BLE001 - delivered to the caller
                fut.set_exception(exc)

    def _post(self, fn: Callable[[], Any]) -> Future:
        fut: Future = Future()
        self._mailbox.put((fn, fut))
        return fut

    def bind_owner_thread(self) -> None:
        """Optional: pin ownership to the current (scheduler) thread."""
        self._owner_thread = threading.get_ident()

    # -- OffloadingManager overrides (scheduler thread) ----------------------

    def lookup(self, block_hashes: Iterable[BlockHash]) -> int | None:
        self._on_owner_thread()
        self._drain_mailbox()
        return super().lookup(block_hashes)

    def take_events(self) -> Iterable[OffloadingEvent]:
        self._on_owner_thread()
        self._drain_mailbox()
        return list(super().take_events())

    def prepare_store(
        self, block_hashes: Iterable[BlockHash]
    ) -> PrepareStoreOutput | None:
        self._on_owner_thread()
        out = super().prepare_store(block_hashes)
        if out is not None and out.block_hashes_evicted and self.listener is not None:
            self.listener.on_blocks_evicted(list(out.block_hashes_evicted))
        self._drain_mailbox()
        return out

    def complete_store(
        self, block_hashes: Iterable[BlockHash], success: bool = True
    ) -> None:
        """Marks blocks READY and notifies the listener on the scheduler
        thread. The listener may call ``acquire_export_lease`` directly (it is
        on the owner thread), which is how a publication that must start in a
        request-free gap gets its pin: the engine may not run another step
        (and so never drain the mailbox) until the next request arrives, so
        the lease has to be taken here, synchronously. Commands posted to the
        mailbox during the callback are drained before returning."""
        self._on_owner_thread()
        hashes = list(block_hashes)
        newly_ready: list[BlockHash] = []
        if success:
            for bh in hashes:
                block = self._policy.get(bh)
                if block is not None and not block.is_ready:
                    newly_ready.append(bh)
        super().complete_store(hashes, success)
        if newly_ready and self.listener is not None:
            self._count("ready_notifications")
            self.listener.on_blocks_ready(newly_ready)
        self._drain_mailbox()

    # -- export lease (scheduler thread) ------------------------------------

    def ready_run(self, block_hashes: Iterable[BlockHash]) -> int:
        """Length of the READY prefix run; does not drain the mailbox."""
        hits = super().lookup(block_hashes)
        return 0 if hits is None else hits

    def acquire_export_lease(
        self, block_hashes: Iterable[BlockHash]
    ) -> ExportLease | None:
        """Atomic READY check + pin for the whole run, or None (no partial
        pin). Runs on the owner thread."""
        self._on_owner_thread()
        hashes = tuple(block_hashes)
        if not hashes or self.ready_run(hashes) != len(hashes):
            self._count("lease_miss")
            return None
        spec = self.prepare_load(hashes)
        assert isinstance(spec, CPULoadStoreSpec)
        self._next_id += 1
        lease = ExportLease(
            self._next_id, hashes, tuple(int(b) for b in spec.block_ids)
        )
        self._leases[lease.lease_id] = lease
        self._count("lease_acquired")
        return lease

    def release_export_lease(self, lease: ExportLease) -> None:
        self._on_owner_thread()
        if self._leases.pop(lease.lease_id, None) is None:
            raise KeyError(f"unknown or already released lease {lease.lease_id}")
        self.complete_load(lease.block_hashes)
        self._count("lease_released")

    def active_leases(self) -> int:
        return len(self._leases)

    # -- destination import (scheduler thread) -------------------------------

    def reserve_import(
        self, block_hashes: Iterable[BlockHash]
    ) -> ImportReservation | None:
        """Allocate CPU slots for an external payload. Blocks already present
        are not re-allocated (their IDs are not returned; the caller must not
        overwrite them). Returns None if the manager cannot make room."""
        self._on_owner_thread()
        hashes = tuple(block_hashes)
        out = self.prepare_store(hashes)
        if out is None:
            self._count("import_reserve_failed")
            return None
        spec = out.store_spec
        assert isinstance(spec, CPULoadStoreSpec)
        self._next_id += 1
        res = ImportReservation(
            self._next_id,
            tuple(out.block_hashes_to_store),
            tuple(int(b) for b in spec.block_ids),
            tuple(out.block_hashes_evicted),
        )
        self._imports[res.reservation_id] = res
        self._count("import_reserved")
        return res

    def commit_import(self, reservation: ImportReservation, success: bool) -> None:
        self._on_owner_thread()
        if self._imports.pop(reservation.reservation_id, None) is None:
            raise KeyError(f"unknown import reservation {reservation.reservation_id}")
        self.complete_store(reservation.block_hashes, success)
        self._count("import_committed" if success else "import_aborted")

    # -- mailbox API for other threads --------------------------------------

    def acquire_export_lease_async(self, block_hashes: Iterable[BlockHash]) -> Future:
        hashes = tuple(block_hashes)
        return self._post(lambda: self.acquire_export_lease(hashes))

    def release_export_lease_async(self, lease: ExportLease) -> Future:
        return self._post(lambda: self.release_export_lease(lease))

    def reserve_import_async(self, block_hashes: Iterable[BlockHash]) -> Future:
        hashes = tuple(block_hashes)
        return self._post(lambda: self.reserve_import(hashes))

    def commit_import_async(
        self, reservation: ImportReservation, success: bool
    ) -> Future:
        return self._post(lambda: self.commit_import(reservation, success))

    def ready_run_async(self, block_hashes: Iterable[BlockHash]) -> Future:
        hashes = tuple(block_hashes)
        return self._post(lambda: self.ready_run(hashes))


# --------------------------------------------------------------------------
# worker side
# --------------------------------------------------------------------------


class CpuPayloadBridge:
    """Zero-copy access to the worker's CPU KV tensors by local block ID."""

    def __init__(self, cpu_tensors: list[Any]):
        if not cpu_tensors:
            raise ValueError("no CPU tensors")
        for t in cpu_tensors:
            if t.device.type != "cpu" or t.ndim != 2:
                raise ValueError("expected 2-D CPU int8 tensors (num_blocks, page)")
        self.cpu_tensors = cpu_tensors
        self.num_blocks = int(cpu_tensors[0].shape[0])
        self.page_sizes = [int(t.shape[1]) for t in cpu_tensors]
        self.block_bytes = sum(self.page_sizes)

    def _check(self, block_ids: Iterable[int]) -> tuple[int, ...]:
        ids = tuple(int(b) for b in block_ids)
        for b in ids:
            if b < 0 or b >= self.num_blocks:
                raise IndexError(f"block id {b} outside [0, {self.num_blocks})")
        return ids

    def export_views(self, block_ids: Iterable[int]) -> Iterator[memoryview]:
        """Per block, per tensor memoryviews in a fixed order (no copy)."""
        for b in self._check(block_ids):
            for t in self.cpu_tensors:
                yield memoryview(t[b].numpy())

    def gather(self, block_ids: Iterable[int]) -> bytearray:
        out = bytearray()
        for view in self.export_views(block_ids):
            out += view
        return out

    def checksum(self, block_ids: Iterable[int]) -> str:
        h = hashlib.sha256()
        for view in self.export_views(block_ids):
            h.update(view)
        return h.hexdigest()

    def import_payload(
        self, block_ids: Iterable[int], payload: memoryview | bytes
    ) -> None:
        """Write ``payload`` (same layout as ``gather``) into the given slots."""
        ids = self._check(block_ids)
        view = memoryview(payload)
        if view.nbytes != len(ids) * self.block_bytes:
            raise ValueError(
                f"payload {view.nbytes} bytes != {len(ids)} blocks x {self.block_bytes}"
            )
        import numpy as np

        off = 0
        for b in ids:
            for t, size in zip(self.cpu_tensors, self.page_sizes):
                dst = t[b].numpy()
                dst[:] = np.frombuffer(view[off : off + size], dtype=np.int8)
                off += size


# --------------------------------------------------------------------------
# spec
# --------------------------------------------------------------------------

_REGISTRY: dict[str, Any] = {}


def register_listener(listener: ReadyListener | None) -> None:
    """Process-global listener installed before the engine builds the spec."""
    _REGISTRY["listener"] = listener


def current_manager() -> VampCPUOffloadingManager | None:
    return _REGISTRY.get("manager")


def current_bridge() -> CpuPayloadBridge | None:
    return _REGISTRY.get("bridge")


class VampOffloadingSpec(CPUOffloadingSpec):
    """Drop-in for CPUOffloadingSpec with the VAMP scheduler/worker hooks."""

    def get_manager(self) -> OffloadingManager:
        if not self._manager:
            kv_events_config = self.vllm_config.kv_events_config
            enable_events = (
                kv_events_config is not None and kv_events_config.enable_kv_cache_events
            )
            assert len(self.gpu_block_size) == 1
            offloaded_block_size = self.gpu_block_size[0] * self.block_size_factor
            manager = VampCPUOffloadingManager(
                block_size=offloaded_block_size,
                num_blocks=self.num_blocks,
                cache_policy=self.eviction_policy,
                enable_events=enable_events,
                listener=_REGISTRY.get("listener"),
            )
            store_threshold = int(self.extra_config.get("store_threshold", 0))
            if store_threshold >= 2:
                raise ValueError(
                    "VampOffloadingSpec does not wrap FilterReusedOffloadingManager; "
                    "set store_threshold < 2"
                )
            self._manager = manager
            _REGISTRY["manager"] = manager
        return self._manager

    def get_handlers(
        self, kv_caches: CanonicalKVCaches
    ) -> Iterator[tuple[type[LoadStoreSpec], type[LoadStoreSpec], OffloadingHandler]]:
        yield from super().get_handlers(kv_caches)
        assert self._handlers is not None
        bridge = CpuPayloadBridge(self._handlers.cpu_to_gpu_handler.src_tensors)
        _REGISTRY["bridge"] = bridge
