# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""GPU-free simulation: fake backend + coordinator + replay runner (spec §8).

Every duration here is a mock sequencing constant. The simulation exists to
check lifecycle, no-oracle, baseline invariance and error cleanup; its
latencies are never reported as measured TTFT or transfer performance.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from typing import IO, Any

from .keys import GIB, IdSource, LayoutDescriptor, PrefixKey, RequestKey, RunKey
from .kv_transfer_adapter import (
    DispatchReceipt,
    ExecutorLimits,
    FakeClock,
    FakeRoutingAdapter,
    FakeServiceTimes,
    FakeSharedStore,
    FakeTransport,
    FakeWorkerKV,
    MigrationCoordinator,
    OffsetMapper,
    StoreCapacity,
    TransferExecutor,
)
from .offload_policy import (
    CalibrationPoint,
    CalibrationTable,
    DemandAction,
    DemandDecision,
    OffloadPolicy,
    PolicyConfig,
    PValuePolicy,
    ShadowedPolicy,
    StateChangeKind,
    make_policy,
)
from .session_replay import (
    ChunkKind,
    ReplayRequest,
    SessionReplayRunner,
    StreamChunk,
    TraceWriter,
)
from .session_workload import Manifest, WorkloadConfig, build_manifest

MS = 1_000_000


@dataclass(frozen=True)
class SimTiming:
    """Mock-only sequencing constants (not measurements)."""

    gpu_pool_tokens: int = 97_552
    cpu_pool_tokens: int = (32 * GIB) // (160 * 1024)
    prefill_ns_per_token: int = 110_000  # ~900 ms for 8192 tokens
    local_restore_ns_per_token: int = 6_000
    decode_ns_per_token: int = 20 * MS
    role_chunk_delay_ns: int = 1 * MS
    gpu_to_cpu_offload_ns: int = 60 * MS
    step_ns: int = 5 * MS
    timer_ns: int = 100 * MS
    snapshot_refresh_ns: int = 100 * MS


MOCK_CALIBRATION = CalibrationTable(
    calibration_id="mock-walkthrough-v0",
    source="mock",
    points=(
        CalibrationPoint(4096, 95.0, 450.0, 45.0, 20.0),
        CalibrationPoint(8192, 180.0, 900.0, 80.0, 35.0),
        CalibrationPoint(12288, 265.0, 1350.0, 115.0, 50.0),
    ),
)


class _Phase:
    WAIT_TRANSFER = "WAIT_TRANSFER"
    PREFILL_QUEUED = "PREFILL_QUEUED"
    PREFILL = "PREFILL"
    DECODE = "DECODE"
    DONE = "DONE"


@dataclass
class _Sim:
    request: ReplayRequest
    worker: str
    decision: DemandDecision
    phase: str
    prefill_tokens: int
    reuse_path: str
    fallback_reason: str | None = None
    first_token_ns: int | None = None
    done_ns: int | None = None
    role_emitted: bool = False
    usage_emitted: bool = False
    job_key: str | None = None


@dataclass
class _WorkerSim:
    prefill_free_at_ns: int = 0
    gpu_lru: OrderedDict[PrefixKey, int] = field(default_factory=OrderedDict)
    cpu_lru: OrderedDict[PrefixKey, int] = field(default_factory=OrderedDict)


class SimulatedBackend:
    def __init__(
        self,
        clock: FakeClock,
        coordinator: MigrationCoordinator,
        routing: FakeRoutingAdapter,
        timing: SimTiming,
    ):
        self.clock = clock
        self.coord = coordinator
        self.routing = routing
        self.timing = timing
        self.workers = {w: _WorkerSim() for w in coordinator.workers}
        self.sims: dict[RequestKey, _Sim] = {}
        self.reuse_paths: list[dict[str, Any]] = []
        self.cpu_evictions_refused = 0

    # -- residency bookkeeping (fake LRU tiers) ---------------------------

    def _touch_gpu(self, worker: str, prefix: PrefixKey, tokens: int) -> None:
        ws = self.workers[worker]
        kv = self.coord.workers[worker]
        ws.gpu_lru[prefix] = tokens
        ws.gpu_lru.move_to_end(prefix)
        kv.mark_gpu_resident(prefix)
        while (
            sum(ws.gpu_lru.values()) > self.timing.gpu_pool_tokens
            and len(ws.gpu_lru) > 1
        ):
            victim, _ = ws.gpu_lru.popitem(last=False)
            kv.evict_gpu(victim)

    def _offload_to_cpu(
        self, worker: str, prefix: PrefixKey, ready_after_ns: int
    ) -> None:
        ws = self.workers[worker]
        kv = self.coord.workers[worker]
        if kv.cpu_state(prefix).value == "READY":
            # already READY locally (earlier turn or an import); refresh recency
            ws.cpu_lru[prefix] = prefix.complete_tokens
            ws.cpu_lru.move_to_end(prefix)
            return
        kv.begin_offload(prefix, ready_after_ns)
        ws.cpu_lru[prefix] = prefix.complete_tokens
        ws.cpu_lru.move_to_end(prefix)
        while (
            sum(ws.cpu_lru.values()) > self.timing.cpu_pool_tokens
            and len(ws.cpu_lru) > 1
        ):
            victim = next(iter(ws.cpu_lru))
            if victim == prefix:
                break
            if kv.evict_cpu(victim):
                del ws.cpu_lru[victim]
                self.coord.state_change(StateChangeKind.SOURCE_EVICTED, victim)
            else:
                self.cpu_evictions_refused += 1
                ws.cpu_lru.move_to_end(victim)  # pinned by an export lease; retry later
                break

    # -- ReplayBackend -------------------------------------------------------

    def submit(self, request: ReplayRequest, destination: str) -> DispatchReceipt:
        receipt = self.routing.dispatch(request.key, destination)
        worker = receipt.actual_worker
        self.coord.state_change(StateChangeKind.NEW_REQUEST, request.prefix)
        decision = self.coord.request(request.key, request.prefix, worker)
        kv = self.coord.workers[worker]
        prompt = request.turn.prompt_tokens
        prefix_tokens = request.prefix.complete_tokens
        suffix = prompt - prefix_tokens
        if decision.action == DemandAction.LOCAL:
            if request.prefix in kv.gpu_resident:
                sim = _Sim(
                    request,
                    worker,
                    decision,
                    _Phase.PREFILL_QUEUED,
                    suffix,
                    "gpu_local",
                )
            else:
                restore = prefix_tokens * self.timing.local_restore_ns_per_token
                sim = _Sim(
                    request,
                    worker,
                    decision,
                    _Phase.PREFILL_QUEUED,
                    suffix,
                    "cpu_local",
                )
                self.workers[worker].prefill_free_at_ns = (
                    max(self.workers[worker].prefill_free_at_ns, self.clock.now_ns())
                    + restore
                )
        elif decision.action in (DemandAction.CXL_RESTORE, DemandAction.NETWORK_COPY):
            table = (
                self.coord.restores
                if decision.action == DemandAction.CXL_RESTORE
                else self.coord.network_copies
            )
            job = next((j for j in table.values() if j.request == request.key), None)
            if job is None:
                # acquire missed after the decision: plain recompute, flagged.
                sim = _Sim(
                    request,
                    worker,
                    decision,
                    _Phase.PREFILL_QUEUED,
                    prompt,
                    "recompute",
                    fallback_reason="acquire_miss_after_decision",
                )
            else:
                path = (
                    "cxl_import"
                    if decision.action == DemandAction.CXL_RESTORE
                    else "network_import"
                )
                sim = _Sim(
                    request, worker, decision, _Phase.WAIT_TRANSFER, suffix, path
                )
                sim.job_key = request.key.label()
        else:
            sim = _Sim(
                request, worker, decision, _Phase.PREFILL_QUEUED, prompt, "recompute"
            )
        self.sims[request.key] = sim
        return receipt

    def _transfer_outcome(self, sim: _Sim) -> str | None:
        label = sim.request.key.label()
        for entry in self.coord.completed:
            if entry.get("request") == label:
                return "ok"
        for failure in self.coord.correctness_failures:
            if failure.get("request") == label:
                return failure["kind"]
        for k in ("restores", "network_copies"):
            if any(
                j.request == sim.request.key for j in getattr(self.coord, k).values()
            ):
                return None
        return "job_lost"

    def poll(self) -> list[tuple[RequestKey, StreamChunk]]:
        self.coord.poll()
        now = self.clock.now_ns()
        out: list[tuple[RequestKey, StreamChunk]] = []
        for key, sim in list(self.sims.items()):
            ws = self.workers[sim.worker]
            if not sim.role_emitted:
                sim.role_emitted = True
                out.append(
                    (key, StreamChunk(ChunkKind.ROLE, now, worker_id=sim.worker))
                )
            if sim.phase == _Phase.WAIT_TRANSFER:
                outcome = self._transfer_outcome(sim)
                if outcome is None:
                    continue
                if outcome != "ok":
                    sim.fallback_reason = outcome
                    sim.prefill_tokens = sim.request.turn.prompt_tokens
                    sim.reuse_path = "recompute"
                sim.phase = _Phase.PREFILL_QUEUED
            if sim.phase == _Phase.PREFILL_QUEUED:
                start = max(now, ws.prefill_free_at_ns)
                sim.first_token_ns = (
                    start + sim.prefill_tokens * self.timing.prefill_ns_per_token
                )
                ws.prefill_free_at_ns = sim.first_token_ns
                sim.phase = _Phase.PREFILL
            if (
                sim.phase == _Phase.PREFILL
                and sim.first_token_ns is not None
                and now >= sim.first_token_ns
            ):
                marker = sim.request.turn.expected_marker
                out.append(
                    (
                        key,
                        StreamChunk(
                            ChunkKind.CONTENT,
                            sim.first_token_ns,
                            text=marker,
                            worker_id=sim.worker,
                        ),
                    )
                )
                sim.done_ns = (
                    sim.first_token_ns
                    + sim.request.max_output_tokens * self.timing.decode_ns_per_token
                )
                sim.phase = _Phase.DECODE
                self._touch_gpu(
                    sim.worker, sim.request.prefix, sim.request.turn.prompt_tokens
                )
                self._offload_to_cpu(
                    sim.worker,
                    sim.request.prefix,
                    (sim.done_ns - now) + self.timing.gpu_to_cpu_offload_ns,
                )
                self.reuse_paths.append(
                    {
                        "request": key.label(),
                        "worker": sim.worker,
                        "path": sim.reuse_path,
                        "decision": sim.decision.action.value,
                        "reason": sim.decision.reason.value,
                        "fallback_reason": sim.fallback_reason,
                        "prefill_tokens": sim.prefill_tokens,
                    }
                )
            if (
                sim.phase == _Phase.DECODE
                and sim.done_ns is not None
                and now >= sim.done_ns
            ):
                usage = {
                    "prompt_tokens": sim.request.turn.prompt_tokens,
                    "completion_tokens": sim.request.max_output_tokens,
                    "cached_tokens": sim.request.turn.prompt_tokens
                    - sim.prefill_tokens,
                }
                out.append(
                    (key, StreamChunk(ChunkKind.USAGE, sim.done_ns, usage=usage))
                )
                out.append(
                    (
                        key,
                        StreamChunk(ChunkKind.DONE, sim.done_ns, worker_id=sim.worker),
                    )
                )
                sim.phase = _Phase.DONE
                del self.sims[key]
        return out


class CoordinatorHooks:
    def __init__(self, coordinator: MigrationCoordinator):
        self.coord = coordinator

    def on_dispatch(
        self, request: RequestKey, prefix: PrefixKey, destination: str
    ) -> None:
        return

    def on_response_done(
        self, request: RequestKey, prefix: PrefixKey, actual_worker: str | None
    ) -> None:
        if actual_worker is None or actual_worker == "unknown":
            return
        self.coord.turn_end(request, prefix, actual_worker)

    def on_session_closed(self, session_id: int, prefix: PrefixKey) -> None:
        self.coord.close_session(session_id, prefix)


@dataclass
class SimulationResult:
    policy_name: str
    manifest: Manifest
    runner: SessionReplayRunner
    coordinator: MigrationCoordinator
    backend: SimulatedBackend
    trace: TraceWriter
    policy: OffloadPolicy
    steps: int
    final_ns: int

    def traces(self) -> list[dict[str, Any]]:
        return [t.as_dict() for t in self.runner.traces.values()]


@dataclass
class SimulationSetup:
    clock: FakeClock
    ids: IdSource
    store: FakeSharedStore
    workers: dict[str, FakeWorkerKV]
    executor: TransferExecutor
    transport: FakeTransport
    coordinator: MigrationCoordinator
    policy: OffloadPolicy
    layout: LayoutDescriptor


def build_setup(
    policy_name: str,
    cfg: WorkloadConfig,
    *,
    calibration: CalibrationTable | None = MOCK_CALIBRATION,
    policy_config: PolicyConfig | None = None,
    limits: ExecutorLimits | None = None,
    capacity: StoreCapacity | None = None,
    service: FakeServiceTimes | None = None,
    shadow_pvalue: bool = False,
    trace: TraceWriter | None = None,
    start_ns: int = 0,
    policy: OffloadPolicy | None = None,
) -> SimulationSetup:
    clock = FakeClock(start_ns)
    ids = IdSource(prefix=f"{policy_name}-{cfg.seed}")
    limits = limits or ExecutorLimits()
    capacity = capacity or StoreCapacity(block_bytes=limits.chunk_bytes)
    layout = LayoutDescriptor(
        cfg.offload_block_tokens, cfg.bytes_per_token, cfg.model_key.layout_version
    )
    mapper = OffsetMapper(provider_maps_slice_relative=True)  # fake: relative == device
    store = FakeSharedStore(clock, ids, capacity, mapper, layout)
    workers = {w: FakeWorkerKV(w, clock, ids, layout, limits) for w in cfg.workers}
    transport = FakeTransport(clock)
    executor = TransferExecutor(transport, limits, clock)
    if policy is None:
        policy = make_policy(policy_name, policy_config)
        if shadow_pvalue:
            policy = ShadowedPolicy(
                policy, PValuePolicy(PolicyConfig(name="P_VALUE_shadow"))
            )
    coordinator = MigrationCoordinator(
        clock,
        ids,
        store,
        workers,
        executor,
        policy,
        cfg.model_key,
        calibration,
        service,
        trace,
    )
    return SimulationSetup(
        clock, ids, store, workers, executor, transport, coordinator, policy, layout
    )


def run_simulation(
    policy_name: str,
    cfg: WorkloadConfig,
    *,
    manifest: Manifest | None = None,
    timing: SimTiming | None = None,
    calibration: CalibrationTable | None = MOCK_CALIBRATION,
    policy_config: PolicyConfig | None = None,
    limits: ExecutorLimits | None = None,
    shadow_pvalue: bool = False,
    misroute: dict[str, str] | None = None,
    trace_stream: IO[str] | None = None,
    max_steps: int = 200_000,
    run_id: str = "sim",
    stop_at_turn: int | None = None,
    policy: OffloadPolicy | None = None,
) -> SimulationResult:
    """Run one cell GPU-free. ``stop_at_turn`` ends the run once every
    session has completed that many turns (used by the no-oracle mock)."""
    timing = timing or SimTiming()
    manifest = manifest or build_manifest(cfg)
    trace = TraceWriter(trace_stream)
    setup = build_setup(
        policy_name,
        cfg,
        calibration=calibration,
        policy_config=policy_config,
        limits=limits,
        shadow_pvalue=shadow_pvalue,
        trace=trace,
        policy=policy,
    )
    routing = FakeRoutingAdapter(setup.ids, misroute)
    backend = SimulatedBackend(setup.clock, setup.coordinator, routing, timing)
    run = RunKey(run_id, f"{cfg.scenario}/{policy_name}", cfg.seed)
    runner = SessionReplayRunner(
        manifest,
        run,
        backend,
        setup.clock,
        trace,
        hooks=CoordinatorHooks(setup.coordinator),
    )
    next_timer = timing.timer_ns
    next_snapshot = timing.snapshot_refresh_ns
    steps = 0
    while steps < max_steps:
        runner.step()
        now = setup.clock.now_ns()
        if now >= next_snapshot:
            setup.coordinator.refresh_snapshot()
            next_snapshot = now + timing.snapshot_refresh_ns
        if now >= next_timer:
            setup.coordinator.timer()
            next_timer = now + timing.timer_ns
        steps += 1
        if stop_at_turn is not None:
            if all(t >= stop_at_turn for t in runner.session_progress().values()):
                break
        elif (
            runner.done()
            and setup.coordinator.quiescent()
            and not setup.policy.pending_prefixes()
        ):
            break
        setup.clock.advance_ns(timing.step_ns)
    return SimulationResult(
        policy_name=policy_name,
        manifest=manifest,
        runner=runner,
        coordinator=setup.coordinator,
        backend=backend,
        trace=trace,
        policy=setup.policy,
        steps=steps,
        final_ns=setup.clock.now_ns(),
    )


def main(argv: list[str] | None = None) -> int:
    """Run the GPU-free first cells (F-STAY, F-GAP, D-DELAY) and print
    summaries. Output is mock-only sequencing, not performance."""
    import argparse
    import json
    import sys

    from .offload_policy import POLICY_NAMES
    from .summarize_migration_run import summarize_simulation

    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument("--sessions", type=int, default=32)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--gap-s", type=float, default=10.0)
    parser.add_argument("--scenarios", nargs="*", default=["STAY", "MOVE_GAP"])
    parser.add_argument("--policies", nargs="*", default=list(POLICY_NAMES))
    parser.add_argument("--trace-dir", default=None, help="write per-cell JSONL traces")
    args = parser.parse_args(argv)
    out: dict[str, Any] = {"mock_only": True, "cells": {}}
    for scenario in args.scenarios:
        cfg = WorkloadConfig(
            sessions=args.sessions,
            scenario=scenario,
            gap_s=args.gap_s,
            seed=args.seed,
            migrating_per_owner=max(1, args.sessions // 4),
        )
        manifest = build_manifest(cfg)
        for name in args.policies:
            stream = None
            if args.trace_dir:
                from pathlib import Path

                path = Path(args.trace_dir) / f"{scenario}_{name}_seed{args.seed}.jsonl"
                path.parent.mkdir(parents=True, exist_ok=True)
                stream = path.open("w", encoding="utf-8")
            try:
                result = run_simulation(
                    name, cfg, manifest=manifest, trace_stream=stream
                )
            finally:
                if stream is not None:
                    stream.close()
            out["cells"][f"{scenario}/{name}"] = summarize_simulation(result)
    json.dump(out, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
