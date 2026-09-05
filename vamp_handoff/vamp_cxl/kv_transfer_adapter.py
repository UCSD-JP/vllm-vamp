# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""Worker/store/transport contracts, GPU-free fakes and the lifecycle
coordinator (spec §5-§7).

Real adapters bind to ``vllm.v1.kv_offload`` and to the fixed external
shared-memory API only in an approved gate. Nothing in this module loads a
library, opens a device or touches a socket. Fakes model the *invariants*
(atomic ready+pin, generation checks, quiescence before release, one
terminal event per job) rather than performance: fake durations are never
reported as measured transfer times.
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .keys import (
    GIB,
    MIB,
    IdSource,
    JobId,
    LayoutDescriptor,
    LeaseId,
    ModelKey,
    PayloadRef,
    PrefixKey,
    RequestKey,
)
from .offload_policy import (
    CalibrationTable,
    CpuState,
    CxlState,
    DemandAction,
    DemandDecision,
    OffloadAction,
    OffloadDecision,
    OffloadPolicy,
    PolicyObservation,
    Reason,
    RequestEvent,
    StateChangeEvent,
    StateChangeKind,
    TransferResultEvent,
    TurnEndEvent,
)

# --------------------------------------------------------------------------
# clock
# --------------------------------------------------------------------------


class Clock(ABC):
    @abstractmethod
    def now_ns(self) -> int: ...


class FakeClock(Clock):
    def __init__(self, start_ns: int = 0):
        self._now = start_ns

    def now_ns(self) -> int:
        return self._now

    def advance_ns(self, delta_ns: int) -> int:
        if delta_ns < 0:
            raise ValueError("clock cannot go backwards")
        self._now += delta_ns
        return self._now

    def advance_ms(self, delta_ms: float) -> int:
        return self.advance_ns(int(delta_ms * 1_000_000))


# --------------------------------------------------------------------------
# capability report (spec §5)
# --------------------------------------------------------------------------


class CapabilityStatus(str, Enum):
    SUPPORTED = "SUPPORTED"
    PARTIAL = "PARTIAL"
    UNSUPPORTED = "UNSUPPORTED"
    UNVERIFIED = "UNVERIFIED"
    BLOCKED = "BLOCKED"


REQUIRED_CAPABILITIES: tuple[str, ...] = (
    "exact_target_worker",
    "cpu_ready_notification",
    "export_lifetime_protection",
    "destination_import",
    "engine_compatible_hash_layout",
    "transfer_completion_notification",
    "cancellation_semantics",
    "shared_offset_check",
    "cross_host_visibility_primitive",
)


@dataclass(frozen=True)
class Capability:
    name: str
    status: CapabilityStatus
    evidence: str
    note: str = ""


@dataclass
class CapabilityReport:
    backend: str
    items: dict[str, Capability]
    chunk_bytes: int
    staging_budget_bytes: int
    is_mock: bool

    def __post_init__(self) -> None:
        missing = [c for c in REQUIRED_CAPABILITIES if c not in self.items]
        if missing:
            raise ValueError(f"capability report missing {missing}")

    def not_supported(self) -> list[str]:
        return [
            name
            for name, cap in self.items.items()
            if cap.status != CapabilityStatus.SUPPORTED
        ]

    def as_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "is_mock": self.is_mock,
            "chunk_bytes": self.chunk_bytes,
            "staging_budget_bytes": self.staging_budget_bytes,
            "items": {
                name: {
                    "status": cap.status.value,
                    "evidence": cap.evidence,
                    "note": cap.note,
                }
                for name, cap in self.items.items()
            },
        }


def audit_vllm_0_19_offload() -> CapabilityReport:
    """Read-only audit of the pinned vLLM v0.19.0 offload hooks.

    Statuses are derived from reading the pinned sources (see
    docs/capability-audit.md); nothing here is hard-coded as supported
    without a named function backing it.
    """
    S, P, U, V, B = (
        CapabilityStatus.SUPPORTED,
        CapabilityStatus.PARTIAL,
        CapabilityStatus.UNSUPPORTED,
        CapabilityStatus.UNVERIFIED,
        CapabilityStatus.BLOCKED,
    )
    items = {
        "exact_target_worker": Capability(
            "exact_target_worker",
            V,
            "dynamo/vllm/handlers.py.ref DecodeWorkerHandler.generate; "
            "kv_router/scheduler.rs.ref",
            "Router chooses the worker; no per-request pin verified. Candidate: "
            "per-worker single-target endpoint/namespace, to be confirmed on gate G-C.",
        ),
        "cpu_ready_notification": Capability(
            "cpu_ready_notification",
            P,
            "offloading/scheduler.py update_connector_output -> manager.complete_store",
            "READY becomes visible to the scheduler only after finished_sending; "
            "no external subscription. Needs an adapter hook in the scheduler process.",
        ),
        "export_lifetime_protection": Capability(
            "export_lifetime_protection",
            P,
            "cpu/manager.py prepare_load ref_cnt++ / complete_load ref_cnt--",
            "Pinning exists but is scheduler-thread local and asserts on misuse; "
            "not a thread-safe lease API for an external RPC.",
        ),
        "destination_import": Capability(
            "destination_import",
            P,
            "worker/cpu_gpu.py SingleDirectionOffloadingHandler(CPU->GPU); "
            "offloading/scheduler.py update_state_after_alloc",
            "CPU->GPU restore exists for the local CPU tier only. Importing an "
            "external payload into the destination's CPU tensors has no hook.",
        ),
        "engine_compatible_hash_layout": Capability(
            "engine_compatible_hash_layout",
            P,
            "vllm/v1/request.py block_hashes; kv_offload/spec.py CanonicalKVCaches",
            "Block hashes and canonical layout are available in-process; the "
            "cross-host ModelKey/PrefixKey wrapper is ours and unverified.",
        ),
        "transfer_completion_notification": Capability(
            "transfer_completion_notification",
            S,
            "worker/cpu_gpu.py get_finished (CUDA end_event.query)",
            "Local GPU<->CPU completion only; failures are asserted, not reported "
            "(offloading/worker.py get_finished asserts success).",
        ),
        "cancellation_semantics": Capability(
            "cancellation_semantics",
            U,
            "worker/worker.py OffloadingHandler has transfer_async/get_finished/wait "
            "only",
            "No cancel primitive; a submitted transfer runs to completion.",
        ),
        "shared_offset_check": Capability(
            "shared_offset_check",
            B,
            "docs/fixed-shared-memory-api.md: offset origin needs operator "
            "confirmation",
            "Slice-relative vs device offset origin unconfirmed; blocked until "
            "the provider confirms the mapping base.",
        ),
        "cross_host_visibility_primitive": Capability(
            "cross_host_visibility_primitive",
            B,
            "docs/fixed-shared-memory-api.md: no flush/fence exposed",
            "CPU cache flush is not GPU-DMA coherence; primitive to be confirmed.",
        ),
    }
    return CapabilityReport(
        backend="vllm-0.19.0-cpu-offload",
        items=items,
        chunk_bytes=32 * 160 * 1024,
        staging_budget_bytes=64 * MIB,
        is_mock=False,
    )


def fake_capabilities(chunk_bytes: int, staging_budget_bytes: int) -> CapabilityReport:
    items = {
        name: Capability(name, CapabilityStatus.SUPPORTED, "fake backend", "mock only")
        for name in REQUIRED_CAPABILITIES
    }
    return CapabilityReport(
        backend="fake",
        items=items,
        chunk_bytes=chunk_bytes,
        staging_budget_bytes=staging_budget_bytes,
        is_mock=True,
    )


# --------------------------------------------------------------------------
# offsets (spec §5: exactly one place applies slice_start)
# --------------------------------------------------------------------------


class OffsetError(ValueError):
    pass


@dataclass(frozen=True)
class OffsetMapper:
    slice_start_bytes: int = 64 * GIB
    slice_len_bytes: int = 64 * GIB
    provider_maps_slice_relative: bool | None = None  # None = unconfirmed

    def check_range(self, relative_offset: int, nbytes: int) -> None:
        if relative_offset < 0 or nbytes <= 0:
            raise OffsetError("negative offset or non-positive length")
        if relative_offset + nbytes > self.slice_len_bytes:
            raise OffsetError(
                f"[{relative_offset}, {relative_offset + nbytes}) exceeds slice "
                f"length {self.slice_len_bytes}"
            )

    def device_offset(self, relative_offset: int, nbytes: int) -> int:
        self.check_range(relative_offset, nbytes)
        if self.provider_maps_slice_relative is None:
            raise OffsetError(
                "offset origin unconfirmed by provider; refusing to compute a "
                "device offset (capability shared_offset_check is BLOCKED)"
            )
        if self.provider_maps_slice_relative:
            return relative_offset
        return self.slice_start_bytes + relative_offset


# --------------------------------------------------------------------------
# transport
# --------------------------------------------------------------------------


class TransferKind(str, Enum):
    CXL_WRITE = "CXL_WRITE"
    CXL_READ = "CXL_READ"
    NETWORK_COPY = "NETWORK_COPY"


class TransferPriority(int, Enum):
    DEMAND = 0
    BACKGROUND = 1


class TransferEventKind(str, Enum):
    STARTED = "STARTED"
    COMPLETED = "COMPLETED"
    VISIBLE = "VISIBLE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


TERMINAL_EVENTS = frozenset(
    {TransferEventKind.COMPLETED, TransferEventKind.FAILED, TransferEventKind.CANCELLED}
)


class JobState(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    DONE = "DONE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True)
class CompletionProof:
    job_id: JobId
    allocator_id: str | None
    generation: int | None
    reservation_id: str | None
    checksum: str
    nbytes: int


@dataclass(frozen=True)
class TransferEvent:
    kind: TransferEventKind
    job_id: JobId
    now_ns: int
    proof: CompletionProof | None = None
    error: str | None = None
    bytes_done: int = 0


@dataclass
class TransferJob:
    job_id: JobId
    kind: TransferKind
    priority: TransferPriority
    source: str
    destination: str
    nbytes: int
    chunk_bytes: int
    service_ns: int
    visibility_ns: int
    checksum: str
    allocator_id: str | None = None
    generation: int | None = None
    reservation_id: str | None = None
    submitted_ns: int = 0
    started_ns: int | None = None
    finished_ns: int | None = None
    visible_ns: int | None = None
    state: JobState = JobState.QUEUED
    chunks_done: int = 0
    cancel_requested_ns: int | None = None
    fail_at_chunk: int | None = None
    terminal_emitted: bool = False
    visible_emitted: bool = False
    _next_chunk_ns: int = 0

    @property
    def chunks_total(self) -> int:
        return max(1, -(-self.nbytes // self.chunk_bytes))

    @property
    def chunk_service_ns(self) -> int:
        return max(1, self.service_ns // self.chunks_total)

    def is_terminal(self) -> bool:
        return self.state in (JobState.DONE, JobState.FAILED, JobState.CANCELLED)


class FakeTransport:
    """Chunked fake transport. Cancellation quiesces at a chunk boundary;
    a failure is terminal at the chunk where it was injected. Exactly one
    terminal event is emitted per job."""

    def __init__(self, clock: Clock):
        self.clock = clock
        self.jobs: dict[JobId, TransferJob] = {}
        self._events: list[TransferEvent] = []

    def submit(self, job: TransferJob) -> JobId:
        if job.job_id in self.jobs:
            raise ValueError(f"duplicate job {job.job_id}")
        job.submitted_ns = self.clock.now_ns()
        self.jobs[job.job_id] = job
        return job.job_id

    def start(self, job_id: JobId) -> None:
        job = self.jobs[job_id]
        assert job.state == JobState.QUEUED
        now = self.clock.now_ns()
        job.state = JobState.RUNNING
        job.started_ns = now
        job._next_chunk_ns = now + job.chunk_service_ns
        self._events.append(TransferEvent(TransferEventKind.STARTED, job_id, now))

    def cancel(self, job_id: JobId) -> None:
        job = self.jobs[job_id]
        if job.is_terminal():
            return
        job.cancel_requested_ns = self.clock.now_ns()
        if job.state == JobState.QUEUED:
            self._terminate(job, JobState.CANCELLED, "cancelled before start")

    def inject_failure(self, job_id: JobId, at_chunk: int) -> None:
        self.jobs[job_id].fail_at_chunk = at_chunk

    def _terminate(self, job: TransferJob, state: JobState, error: str | None) -> None:
        assert not job.terminal_emitted, "second terminal event"
        now = self.clock.now_ns()
        job.state = state
        job.finished_ns = now
        job.terminal_emitted = True
        proof = None
        kind = {
            JobState.DONE: TransferEventKind.COMPLETED,
            JobState.FAILED: TransferEventKind.FAILED,
            JobState.CANCELLED: TransferEventKind.CANCELLED,
        }[state]
        if state == JobState.DONE:
            proof = CompletionProof(
                job_id=job.job_id,
                allocator_id=job.allocator_id,
                generation=job.generation,
                reservation_id=job.reservation_id,
                checksum=job.checksum,
                nbytes=job.nbytes,
            )
        self._events.append(
            TransferEvent(
                kind,
                job.job_id,
                now,
                proof=proof,
                error=error,
                bytes_done=min(job.nbytes, job.chunks_done * job.chunk_bytes),
            )
        )

    def advance(self) -> None:
        now = self.clock.now_ns()
        for job in self.jobs.values():
            if job.state == JobState.RUNNING:
                while job.state == JobState.RUNNING and now >= job._next_chunk_ns:
                    job.chunks_done += 1
                    if (
                        job.fail_at_chunk is not None
                        and job.chunks_done >= job.fail_at_chunk
                    ):
                        self._terminate(
                            job, JobState.FAILED, "injected transport error"
                        )
                    elif job.cancel_requested_ns is not None:
                        self._terminate(
                            job, JobState.CANCELLED, "cancelled at chunk boundary"
                        )
                    elif job.chunks_done >= job.chunks_total:
                        self._terminate(job, JobState.DONE, None)
                    else:
                        job._next_chunk_ns += job.chunk_service_ns
            if (
                job.state == JobState.DONE
                and not job.visible_emitted
                and job.finished_ns is not None
                and now >= job.finished_ns + job.visibility_ns
            ):
                job.visible_emitted = True
                job.visible_ns = job.finished_ns + job.visibility_ns
                self._events.append(
                    TransferEvent(
                        TransferEventKind.VISIBLE,
                        job.job_id,
                        job.visible_ns,
                        proof=CompletionProof(
                            job.job_id,
                            job.allocator_id,
                            job.generation,
                            job.reservation_id,
                            job.checksum,
                            job.nbytes,
                        ),
                    )
                )

    def poll(self) -> list[TransferEvent]:
        self.advance()
        events, self._events = self._events, []
        return events

    def active_jobs(self) -> int:
        return sum(1 for j in self.jobs.values() if not j.is_terminal())


# --------------------------------------------------------------------------
# executor: demand reads before background writes (spec §7)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ExecutorLimits:
    max_inflight_publications_per_source: int = 1
    max_inflight_publications: int = 2
    staging_budget_bytes: int = 64 * MIB
    chunk_bytes: int = 32 * 160 * 1024  # one offload block at 160 KiB/token


class TransferExecutor:
    def __init__(self, transport: FakeTransport, limits: ExecutorLimits, clock: Clock):
        self.transport = transport
        self.limits = limits
        self.clock = clock
        self._queue: list[tuple[int, int, JobId]] = []
        self._seq = 0
        self._running: dict[JobId, TransferJob] = {}
        self.staging_in_use = 0
        self.staging_high_watermark = 0
        self.queue_wait_ns: dict[JobId, int] = {}

    def submit(self, job: TransferJob) -> JobId:
        self.transport.submit(job)
        self._seq += 1
        self._queue.append((int(job.priority), self._seq, job.job_id))
        self.schedule()
        return job.job_id

    def cancel(self, job_id: JobId) -> None:
        self.transport.cancel(job_id)
        self._queue = [q for q in self._queue if q[2] != job_id]

    def _publications_inflight(self, source: str | None = None) -> int:
        return sum(
            1
            for j in self._running.values()
            if j.kind == TransferKind.CXL_WRITE
            and (source is None or j.source == source)
        )

    def schedule(self) -> None:
        self._queue.sort()
        remaining: list[tuple[int, int, JobId]] = []
        for item in self._queue:
            job = self.transport.jobs[item[2]]
            if job.is_terminal():
                continue
            if self._can_start(job):
                self._start(job)
            else:
                remaining.append(item)
        self._queue = remaining

    def _can_start(self, job: TransferJob) -> bool:
        if self.staging_in_use + job.chunk_bytes > self.limits.staging_budget_bytes:
            return False
        if job.kind == TransferKind.CXL_WRITE:
            if self._publications_inflight() >= self.limits.max_inflight_publications:
                return False
            if (
                self._publications_inflight(job.source)
                >= self.limits.max_inflight_publications_per_source
            ):
                return False
        return True

    def _start(self, job: TransferJob) -> None:
        self.staging_in_use += job.chunk_bytes
        self.staging_high_watermark = max(
            self.staging_high_watermark, self.staging_in_use
        )
        self._running[job.job_id] = job
        self.queue_wait_ns[job.job_id] = self.clock.now_ns() - job.submitted_ns
        self.transport.start(job.job_id)

    def poll(self) -> list[TransferEvent]:
        events = self.transport.poll()
        for ev in events:
            if ev.kind in TERMINAL_EVENTS:
                job = self._running.pop(ev.job_id, None)
                if job is not None:
                    self.staging_in_use -= job.chunk_bytes
        self.schedule()
        return events

    def demand_reads_pending(self) -> int:
        queued = sum(
            1
            for _, _, jid in self._queue
            if self.transport.jobs[jid].kind == TransferKind.CXL_READ
        )
        running = sum(
            1 for j in self._running.values() if j.kind == TransferKind.CXL_READ
        )
        return queued + running

    def inflight(self) -> int:
        return len(self._running) + len(self._queue)


# --------------------------------------------------------------------------
# shared store (spec §5, §6)
# --------------------------------------------------------------------------


class ReserveStatus(str, Enum):
    RESERVED = "RESERVED"
    ALREADY_READY = "ALREADY_READY"
    ALREADY_WRITING = "ALREADY_WRITING"
    BUSY = "BUSY"
    REJECTED_CAPACITY = "REJECTED_CAPACITY"
    REJECTED_BOUNDS = "REJECTED_BOUNDS"


@dataclass(frozen=True)
class Reservation:
    reservation_id: str
    prefix: PrefixKey
    allocator_id: str
    generation: int
    relative_offset: int
    payload_bytes: int
    rounded_bytes: int
    writer_id: str


@dataclass(frozen=True)
class ReserveResult:
    status: ReserveStatus
    reservation: Reservation | None = None
    existing_generation: int | None = None


@dataclass(frozen=True)
class LeaseGrant:
    lease_id: LeaseId
    payload_ref: PayloadRef
    checksum: str


@dataclass(frozen=True)
class Miss:
    reason: str


@dataclass(frozen=True)
class CommitResult:
    accepted: bool
    reason: str


@dataclass
class Slot:
    prefix: PrefixKey
    state: CxlState
    generation: int
    reservation_id: str | None
    relative_offset: int
    payload_bytes: int
    rounded_bytes: int
    writer_id: str | None
    checksum: str | None = None
    readers: dict[LeaseId, str] = field(default_factory=dict)


@dataclass(frozen=True)
class StoreCapacity:
    nominal_bytes: int = 64 * GIB
    metadata_reserve_bytes: int = 256 * MIB
    block_bytes: int = 32 * 160 * 1024

    @property
    def payload_capacity_bytes(self) -> int:
        usable = self.nominal_bytes - self.metadata_reserve_bytes
        return usable - usable % self.block_bytes

    def round_up(self, nbytes: int) -> int:
        return -(-nbytes // self.block_bytes) * self.block_bytes


@dataclass(frozen=True)
class SharedSnapshot:
    snapshot_id: str
    taken_ns: int
    states: dict[PrefixKey, tuple[CxlState, int]]


@dataclass(frozen=True)
class StoreUsage:
    payload_capacity_bytes: int
    occupied_bytes: int
    slots_by_state: dict[str, int]
    active_reader_leases: int
    active_writer_slots: int


class SharedKVStore(ABC):
    @abstractmethod
    def reserve(
        self, prefix: PrefixKey, nbytes: int, writer_id: str
    ) -> ReserveResult: ...

    @abstractmethod
    def mark_writing(self, reservation: Reservation) -> None: ...

    @abstractmethod
    def commit_ready(
        self, reservation: Reservation, proof: CompletionProof
    ) -> CommitResult: ...

    @abstractmethod
    def acquire_ready(
        self, prefix: PrefixKey, reader_id: str, expected_generation: int | None = None
    ) -> LeaseGrant | Miss: ...

    @abstractmethod
    def release(self, lease_id: LeaseId) -> None: ...

    @abstractmethod
    def abort(self, reservation: Reservation) -> None: ...

    @abstractmethod
    def settle(self, reservation: Reservation, terminal: TransferEvent) -> None: ...


class FakeSharedStore(SharedKVStore):
    def __init__(
        self,
        clock: Clock,
        ids: IdSource,
        capacity: StoreCapacity,
        mapper: OffsetMapper,
        layout: LayoutDescriptor,
        allocator_id: str = "alloc-fake-1",
    ):
        self.clock = clock
        self.ids = ids
        self.capacity = capacity
        self.mapper = mapper
        self.layout = layout
        self.allocator_id = allocator_id
        self.slots: dict[PrefixKey, Slot] = {}
        self._generation = 0
        self._free: list[tuple[int, int]] = [(0, capacity.payload_capacity_bytes)]
        self._leases: dict[LeaseId, PrefixKey] = {}
        self._snapshots = 0

    # -- allocation -------------------------------------------------------

    def _alloc(self, rounded: int) -> int | None:
        for idx, (off, size) in enumerate(self._free):
            if size >= rounded:
                if size == rounded:
                    del self._free[idx]
                else:
                    self._free[idx] = (off + rounded, size - rounded)
                return off
        return None

    def _dealloc(self, off: int, size: int) -> None:
        self._free.append((off, size))
        self._free.sort()
        merged: list[tuple[int, int]] = []
        for o, s in self._free:
            if merged and merged[-1][0] + merged[-1][1] == o:
                merged[-1] = (merged[-1][0], merged[-1][1] + s)
            else:
                merged.append((o, s))
        self._free = merged

    # -- SharedKVStore -----------------------------------------------------

    def reserve(self, prefix: PrefixKey, nbytes: int, writer_id: str) -> ReserveResult:
        slot = self.slots.get(prefix)
        if slot is not None:
            if slot.state == CxlState.READY:
                return ReserveResult(
                    ReserveStatus.ALREADY_READY, existing_generation=slot.generation
                )
            if slot.state in (CxlState.RESERVED, CxlState.WRITING):
                return ReserveResult(
                    ReserveStatus.ALREADY_WRITING, existing_generation=slot.generation
                )
            return ReserveResult(
                ReserveStatus.BUSY, existing_generation=slot.generation
            )
        rounded = self.capacity.round_up(nbytes)
        off = self._alloc(rounded)
        if off is None:
            return ReserveResult(ReserveStatus.REJECTED_CAPACITY)
        try:
            self.mapper.check_range(off, rounded)
        except OffsetError:
            self._dealloc(off, rounded)
            return ReserveResult(ReserveStatus.REJECTED_BOUNDS)
        self._generation += 1
        res = Reservation(
            reservation_id=self.ids.next("res"),
            prefix=prefix,
            allocator_id=self.allocator_id,
            generation=self._generation,
            relative_offset=off,
            payload_bytes=nbytes,
            rounded_bytes=rounded,
            writer_id=writer_id,
        )
        self.slots[prefix] = Slot(
            prefix=prefix,
            state=CxlState.RESERVED,
            generation=res.generation,
            reservation_id=res.reservation_id,
            relative_offset=off,
            payload_bytes=nbytes,
            rounded_bytes=rounded,
            writer_id=writer_id,
        )
        return ReserveResult(ReserveStatus.RESERVED, reservation=res)

    def _slot_for(self, reservation: Reservation) -> Slot | None:
        slot = self.slots.get(reservation.prefix)
        if slot is None or slot.generation != reservation.generation:
            return None
        if slot.reservation_id != reservation.reservation_id:
            return None
        return slot

    def mark_writing(self, reservation: Reservation) -> None:
        slot = self._slot_for(reservation)
        if slot is not None and slot.state == CxlState.RESERVED:
            slot.state = CxlState.WRITING

    def commit_ready(
        self, reservation: Reservation, proof: CompletionProof
    ) -> CommitResult:
        slot = self._slot_for(reservation)
        if slot is None:
            return CommitResult(False, "stale reservation (generation/id mismatch)")
        if (
            proof.allocator_id != self.allocator_id
            or proof.generation != slot.generation
            or proof.reservation_id != slot.reservation_id
        ):
            return CommitResult(
                False, "completion proof does not match slot generation"
            )
        if slot.state != CxlState.WRITING:
            return CommitResult(False, f"slot not WRITING ({slot.state.value})")
        if proof.nbytes != slot.payload_bytes:
            return CommitResult(False, "byte count mismatch")
        slot.state = CxlState.READY
        slot.checksum = proof.checksum
        return CommitResult(True, "READY")

    def acquire_ready(
        self, prefix: PrefixKey, reader_id: str, expected_generation: int | None = None
    ) -> LeaseGrant | Miss:
        slot = self.slots.get(prefix)
        if slot is None:
            return Miss(CxlState.ABSENT.value)
        if slot.state != CxlState.READY:
            return Miss(slot.state.value)
        if expected_generation is not None and expected_generation != slot.generation:
            return Miss("GENERATION_MISMATCH")
        lease = self.ids.lease()
        slot.readers[lease] = reader_id
        self._leases[lease] = prefix
        assert slot.checksum is not None
        return LeaseGrant(
            lease_id=lease,
            payload_ref=PayloadRef(
                prefix=prefix,
                allocator_id=self.allocator_id,
                generation=slot.generation,
                relative_offset=slot.relative_offset,
                length=slot.payload_bytes,
                layout=self.layout,
            ),
            checksum=slot.checksum,
        )

    def release(self, lease_id: LeaseId) -> None:
        prefix = self._leases.pop(lease_id)
        slot = self.slots[prefix]
        del slot.readers[lease_id]
        if slot.state == CxlState.EVICTING and not slot.readers:
            self._free_slot(slot)

    def abort(self, reservation: Reservation) -> None:
        slot = self._slot_for(reservation)
        if slot is not None and slot.state in (CxlState.RESERVED, CxlState.WRITING):
            slot.state = CxlState.ABORTING

    def settle(self, reservation: Reservation, terminal: TransferEvent) -> None:
        if terminal.kind not in TERMINAL_EVENTS:
            raise ValueError("settle requires a terminal transport event")
        slot = self._slot_for(reservation)
        if slot is None:
            return
        if slot.state == CxlState.ABORTING:
            self._free_slot(slot)

    # -- eviction / snapshot / accounting ---------------------------------

    def request_evict(self, prefix: PrefixKey) -> str:
        slot = self.slots.get(prefix)
        if slot is None:
            return "ABSENT"
        if slot.state in (CxlState.RESERVED, CxlState.WRITING, CxlState.ABORTING):
            return "REFUSED_WRITER_PROTECTED"
        if slot.readers:
            slot.state = CxlState.EVICTING
            return "DEFERRED_READERS_ACTIVE"
        self._free_slot(slot)
        return "EVICTED"

    def _free_slot(self, slot: Slot) -> None:
        assert not slot.readers
        del self.slots[slot.prefix]
        self._dealloc(slot.relative_offset, slot.rounded_bytes)

    def state_of(self, prefix: PrefixKey) -> tuple[CxlState, int | None]:
        slot = self.slots.get(prefix)
        if slot is None:
            return CxlState.ABSENT, None
        return slot.state, slot.generation

    def snapshot(self) -> SharedSnapshot:
        self._snapshots += 1
        return SharedSnapshot(
            snapshot_id=f"snap-{self._snapshots}",
            taken_ns=self.clock.now_ns(),
            states={p: (s.state, s.generation) for p, s in self.slots.items()},
        )

    def usage(self) -> StoreUsage:
        by_state: dict[str, int] = {}
        occupied = 0
        for slot in self.slots.values():
            by_state[slot.state.value] = by_state.get(slot.state.value, 0) + 1
            occupied += slot.rounded_bytes
        return StoreUsage(
            payload_capacity_bytes=self.capacity.payload_capacity_bytes,
            occupied_bytes=occupied,
            slots_by_state=by_state,
            active_reader_leases=len(self._leases),
            active_writer_slots=sum(
                1
                for s in self.slots.values()
                if s.state in (CxlState.RESERVED, CxlState.WRITING, CxlState.ABORTING)
            ),
        )


# --------------------------------------------------------------------------
# worker adapter (spec §5)
# --------------------------------------------------------------------------


@dataclass
class CpuEntry:
    prefix: PrefixKey
    state: CpuState
    ready_at_ns: int
    generation: int
    nbytes: int
    checksum: str
    ref_cnt: int = 0


@dataclass(frozen=True)
class SourceLease:
    lease_id: LeaseId
    worker_id: str
    prefix: PrefixKey
    generation: int
    nbytes: int
    checksum: str


class WorkerKVAdapter(ABC):
    @abstractmethod
    def inspect_capabilities(self) -> CapabilityReport: ...

    @abstractmethod
    def acquire_ready_prefix(
        self, prefix: PrefixKey, deadline_ns: int
    ) -> SourceLease | Miss: ...

    @abstractmethod
    def release(self, lease_id: LeaseId) -> None: ...

    @abstractmethod
    def export(
        self,
        lease_id: LeaseId,
        executor: TransferExecutor,
        destination: Reservation | str,
        kind: TransferKind,
        priority: TransferPriority,
        service_ns: int,
        visibility_ns: int,
    ) -> JobId: ...

    @abstractmethod
    def import_and_restore(
        self,
        request: RequestKey,
        payload_ref: PayloadRef,
        checksum: str,
        executor: TransferExecutor,
        service_ns: int,
    ) -> JobId: ...


def payload_checksum(prefix: PrefixKey, generation: int) -> str:
    h = hashlib.sha256()
    h.update(prefix.short().encode())
    h.update(str(generation).encode())
    return h.hexdigest()[:16]


class FakeWorkerKV(WorkerKVAdapter):
    def __init__(
        self,
        worker_id: str,
        clock: Clock,
        ids: IdSource,
        layout: LayoutDescriptor,
        limits: ExecutorLimits,
    ):
        self.worker_id = worker_id
        self.clock = clock
        self.ids = ids
        self.layout = layout
        self.limits = limits
        self.cpu: dict[PrefixKey, CpuEntry] = {}
        self.gpu_resident: set[PrefixKey] = set()
        self.leases: dict[LeaseId, SourceLease] = {}
        self._generation = 0

    def inspect_capabilities(self) -> CapabilityReport:
        return fake_capabilities(
            self.limits.chunk_bytes, self.limits.staging_budget_bytes
        )

    # -- residency modelling ----------------------------------------------

    def begin_offload(self, prefix: PrefixKey, ready_after_ns: int) -> None:
        """GPU->private DRAM offload started; READY only after ready_after_ns."""
        self._generation += 1
        self.cpu[prefix] = CpuEntry(
            prefix=prefix,
            state=CpuState.PENDING,
            ready_at_ns=self.clock.now_ns() + ready_after_ns,
            generation=self._generation,
            nbytes=self.layout.bytes_for_tokens(prefix.complete_tokens),
            checksum=payload_checksum(prefix, self._generation),
        )

    def tick(self) -> list[PrefixKey]:
        now = self.clock.now_ns()
        ready: list[PrefixKey] = []
        for entry in self.cpu.values():
            if entry.state == CpuState.PENDING and now >= entry.ready_at_ns:
                entry.state = CpuState.READY
                ready.append(entry.prefix)
        return ready

    def cpu_state(self, prefix: PrefixKey) -> CpuState:
        entry = self.cpu.get(prefix)
        return entry.state if entry is not None else CpuState.ABSENT

    def local_ready(self, prefix: PrefixKey) -> bool:
        return prefix in self.gpu_resident or self.cpu_state(prefix) == CpuState.READY

    def evict_cpu(self, prefix: PrefixKey) -> bool:
        entry = self.cpu.get(prefix)
        if entry is None:
            return False
        if entry.ref_cnt > 0:
            return False  # pinned by an export lease
        entry.state = CpuState.EVICTED
        return True

    def evict_gpu(self, prefix: PrefixKey) -> None:
        self.gpu_resident.discard(prefix)

    def mark_gpu_resident(self, prefix: PrefixKey) -> None:
        self.gpu_resident.add(prefix)

    # -- WorkerKVAdapter --------------------------------------------------

    def acquire_ready_prefix(
        self, prefix: PrefixKey, deadline_ns: int
    ) -> SourceLease | Miss:
        entry = self.cpu.get(prefix)
        if entry is None:
            return Miss(CpuState.ABSENT.value)
        if entry.state != CpuState.READY:
            return Miss(entry.state.value)
        entry.ref_cnt += 1
        lease = SourceLease(
            lease_id=self.ids.lease(),
            worker_id=self.worker_id,
            prefix=prefix,
            generation=entry.generation,
            nbytes=entry.nbytes,
            checksum=entry.checksum,
        )
        self.leases[lease.lease_id] = lease
        return lease

    def release(self, lease_id: LeaseId) -> None:
        lease = self.leases.pop(lease_id)
        entry = self.cpu[lease.prefix]
        assert entry.ref_cnt > 0
        entry.ref_cnt -= 1

    def export(
        self,
        lease_id: LeaseId,
        executor: TransferExecutor,
        destination: Reservation | str,
        kind: TransferKind,
        priority: TransferPriority,
        service_ns: int,
        visibility_ns: int,
    ) -> JobId:
        lease = self.leases[lease_id]
        entry = self.cpu[lease.prefix]
        if entry.generation != lease.generation or entry.state != CpuState.READY:
            raise RuntimeError("lease no longer matches a READY source entry")
        if isinstance(destination, Reservation):
            job = TransferJob(
                job_id=self.ids.job(),
                kind=kind,
                priority=priority,
                source=self.worker_id,
                destination=f"cxl:{destination.reservation_id}",
                nbytes=lease.nbytes,
                chunk_bytes=self.limits.chunk_bytes,
                service_ns=service_ns,
                visibility_ns=visibility_ns,
                checksum=lease.checksum,
                allocator_id=destination.allocator_id,
                generation=destination.generation,
                reservation_id=destination.reservation_id,
            )
        else:
            job = TransferJob(
                job_id=self.ids.job(),
                kind=kind,
                priority=priority,
                source=self.worker_id,
                destination=destination,
                nbytes=lease.nbytes,
                chunk_bytes=self.limits.chunk_bytes,
                service_ns=service_ns,
                visibility_ns=visibility_ns,
                checksum=lease.checksum,
            )
        return executor.submit(job)

    def import_and_restore(
        self,
        request: RequestKey,
        payload_ref: PayloadRef,
        checksum: str,
        executor: TransferExecutor,
        service_ns: int,
    ) -> JobId:
        job = TransferJob(
            job_id=self.ids.job(),
            kind=TransferKind.CXL_READ,
            priority=TransferPriority.DEMAND,
            source=f"cxl:{payload_ref.allocator_id}:{payload_ref.generation}",
            destination=self.worker_id,
            nbytes=payload_ref.length,
            chunk_bytes=self.limits.chunk_bytes,
            service_ns=service_ns,
            visibility_ns=0,
            checksum=checksum,
            allocator_id=payload_ref.allocator_id,
            generation=payload_ref.generation,
        )
        return executor.submit(job)

    def on_import_complete(self, prefix: PrefixKey, nbytes: int, checksum: str) -> None:
        self._generation += 1
        self.cpu[prefix] = CpuEntry(
            prefix=prefix,
            state=CpuState.READY,
            ready_at_ns=self.clock.now_ns(),
            generation=self._generation,
            nbytes=nbytes,
            checksum=checksum,
        )
        self.gpu_resident.add(prefix)

    def lease_count(self) -> int:
        return len(self.leases)


# --------------------------------------------------------------------------
# routing adapter (spec §5): request + destination -> actual worker receipt
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class DispatchReceipt:
    request: RequestKey
    designated_worker: str
    actual_worker: str
    receipt_id: str

    @property
    def target_verified(self) -> bool:
        return self.designated_worker == self.actual_worker


class RoutingAdapter(ABC):
    @abstractmethod
    def dispatch(self, request: RequestKey, destination: str) -> DispatchReceipt: ...


class FakeRoutingAdapter(RoutingAdapter):
    def __init__(self, ids: IdSource, misroute: dict[str, str] | None = None):
        self.ids = ids
        self.misroute = misroute or {}

    def dispatch(self, request: RequestKey, destination: str) -> DispatchReceipt:
        actual = self.misroute.get(destination, destination)
        return DispatchReceipt(request, destination, actual, self.ids.next("rcpt"))


# --------------------------------------------------------------------------
# coordinator (spec §6 lifecycle)
# --------------------------------------------------------------------------


@dataclass
class Publication:
    consideration_id: str
    prefix: PrefixKey
    source_worker: str
    lease: SourceLease
    reservation: Reservation
    job_id: JobId
    enqueued_ns: int
    completed_ns: int | None = None
    visible_ns: int | None = None
    terminal: TransferEvent | None = None


@dataclass
class Restore:
    request: RequestKey
    prefix: PrefixKey
    destination_worker: str
    lease: LeaseGrant
    job_id: JobId
    enqueued_ns: int


@dataclass
class NetworkCopy:
    request: RequestKey
    prefix: PrefixKey
    source_worker: str
    destination_worker: str
    lease: SourceLease
    job_id: JobId
    enqueued_ns: int


@dataclass(frozen=True)
class FakeServiceTimes:
    """Fake durations used only to sequence the mock; never reported as
    measured transfer times."""

    cxl_write_ns: int = 35 * 1_000_000
    cxl_visibility_ns: int = 2 * 1_000_000
    cxl_read_ns: int = 80 * 1_000_000
    network_copy_ns: int = 180 * 1_000_000


class MigrationCoordinator:
    """Executes policy decisions against adapters and reports completions.

    Invariants enforced here (spec §6): source lease acquired before reserve,
    reserve failure releases the lease at once, READY published only after
    completion + visibility, slots freed only after the transport's terminal
    event, reader leases held until import completion.
    """

    def __init__(
        self,
        clock: Clock,
        ids: IdSource,
        store: FakeSharedStore,
        workers: dict[str, FakeWorkerKV],
        executor: TransferExecutor,
        policy: OffloadPolicy,
        model_key: ModelKey,
        calibration: CalibrationTable | None,
        service: FakeServiceTimes | None = None,
        trace: Any | None = None,
    ):
        self.clock = clock
        self.ids = ids
        self.store = store
        self.workers = workers
        self.executor = executor
        self.policy = policy
        self.model_key = model_key
        self.calibration = calibration
        self.service = service or FakeServiceTimes()
        self.trace = trace
        self.publications: dict[JobId, Publication] = {}
        self.restores: dict[JobId, Restore] = {}
        self.network_copies: dict[JobId, NetworkCopy] = {}
        self.completed: list[dict[str, Any]] = []
        self.correctness_failures: list[dict[str, Any]] = []
        self.closed_sessions: set[int] = set()
        self._snapshot: SharedSnapshot = store.snapshot()
        self.counters: dict[str, int] = {}

    # -- helpers ------------------------------------------------------------

    def _count(self, key: str) -> None:
        self.counters[key] = self.counters.get(key, 0) + 1

    def _emit(self, kind: str, **fields: Any) -> None:
        if self.trace is not None:
            self.trace.write(kind, now_ns=self.clock.now_ns(), **fields)

    def refresh_snapshot(self) -> SharedSnapshot:
        self._snapshot = self.store.snapshot()
        return self._snapshot

    def observe(
        self,
        prefix: PrefixKey,
        source_worker: str | None,
        *,
        session_id: int | None = None,
        destination: str | None = None,
    ) -> PolicyObservation:
        """Observation from present state only. No ground truth enters."""
        state, generation = self._snapshot.states.get(prefix, (CxlState.ABSENT, None))
        src_state = (
            self.workers[source_worker].cpu_state(prefix)
            if source_worker is not None
            else CpuState.ABSENT
        )
        cpu_ready_workers = tuple(
            sorted(
                w
                for w, k in self.workers.items()
                if k.cpu_state(prefix) == CpuState.READY
            )
        )
        point = (
            self.calibration.lookup(prefix.complete_tokens)
            if self.calibration
            else None
        )
        return PolicyObservation(
            now_ns=self.clock.now_ns(),
            snapshot_id=self._snapshot.snapshot_id,
            snapshot_taken_ns=self._snapshot.taken_ns,
            session_closed=session_id in self.closed_sessions
            if session_id is not None
            else False,
            model_matches=prefix.model == self.model_key,
            source_cpu_state=src_state,
            cxl_state=state,
            cxl_generation=generation,
            demand_reads_pending=self.executor.demand_reads_pending(),
            calibration=point,
            calibration_id=self.calibration.calibration_id
            if self.calibration
            else None,
            local_ready_at_destination=(
                self.workers[destination].local_ready(prefix) if destination else False
            ),
            source_workers_cpu_ready=cpu_ready_workers,
        )

    # -- policy entry points --------------------------------------------------

    def turn_end(
        self, request: RequestKey, prefix: PrefixKey, source_worker: str
    ) -> OffloadDecision:
        event = TurnEndEvent(
            request=request,
            prefix=prefix,
            turn_done_ns=self.clock.now_ns(),
            consideration_id=self.ids.next("cons"),
            source_worker=source_worker,
        )
        obs = self.observe(prefix, source_worker, session_id=request.session_id)
        decision = self.policy.on_turn_end(event, obs)
        self._emit("offload_decision", **decision_fields(decision))
        self._act_offload(decision, request)
        return decision

    def state_change(
        self,
        kind: StateChangeKind,
        prefix: PrefixKey | None,
        session_id: int | None = None,
    ) -> list[OffloadDecision]:
        if prefix is None:
            return []
        cons = self.policy.consideration(prefix)
        if cons is None:
            return []
        obs = self.observe(
            prefix, cons.source_worker, session_id=cons.request.session_id
        )
        decisions = self.policy.on_state_change(
            StateChangeEvent(kind=kind, now_ns=self.clock.now_ns(), prefix=prefix), obs
        )
        for d in decisions:
            self._emit("offload_decision", **decision_fields(d))
            self._act_offload(d, cons.request)
        return decisions

    def timer(self) -> list[OffloadDecision]:
        out: list[OffloadDecision] = []
        for prefix in list(self.policy.pending_prefixes()):
            out.extend(self.state_change(StateChangeKind.TIMER, prefix))
        return out

    def close_session(self, session_id: int, prefix: PrefixKey | None) -> None:
        self.closed_sessions.add(session_id)
        self.state_change(StateChangeKind.SESSION_CLOSED, prefix, session_id)

    def request(
        self, request: RequestKey, prefix: PrefixKey, destination: str
    ) -> DemandDecision:
        obs = self.observe(
            prefix, None, session_id=request.session_id, destination=destination
        )
        decision = self.policy.on_request(
            RequestEvent(request, prefix, destination, self.clock.now_ns()), obs
        )
        self._emit("demand_decision", **decision_fields(decision))
        self._act_demand(decision)
        return decision

    # -- actions ------------------------------------------------------------

    def _act_offload(self, decision: OffloadDecision, request: RequestKey) -> None:
        if decision.action != OffloadAction.STORE_NOW:
            return
        self._count("publication_selected")
        worker = self.workers[decision.source_worker]
        lease = worker.acquire_ready_prefix(
            decision.prefix, deadline_ns=self.clock.now_ns()
        )
        if isinstance(lease, Miss):
            # Re-acquire failed after DEFER: the copy was evicted meanwhile.
            self._report(
                decision.prefix,
                decision.consideration_id,
                None,
                False,
                Reason.SKIP_SOURCE_EVICTED,
            )
            return
        res = self.store.reserve(
            decision.prefix, lease.nbytes, writer_id=decision.source_worker
        )
        if res.status != ReserveStatus.RESERVED or res.reservation is None:
            worker.release(lease.lease_id)  # immediately, spec §6.3
            reason = {
                ReserveStatus.ALREADY_READY: Reason.SKIP_ALREADY_READY,
                ReserveStatus.ALREADY_WRITING: Reason.SKIP_ALREADY_WRITING,
                ReserveStatus.BUSY: Reason.SKIP_SLOT_BUSY,
                ReserveStatus.REJECTED_CAPACITY: Reason.RESERVE_REJECTED_CAPACITY,
                ReserveStatus.REJECTED_BOUNDS: Reason.RESERVE_REJECTED_CAPACITY,
            }[res.status]
            self._report(
                decision.prefix, decision.consideration_id, None, False, reason
            )
            return
        job_id = worker.export(
            lease.lease_id,
            self.executor,
            res.reservation,
            TransferKind.CXL_WRITE,
            TransferPriority.BACKGROUND,
            self.service.cxl_write_ns,
            self.service.cxl_visibility_ns,
        )
        self.publications[job_id] = Publication(
            consideration_id=decision.consideration_id,
            prefix=decision.prefix,
            source_worker=decision.source_worker,
            lease=lease,
            reservation=res.reservation,
            job_id=job_id,
            enqueued_ns=self.clock.now_ns(),
        )
        self._count("publication_started")
        # A writer knows its own reservation at once; only remote writes wait
        # for the next snapshot refresh.
        self.refresh_snapshot()
        self._emit(
            "publication_enqueued",
            job_id=job_id.value,
            prefix=decision.prefix.short(),
            consideration_id=decision.consideration_id,
            generation=res.reservation.generation,
        )

    def cancel_publication(self, prefix: PrefixKey) -> bool:
        for pub in self.publications.values():
            if pub.prefix == prefix and pub.terminal is None:
                self.executor.cancel(pub.job_id)
                self._count("publication_cancel_requested")
                return True
        return False

    def _act_demand(self, decision: DemandDecision) -> None:
        if decision.action == DemandAction.CXL_RESTORE:
            grant = self.store.acquire_ready(
                decision.prefix,
                reader_id=decision.destination_worker,
                expected_generation=decision.cxl_generation,
            )
            if isinstance(grant, Miss):
                # State changed between snapshot and acquire: recompute, no wait.
                self._count("restore_miss_after_decision")
                self._emit(
                    "restore_miss", prefix=decision.prefix.short(), reason=grant.reason
                )
                return
            dest = self.workers[decision.destination_worker]
            job_id = dest.import_and_restore(
                decision.request,
                grant.payload_ref,
                grant.checksum,
                self.executor,
                self.service.cxl_read_ns,
            )
            self.restores[job_id] = Restore(
                decision.request,
                decision.prefix,
                decision.destination_worker,
                grant,
                job_id,
                self.clock.now_ns(),
            )
            self._count("restore_started")
        elif decision.action == DemandAction.NETWORK_COPY:
            assert decision.source_worker is not None
            src = self.workers[decision.source_worker]
            lease = src.acquire_ready_prefix(
                decision.prefix, deadline_ns=self.clock.now_ns()
            )
            if isinstance(lease, Miss):
                self._count("network_source_miss_after_decision")
                self._emit(
                    "network_source_miss",
                    prefix=decision.prefix.short(),
                    reason=lease.reason,
                )
                return
            job_id = src.export(
                lease.lease_id,
                self.executor,
                decision.destination_worker,
                TransferKind.NETWORK_COPY,
                TransferPriority.DEMAND,
                self.service.network_copy_ns,
                0,
            )
            self.network_copies[job_id] = NetworkCopy(
                decision.request,
                decision.prefix,
                decision.source_worker,
                decision.destination_worker,
                lease,
                job_id,
                self.clock.now_ns(),
            )
            self._count("network_copy_started")

    # -- completion handling ------------------------------------------------

    def _report(
        self,
        prefix: PrefixKey,
        consideration_id: str | None,
        job_id: JobId | None,
        success: bool,
        reason: Reason,
    ) -> None:
        event = TransferResultEvent(
            job_id=job_id.value if job_id else None,
            prefix=prefix,
            consideration_id=consideration_id,
            success=success,
            reason=reason,
            now_ns=self.clock.now_ns(),
        )
        self.policy.on_transfer_result(event)
        self._count(f"result_{reason.value}")
        self._emit(
            "transfer_result",
            job_id=event.job_id,
            prefix=prefix.short(),
            consideration_id=consideration_id,
            success=success,
            reason=reason.value,
        )

    def poll(self) -> list[TransferEvent]:
        for worker in self.workers.values():
            for prefix in worker.tick():
                self.state_change(StateChangeKind.CPU_READY, prefix)
        events = self.executor.poll()
        for ev in events:
            if ev.job_id in self.publications:
                self._on_publication_event(self.publications[ev.job_id], ev)
            elif ev.job_id in self.restores:
                self._on_restore_event(self.restores[ev.job_id], ev)
            elif ev.job_id in self.network_copies:
                self._on_network_event(self.network_copies[ev.job_id], ev)
        return events

    def _on_publication_event(self, pub: Publication, ev: TransferEvent) -> None:
        if ev.kind == TransferEventKind.STARTED:
            self.store.mark_writing(pub.reservation)
            return
        if ev.kind == TransferEventKind.COMPLETED:
            pub.completed_ns = ev.now_ns
            pub.terminal = ev
            return  # READY only after VISIBLE
        if ev.kind == TransferEventKind.VISIBLE:
            assert ev.proof is not None
            pub.visible_ns = ev.now_ns
            result = self.store.commit_ready(pub.reservation, ev.proof)
            self.workers[pub.source_worker].release(pub.lease.lease_id)
            del self.publications[pub.job_id]
            self.refresh_snapshot()
            if result.accepted:
                self._count("publication_ready")
                self.completed.append(
                    {
                        "kind": "publication",
                        "prefix": pub.prefix.short(),
                        "enqueued_ns": pub.enqueued_ns,
                        "completed_ns": pub.completed_ns,
                        "visible_ns": pub.visible_ns,
                        "bytes": pub.lease.nbytes,
                    }
                )
                self._report(
                    pub.prefix, pub.consideration_id, pub.job_id, True, Reason.PUBLISHED
                )
            else:
                self._count("publication_stale_completion_rejected")
                self._report(
                    pub.prefix,
                    pub.consideration_id,
                    pub.job_id,
                    False,
                    Reason.PUBLICATION_FAILED,
                )
            self.state_change(StateChangeKind.TRANSFER_COMPLETE, pub.prefix)
            return
        if ev.kind in (TransferEventKind.FAILED, TransferEventKind.CANCELLED):
            pub.terminal = ev
            self.store.abort(pub.reservation)
            self.store.settle(pub.reservation, ev)  # terminal == quiescence
            self.workers[pub.source_worker].release(pub.lease.lease_id)
            del self.publications[pub.job_id]
            self.refresh_snapshot()
            reason = (
                Reason.PUBLICATION_CANCELLED
                if ev.kind == TransferEventKind.CANCELLED
                else Reason.PUBLICATION_FAILED
            )
            self._report(pub.prefix, pub.consideration_id, pub.job_id, False, reason)
            self.state_change(StateChangeKind.TRANSFER_COMPLETE, pub.prefix)

    def _on_restore_event(self, restore: Restore, ev: TransferEvent) -> None:
        if ev.kind == TransferEventKind.STARTED:
            return
        if ev.kind == TransferEventKind.COMPLETED:
            assert ev.proof is not None
            ok = ev.proof.checksum == restore.lease.checksum
            self.store.release(restore.lease.lease_id)
            del self.restores[restore.job_id]
            if ok:
                self.workers[restore.destination_worker].on_import_complete(
                    restore.prefix, ev.proof.nbytes, ev.proof.checksum
                )
                self._count("restore_completed")
                self.completed.append(
                    {
                        "kind": "restore",
                        "request": restore.request.label(),
                        "prefix": restore.prefix.short(),
                        "enqueued_ns": restore.enqueued_ns,
                        "completed_ns": ev.now_ns,
                        "bytes": ev.proof.nbytes,
                    }
                )
            else:
                self.correctness_failures.append(
                    {"kind": "checksum_mismatch", "request": restore.request.label()}
                )
                self._report(
                    restore.prefix,
                    None,
                    restore.job_id,
                    False,
                    Reason.CHECKSUM_MISMATCH,
                )
            return
        if ev.kind in (TransferEventKind.FAILED, TransferEventKind.CANCELLED):
            self.store.release(restore.lease.lease_id)
            del self.restores[restore.job_id]
            self.correctness_failures.append(
                {
                    "kind": "import_failed",
                    "request": restore.request.label(),
                    "error": ev.error,
                }
            )
            self._report(
                restore.prefix, None, restore.job_id, False, Reason.IMPORT_FAILED
            )

    def _on_network_event(self, copy: NetworkCopy, ev: TransferEvent) -> None:
        if ev.kind == TransferEventKind.STARTED:
            return
        if ev.kind == TransferEventKind.VISIBLE:
            return
        self.workers[copy.source_worker].release(copy.lease.lease_id)
        del self.network_copies[copy.job_id]
        if ev.kind == TransferEventKind.COMPLETED:
            assert ev.proof is not None
            self.workers[copy.destination_worker].on_import_complete(
                copy.prefix, ev.proof.nbytes, ev.proof.checksum
            )
            self._count("network_copy_completed")
            self.completed.append(
                {
                    "kind": "network_copy",
                    "request": copy.request.label(),
                    "prefix": copy.prefix.short(),
                    "enqueued_ns": copy.enqueued_ns,
                    "completed_ns": ev.now_ns,
                    "bytes": ev.proof.nbytes,
                }
            )
        else:
            self._count("network_copy_failed")
            self._report(
                copy.prefix, None, copy.job_id, False, Reason.NETWORK_COPY_FAILED
            )

    # -- accounting for invariant checks -------------------------------------

    def accounting(self) -> dict[str, int]:
        usage = self.store.usage()
        return {
            "source_leases": sum(w.lease_count() for w in self.workers.values()),
            "reader_leases": usage.active_reader_leases,
            "writer_slots": usage.active_writer_slots,
            "active_jobs": self.executor.transport.active_jobs(),
            "publications_open": len(self.publications),
            "restores_open": len(self.restores),
            "network_copies_open": len(self.network_copies),
        }

    def quiescent(self) -> bool:
        return all(v == 0 for v in self.accounting().values())


def decision_fields(decision: OffloadDecision | DemandDecision) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "action": decision.action.value,
        "reason": decision.reason.value,
        "prefix": decision.prefix.short(),
        "snapshot_id": decision.snapshot_id,
        "decided_at_ns": decision.decided_at_ns,
    }
    if isinstance(decision, OffloadDecision):
        fields.update(
            consideration_id=decision.consideration_id,
            deadline_ns=decision.deadline_ns,
            estimate_valid=decision.estimate_valid,
            calibration_id=decision.calibration_id,
            shadow_failed=decision.shadow_failed,
        )
        if decision.score is not None:
            fields["score"] = {
                "weighted_benefit_ms": decision.score.weighted_benefit_ms,
                "publication_charge_ms": decision.score.publication_charge_ms,
                "threshold_ms": decision.score.threshold_ms,
                "eligible": decision.score.eligible,
                "remote_reuse_weight": decision.score.remote_reuse_weight,
                "margin": decision.score.margin,
            }
    else:
        fields.update(
            request=decision.request.label(),
            destination_worker=decision.destination_worker,
            source_worker=decision.source_worker,
            shadow_failed=decision.shadow_failed,
        )
    return fields
