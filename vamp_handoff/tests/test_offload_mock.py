# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""GPU-free mocks M1-M12 and the P_VALUE numeric walk-through (spec §7-§8).

Passing here is not a hardware capability pass. Every scenario injects the
condition it claims to check and verifies that leases, slots and jobs return
to their baseline afterwards.
"""

import math
import sys
import unittest
from pathlib import Path

HANDOFF = Path(__file__).resolve().parents[1]
if str(HANDOFF) not in sys.path:
    sys.path.insert(0, str(HANDOFF))

from vamp_cxl.keys import (  # noqa: E402
    GIB,
    ModelKey,
    PrefixKey,
    RequestKey,
    RunKey,
    block_hash_chain,
)
from vamp_cxl.kv_transfer_adapter import (  # noqa: E402
    REQUIRED_CAPABILITIES,
    CapabilityStatus,
    CompletionProof,
    DispatchReceipt,
    ExecutorLimits,
    FakeClock,
    Miss,
    OffsetError,
    OffsetMapper,
    ReserveStatus,
    StoreCapacity,
    TransferEventKind,
    TransferKind,
    audit_vllm_0_19_offload,
)
from vamp_cxl.offload_policy import (  # noqa: E402
    CalibrationPoint,
    CalibrationTable,
    CpuState,
    CxlState,
    DemandAction,
    OffloadAction,
    PolicyConfig,
    PolicyObservation,
    PValuePolicy,
    Reason,
    RequestEvent,
    ShadowedPolicy,
    StateChangeEvent,
    StateChangeKind,
    TransferResultEvent,
    TurnEndEvent,
    value_score,
)
from vamp_cxl.session_replay import (  # noqa: E402
    ChunkKind,
    SessionReplayRunner,
    StreamChunk,
    TraceWriter,
    classify_chunk,
)
from vamp_cxl.session_workload import (  # noqa: E402
    WorkloadConfig,
    build_manifest,
    manifest_digest,
)
from vamp_cxl.simulation import (  # noqa: E402
    MOCK_CALIBRATION,
    build_setup,
    run_simulation,
)
from vamp_cxl.summarize_migration_run import summarize_simulation  # noqa: E402

MS = 1_000_000
S = 1_000_000_000
MODEL = WorkloadConfig().model_key
SMALL = WorkloadConfig(sessions=2, seed=3)


def make_prefix(
    tokens: int = 8192, salt: str = "t", model: ModelKey = MODEL
) -> PrefixKey:
    ids = [(i * 7919 + len(salt)) % 1000 for i in range(tokens)]
    return PrefixKey(model, salt, block_hash_chain(ids, 32, salt), tokens)


def obs(**kw) -> PolicyObservation:
    base = dict(
        now_ns=0,
        snapshot_id="snap-0",
        snapshot_taken_ns=0,
        session_closed=False,
        model_matches=True,
        source_cpu_state=CpuState.READY,
        cxl_state=CxlState.ABSENT,
        cxl_generation=None,
        demand_reads_pending=0,
        calibration=MOCK_CALIBRATION.lookup(8192),
        calibration_id=MOCK_CALIBRATION.calibration_id,
    )
    base.update(kw)
    return PolicyObservation(**base)


def req(session: int = 0, turn: int = 0) -> RequestKey:
    return RequestKey(RunKey("r", "c", 0), session, turn, 0)


def advance_until(setup, predicate, step_ms: float = 1.0, limit_ms: float = 10_000.0):
    elapsed = 0.0
    while elapsed <= limit_ms:
        setup.coordinator.poll()
        if predicate():
            return True
        setup.clock.advance_ms(step_ms)
        elapsed += step_ms
    return False


class M1Manifest(unittest.TestCase):
    def test_manifest(self):
        cfg = WorkloadConfig(scenario="MOVE_GAP", seed=7)
        a = build_manifest(cfg)
        b = build_manifest(cfg)
        self.assertEqual(manifest_digest(a, True), manifest_digest(b, True))
        owners = {}
        for s in a.sessions.values():
            self.assertEqual(s.reusable_prefix_tokens, 8192)
            self.assertGreaterEqual(
                a.diagnostics["measured_lcp_by_session"][s.session_id], 8192
            )
            head = s.turns[0].prompt_token_ids[:8192]
            for t in s.turns:
                self.assertEqual(t.prompt_token_ids[:8192], head)
                self.assertLessEqual(
                    t.prompt_tokens + cfg.max_output_tokens, cfg.max_model_len
                )
            owners[s.initial_owner] = owners.get(s.initial_owner, 0) + 1
            self.assertEqual(len(s.prefix.hash_chain), 8192 // 32)
        self.assertEqual(owners, {"w0": 16, "w1": 16})
        self.assertEqual(len({s.prefix for s in a.sessions.values()}), 32)
        self.assertLess(a.cross_session_lcp_tokens, 64)
        self.assertEqual(a.total_reusable_tokens, 32 * 8192)
        self.assertGreater(a.total_prompt_tokens, a.total_reusable_tokens)
        moving = [g for g in a.ground_truth.values() if g.migrates]
        self.assertEqual(len(moving), 16)
        per_owner = {}
        for g in moving:
            per_owner[a.sessions[g.session_id].initial_owner] = (
                per_owner.get(a.sessions[g.session_id].initial_owner, 0) + 1
            )
        self.assertEqual(per_owner, {"w0": 8, "w1": 8})
        for g in a.ground_truth.values():
            owner = a.sessions[g.session_id].initial_owner
            self.assertEqual(g.destination_by_turn[:2], (owner, owner))
            if g.migrates:
                self.assertNotEqual(g.destination_by_turn[2], owner)
                self.assertEqual(g.destination_by_turn[3], g.destination_by_turn[2])
        self.assertNotIn("ground_truth", a.public_view())
        self.assertNotIn("destination", str(a.public_view()))

    def test_mixed_length_arm_and_validation(self):
        lengths = (4096,) * 8 + (8192,) * 16 + (12288,) * 8
        cfg = WorkloadConfig(prefix_tokens_per_session=lengths, seed=1)
        m = build_manifest(cfg)
        self.assertEqual(m.total_reusable_tokens, 32 * 8192)
        with self.assertRaises(ValueError):
            WorkloadConfig(reusable_prefix_tokens=8100)
        with self.assertRaises(ValueError):
            build_manifest(WorkloadConfig(sessions=1, max_model_len=8192))


class M2Publication(unittest.TestCase):
    def test_ready_only_after_cpu_ready_and_visibility(self):
        setup = build_setup("B2_EAGER", SMALL)
        c, w0, store = setup.coordinator, setup.workers["w0"], setup.store
        p = make_prefix()
        w0.begin_offload(p, ready_after_ns=50 * MS)
        d = c.turn_end(req(), p, "w0")
        self.assertEqual(
            (d.action, d.reason), (OffloadAction.DEFER, Reason.WAIT_SOURCE_READY)
        )
        setup.clock.advance_ms(20)
        c.poll()
        self.assertEqual(store.state_of(p)[0], CxlState.ABSENT)
        self.assertEqual(c.accounting()["source_leases"], 0)  # no pin while waiting
        self.assertTrue(
            advance_until(setup, lambda: store.state_of(p)[0] != CxlState.ABSENT)
        )
        self.assertIn(store.state_of(p)[0], (CxlState.RESERVED, CxlState.WRITING))
        self.assertIsInstance(store.acquire_ready(p, "w1"), Miss)
        # completed but not yet visible -> still not READY
        pub = next(iter(c.publications.values()))
        self.assertTrue(advance_until(setup, lambda: pub.completed_ns is not None))
        self.assertEqual(store.state_of(p)[0], CxlState.WRITING)
        self.assertIsInstance(store.acquire_ready(p, "w1"), Miss)
        self.assertTrue(
            advance_until(setup, lambda: store.state_of(p)[0] == CxlState.READY)
        )
        grant = store.acquire_ready(p, "w1")
        self.assertFalse(isinstance(grant, Miss))
        store.release(grant.lease_id)
        self.assertTrue(c.quiescent())
        self.assertEqual(c.counters["publication_ready"], 1)
        results = [
            r for r in setup.policy.transfer_results if r.reason == Reason.PUBLISHED
        ]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].consideration_id, d.consideration_id)


class M3Eviction(unittest.TestCase):
    def test_lookup_then_evict_is_explicit_miss(self):
        setup = build_setup("B2_EAGER", SMALL)
        w0 = setup.workers["w0"]
        p = make_prefix()
        w0.begin_offload(p, 0)
        w0.tick()
        self.assertEqual(w0.cpu_state(p), CpuState.READY)
        self.assertTrue(w0.evict_cpu(p))
        self.assertIsInstance(w0.acquire_ready_prefix(p, 0), Miss)
        lease = None
        w0.begin_offload(p, 0)
        w0.tick()
        lease = w0.acquire_ready_prefix(p, 0)
        self.assertFalse(isinstance(lease, Miss))
        self.assertFalse(w0.evict_cpu(p))  # pinned copies are never evicted under us
        w0.release(lease.lease_id)
        self.assertTrue(w0.evict_cpu(p))

    def test_defer_does_not_pin_and_eviction_skips(self):
        cfg = PolicyConfig(name="P_DELAY")
        setup = build_setup("P_DELAY", SMALL, policy_config=cfg)
        c, w0 = setup.coordinator, setup.workers["w0"]
        p = make_prefix()
        w0.begin_offload(p, 0)
        w0.tick()
        # demand read pressure: a queued CXL_READ makes P_DELAY defer
        other = make_prefix(salt="o")
        w0.begin_offload(other, 0)
        w0.tick()
        c.turn_end(req(1), other, "w0")
        advance_until(setup, lambda: setup.store.state_of(other)[0] == CxlState.READY)
        c.refresh_snapshot()
        d0 = c.request(req(1, 1), other, "w1")
        self.assertEqual(d0.action, DemandAction.CXL_RESTORE)
        d = c.turn_end(req(), p, "w0")
        self.assertEqual(
            (d.action, d.reason), (OffloadAction.DEFER, Reason.DEFER_READ_PRESSURE)
        )
        self.assertEqual(w0.lease_count(), 0)
        self.assertTrue(w0.evict_cpu(p))
        decisions = c.state_change(StateChangeKind.SOURCE_EVICTED, p)
        self.assertEqual([x.reason for x in decisions], [Reason.SKIP_SOURCE_EVICTED])
        self.assertEqual(
            [
                j.kind
                for j in setup.transport.jobs.values()
                if j.kind == TransferKind.CXL_WRITE
                and j.source == "w0"
                and j.checksum == w0.cpu[p].checksum
            ],
            [],
        )
        self.assertTrue(advance_until(setup, c.quiescent))


class M4ReaderRace(unittest.TestCase):
    def test_no_slot_reuse_while_reader_active(self):
        limits = ExecutorLimits()
        cap = StoreCapacity(
            nominal_bytes=limits.chunk_bytes * 4,
            metadata_reserve_bytes=0,
            block_bytes=limits.chunk_bytes,
        )
        setup = build_setup("B2_EAGER", SMALL, capacity=cap)
        c, w0, store = setup.coordinator, setup.workers["w0"], setup.store
        p = make_prefix(tokens=64)  # 2 blocks
        w0.begin_offload(p, 0)
        w0.tick()
        c.turn_end(req(), p, "w0")
        self.assertTrue(
            advance_until(setup, lambda: store.state_of(p)[0] == CxlState.READY)
        )
        grant = store.acquire_ready(p, "w1")
        self.assertFalse(isinstance(grant, Miss))
        first_offset = grant.payload_ref.relative_offset
        self.assertEqual(store.request_evict(p), "DEFERRED_READERS_ACTIVE")
        self.assertEqual(store.state_of(p)[0], CxlState.EVICTING)
        self.assertIsInstance(store.acquire_ready(p, "w1"), Miss)  # no new readers
        q = make_prefix(
            tokens=96, salt="q"
        )  # 3 blocks: cannot fit while p holds 2 of 4
        self.assertEqual(
            store.reserve(q, q.complete_tokens * 160 * 1024, "w0").status,
            ReserveStatus.REJECTED_CAPACITY,
        )
        store.release(grant.lease_id)
        self.assertEqual(store.state_of(p)[0], CxlState.ABSENT)
        res = store.reserve(q, q.complete_tokens * 160 * 1024, "w0")
        self.assertEqual(res.status, ReserveStatus.RESERVED)
        self.assertEqual(res.reservation.relative_offset, first_offset)
        self.assertGreater(res.reservation.generation, grant.payload_ref.generation)


class M5DuplicateAndABA(unittest.TestCase):
    def test_single_writer_and_stale_completion(self):
        setup = build_setup("B2_EAGER", SMALL)
        store = setup.store
        p = make_prefix(tokens=64)
        nbytes = 64 * 160 * 1024
        r1 = store.reserve(p, nbytes, "w0")
        self.assertEqual(r1.status, ReserveStatus.RESERVED)
        self.assertEqual(
            store.reserve(p, nbytes, "w1").status, ReserveStatus.ALREADY_WRITING
        )
        store.mark_writing(r1.reservation)
        # writer 1 fails; slot is freed only after the terminal event
        store.abort(r1.reservation)
        self.assertEqual(store.state_of(p)[0], CxlState.ABORTING)
        self.assertEqual(store.reserve(p, nbytes, "w1").status, ReserveStatus.BUSY)
        from vamp_cxl.kv_transfer_adapter import TransferEvent

        terminal = TransferEvent(
            TransferEventKind.FAILED, setup.ids.job(), 0, error="x"
        )
        store.settle(r1.reservation, terminal)
        self.assertEqual(store.state_of(p)[0], CxlState.ABSENT)
        r2 = store.reserve(p, nbytes, "w1")
        self.assertEqual(r2.status, ReserveStatus.RESERVED)
        self.assertEqual(r2.reservation.relative_offset, r1.reservation.relative_offset)
        store.mark_writing(r2.reservation)
        stale = CompletionProof(
            setup.ids.job(),
            r1.reservation.allocator_id,
            r1.reservation.generation,
            r1.reservation.reservation_id,
            "deadbeef",
            nbytes,
        )
        self.assertFalse(store.commit_ready(r2.reservation, stale).accepted)
        self.assertFalse(store.commit_ready(r1.reservation, stale).accepted)
        self.assertEqual(
            store.state_of(p), (CxlState.WRITING, r2.reservation.generation)
        )
        good = CompletionProof(
            setup.ids.job(),
            r2.reservation.allocator_id,
            r2.reservation.generation,
            r2.reservation.reservation_id,
            "cafe",
            nbytes,
        )
        self.assertTrue(store.commit_ready(r2.reservation, good).accepted)
        self.assertEqual(store.state_of(p)[0], CxlState.READY)
        # wrong allocator id is also rejected
        bad_alloc = CompletionProof(
            setup.ids.job(),
            "other-alloc",
            r2.reservation.generation,
            r2.reservation.reservation_id,
            "cafe",
            nbytes,
        )
        self.assertFalse(store.commit_ready(r2.reservation, bad_alloc).accepted)

    def test_coordinator_dedups_concurrent_turn_ends(self):
        setup = build_setup("B2_EAGER", SMALL)
        c = setup.coordinator
        p = make_prefix()
        for w in ("w0", "w1"):
            setup.workers[w].begin_offload(p, 0)
            setup.workers[w].tick()
        d0 = c.turn_end(req(0), p, "w0")
        c.refresh_snapshot()
        d1 = c.turn_end(req(1), p, "w1")
        self.assertEqual(d0.action, OffloadAction.STORE_NOW)
        self.assertEqual(
            (d1.action, d1.reason), (OffloadAction.SKIP, Reason.SKIP_ALREADY_WRITING)
        )
        self.assertEqual(sum(1 for j in setup.transport.jobs.values()), 1)
        self.assertTrue(advance_until(setup, c.quiescent))
        self.assertEqual(c.counters["publication_ready"], 1)


class M6CancelAndError(unittest.TestCase):
    def _start_publication(self, setup, p):
        w0 = setup.workers["w0"]
        w0.begin_offload(p, 0)
        w0.tick()
        d = setup.coordinator.turn_end(req(), p, "w0")
        self.assertEqual(d.action, OffloadAction.STORE_NOW)
        return next(iter(setup.coordinator.publications.values()))

    def _terminal_events(self, trace_events, job_id):
        return [
            e
            for e in trace_events
            if e["event"] == "transfer_result" and e.get("job_id") == job_id.value
        ]

    def test_write_failure_cleans_up(self):
        trace = TraceWriter()
        setup = build_setup("B2_EAGER", SMALL, trace=trace)
        c, store = setup.coordinator, setup.store
        p = make_prefix()
        pub = self._start_publication(setup, p)
        setup.transport.inject_failure(pub.job_id, at_chunk=3)
        self.assertTrue(advance_until(setup, lambda: pub.job_id not in c.publications))
        self.assertEqual(store.state_of(p)[0], CxlState.ABSENT)
        self.assertEqual(c.accounting()["source_leases"], 0)
        job = setup.transport.jobs[pub.job_id]
        self.assertTrue(job.terminal_emitted)
        self.assertEqual(len(self._terminal_events(trace.events, pub.job_id)), 1)
        self.assertEqual(
            setup.policy.transfer_results[-1].reason, Reason.PUBLICATION_FAILED
        )
        self.assertTrue(c.quiescent())
        self.assertEqual(setup.policy.pending_prefixes(), [])  # no silent retry

    def test_cancel_waits_for_quiescence(self):
        setup = build_setup("B2_EAGER", SMALL)
        c, store = setup.coordinator, setup.store
        p = make_prefix()
        pub = self._start_publication(setup, p)
        advance_until(
            setup, lambda: setup.transport.jobs[pub.job_id].started_ns is not None
        )
        setup.clock.advance_ms(0.5)
        c.poll()
        self.assertTrue(c.cancel_publication(p))
        job = setup.transport.jobs[pub.job_id]
        # cancellation requested mid-chunk: slot still held, lease still pinned
        self.assertFalse(job.terminal_emitted)
        self.assertEqual(store.state_of(p)[0], CxlState.WRITING)
        self.assertEqual(c.accounting()["source_leases"], 1)
        self.assertTrue(advance_until(setup, lambda: job.terminal_emitted))
        self.assertEqual(job.state.value, "CANCELLED")
        self.assertEqual(store.state_of(p)[0], CxlState.ABSENT)
        self.assertTrue(c.quiescent())
        self.assertEqual(
            setup.policy.transfer_results[-1].reason, Reason.PUBLICATION_CANCELLED
        )

    def test_read_failure_releases_reader_lease(self):
        setup = build_setup("B2_EAGER", SMALL)
        c, store = setup.coordinator, setup.store
        p = make_prefix()
        self._start_publication(setup, p)
        self.assertTrue(
            advance_until(setup, lambda: store.state_of(p)[0] == CxlState.READY)
        )
        c.refresh_snapshot()
        d = c.request(req(0, 2), p, "w1")
        self.assertEqual(d.action, DemandAction.CXL_RESTORE)
        restore = next(iter(c.restores.values()))
        self.assertEqual(store.usage().active_reader_leases, 1)
        setup.transport.inject_failure(restore.job_id, at_chunk=2)
        self.assertTrue(advance_until(setup, lambda: not c.restores))
        self.assertEqual(store.usage().active_reader_leases, 0)
        self.assertEqual(store.state_of(p)[0], CxlState.READY)  # payload untouched
        self.assertEqual(c.correctness_failures[-1]["kind"], "import_failed")
        self.assertFalse(setup.workers["w1"].local_ready(p))  # no fake restore
        self.assertTrue(c.quiescent())


class M7Policies(unittest.TestCase):
    def _cons(self, policy, now_ns=0, prefix=None):
        p = prefix or make_prefix()
        ev = TurnEndEvent(req(), p, now_ns, "cons-1", "w0")
        return p, ev

    def test_numeric_walkthrough(self):
        point = CalibrationPoint(8192, 180.0, 900.0, 80.0, 35.0)
        s = value_score(point, 0.5, 0.10)
        self.assertAlmostEqual(s.weighted_benefit_ms, 50.0)
        self.assertAlmostEqual(s.threshold_ms, 38.5)
        self.assertTrue(s.eligible)
        expensive = value_score(
            CalibrationPoint(8192, 180.0, 900.0, 80.0, 200.0), 0.5, 0.10
        )
        self.assertAlmostEqual(expensive.threshold_ms, 220.0)
        self.assertFalse(expensive.eligible)
        bad = value_score(CalibrationPoint(8192, math.nan, 900.0, 80.0, 35.0), 0.5, 0.1)
        self.assertFalse(bad.valid)

        policy = PValuePolicy(PolicyConfig(name="P_VALUE"))
        p, ev = self._cons(policy)
        d = policy.on_turn_end(ev, obs(calibration=point))
        self.assertEqual(
            (d.action, d.reason), (OffloadAction.STORE_NOW, Reason.STORE_NOW)
        )
        self.assertTrue(d.estimate_valid)
        self.assertEqual(d.deadline_ns, 5 * S)

        policy = PValuePolicy(PolicyConfig(name="P_VALUE"))
        p, ev = self._cons(policy)
        d = policy.on_turn_end(ev, obs(calibration=point, demand_reads_pending=1))
        self.assertEqual(
            (d.action, d.reason), (OffloadAction.DEFER, Reason.DEFER_READ_PRESSURE)
        )
        self.assertEqual(policy.pending_prefixes(), [p])

        policy = PValuePolicy(PolicyConfig(name="P_VALUE"))
        p, ev = self._cons(policy)
        d = policy.on_turn_end(
            ev, obs(calibration=CalibrationPoint(8192, 180.0, 900.0, 80.0, 200.0))
        )
        self.assertEqual(
            (d.action, d.reason), (OffloadAction.SKIP, Reason.SKIP_LOW_VALUE)
        )
        self.assertEqual(policy.pending_prefixes(), [])

        # defer then source evicted -> SKIP_SOURCE_EVICTED
        policy = PValuePolicy(PolicyConfig(name="P_VALUE"))
        p, ev = self._cons(policy)
        policy.on_turn_end(ev, obs(calibration=point, demand_reads_pending=1))
        d = policy.on_state_change(
            StateChangeEvent(StateChangeKind.SOURCE_EVICTED, 100 * MS, p),
            obs(calibration=point, now_ns=100 * MS, source_cpu_state=CpuState.EVICTED),
        )[0]
        self.assertEqual(d.reason, Reason.SKIP_SOURCE_EVICTED)

        # request while WRITING -> RECOMPUTE_NOT_READY (no partial payload)
        d = policy.on_request(
            RequestEvent(req(0, 2), p, "w1", 0),
            obs(cxl_state=CxlState.WRITING, cxl_generation=3),
        )
        self.assertEqual(
            (d.action, d.reason), (DemandAction.RECOMPUTE, Reason.RECOMPUTE_NOT_READY)
        )
        d = policy.on_request(
            RequestEvent(req(0, 2), p, "w1", 0),
            obs(cxl_state=CxlState.READY, cxl_generation=3),
        )
        self.assertEqual(
            (d.action, d.reason, d.cxl_generation),
            (DemandAction.CXL_RESTORE, Reason.CXL_READY, 3),
        )

    def test_order_stale_timeout_and_calibration(self):
        point = CalibrationPoint(8192, 180.0, 900.0, 80.0, 35.0)
        policy = PValuePolicy(PolicyConfig(name="P_VALUE"))
        p, ev = self._cons(policy, now_ns=10 * S)
        d = policy.on_turn_end(
            ev,
            obs(calibration=point, now_ns=10 * S + 1500 * MS, snapshot_taken_ns=10 * S),
        )
        self.assertEqual(d.reason, Reason.DEFER_STALE)
        # timers re-evaluate with the same order; deadline is not restarted
        for t_ms in (100, 2000, 4900):
            d = policy.on_state_change(
                StateChangeEvent(StateChangeKind.TIMER, 10 * S + t_ms * MS, p),
                obs(
                    calibration=point,
                    now_ns=10 * S + t_ms * MS,
                    snapshot_taken_ns=10 * S + t_ms * MS - 1500 * MS,
                ),
            )[0]
            self.assertEqual(
                (d.action, d.reason), (OffloadAction.DEFER, Reason.DEFER_STALE)
            )
            self.assertEqual(d.deadline_ns, 15 * S)
        d = policy.on_state_change(
            StateChangeEvent(StateChangeKind.TIMER, 15 * S, p),
            obs(calibration=point, now_ns=15 * S, snapshot_taken_ns=15 * S),
        )[0]
        self.assertEqual(
            (d.action, d.reason), (OffloadAction.SKIP, Reason.SKIP_DEFER_TIMEOUT)
        )
        self.assertEqual(policy.pending_prefixes(), [])
        self.assertEqual(
            policy.on_state_change(
                StateChangeEvent(StateChangeKind.TIMER, 16 * S, p), obs()
            ),
            [],
        )

        policy = PValuePolicy(PolicyConfig(name="P_VALUE"))
        p, ev = self._cons(policy)
        self.assertEqual(
            policy.on_turn_end(ev, obs(calibration=None, calibration_id=None)).reason,
            Reason.SKIP_UNCALIBRATED,
        )
        p, ev = self._cons(policy)
        self.assertEqual(
            policy.on_turn_end(
                ev, obs(calibration=CalibrationPoint(8192, -1.0, 900.0, 80.0, 35.0))
            ).reason,
            Reason.SKIP_INVALID_CALIBRATION,
        )
        p, ev = self._cons(policy)
        self.assertEqual(
            policy.on_turn_end(ev, obs(session_closed=True)).reason, Reason.SKIP_CLOSED
        )
        p, ev = self._cons(policy)
        self.assertEqual(
            policy.on_turn_end(ev, obs(cxl_state=CxlState.READY)).reason,
            Reason.SKIP_ALREADY_READY,
        )
        p, ev = self._cons(policy)
        self.assertEqual(
            policy.on_turn_end(ev, obs(model_matches=False)).reason,
            Reason.FAIL_CLOSED_MODEL_MISMATCH,
        )
        # deadline applies even when the calibration would otherwise defer
        p, ev = self._cons(policy)
        d = policy.on_turn_end(
            ev,
            obs(
                calibration=point,
                now_ns=6 * S,
                snapshot_taken_ns=0,
                demand_reads_pending=1,
            ),
        )
        self.assertEqual(d.reason, Reason.SKIP_DEFER_TIMEOUT)

    def test_calibration_table_no_extrapolation(self):
        self.assertIsNone(MOCK_CALIBRATION.lookup(2048))
        self.assertIsNone(MOCK_CALIBRATION.lookup(16384))
        mid = MOCK_CALIBRATION.lookup(6144)
        self.assertAlmostEqual(mid.t_cxl_write_ms, 27.5)
        with self.assertRaises(ValueError):
            CalibrationTable(
                "x",
                "mock",
                (
                    CalibrationPoint(8192, 1, 1, 1, 1),
                    CalibrationPoint(4096, 1, 1, 1, 1),
                ),
            )


class M8Baselines(unittest.TestCase):
    def _keyed(self, result):
        return [
            (
                e["event"],
                e.get("request") or e.get("prefix"),
                e["action"],
                e["reason"],
                e.get("destination_worker"),
            )
            for e in result.trace.events
            if e["event"] in ("offload_decision", "demand_decision")
        ]

    def test_shadow_exception_never_changes_baseline(self):
        from vamp_cxl.offload_policy import make_policy

        cfg = WorkloadConfig(
            sessions=8, scenario="MOVE_GAP", gap_s=1.0, seed=5, migrating_per_owner=2
        )
        manifest = build_manifest(cfg)
        for name in ("B0_RECOMPUTE", "B1_NETWORK"):
            plain = run_simulation(name, cfg, manifest=manifest)

            class Exploding(PValuePolicy):
                def on_turn_end(self, event, observation):
                    raise RuntimeError("inject")

                def on_request(self, event, observation):
                    raise RuntimeError("inject")

                def on_state_change(self, event, observation):
                    raise RuntimeError("inject")

            shadowed = ShadowedPolicy(
                make_policy(name), Exploding(PolicyConfig(name="P_VALUE"))
            )
            with_shadow = run_simulation(name, cfg, manifest=manifest, policy=shadowed)
            self.assertEqual(self._keyed(plain), self._keyed(with_shadow))
            self.assertGreater(len(shadowed.shadow_errors), 0)
            flagged = [e for e in with_shadow.trace.events if e.get("shadow_failed")]
            self.assertEqual(len(flagged), len(self._keyed(plain)))
            self.assertEqual(
                [t.actual_worker for t in plain.runner.traces.values()],
                [t.actual_worker for t in with_shadow.runner.traces.values()],
            )
            self.assertTrue(with_shadow.coordinator.quiescent())


class M9NoOracle(unittest.TestCase):
    def test_same_history_same_decisions(self):
        cfg = WorkloadConfig(
            sessions=16, scenario="MOVE_GAP", gap_s=1.0, seed=11, migrating_per_owner=4
        )
        a = build_manifest(cfg)
        b = build_manifest(WorkloadConfig(**{**cfg.__dict__, "scenario": "STAY"}))
        # identical past: same public view; different future: ground truth differs
        self.assertEqual(manifest_digest(a, False), manifest_digest(b, False))
        self.assertNotEqual(manifest_digest(a, True), manifest_digest(b, True))
        ra = run_simulation("P_VALUE", cfg, manifest=a, stop_at_turn=3)
        rb = run_simulation("P_VALUE", cfg, manifest=b, stop_at_turn=3)

        def first_move_dispatch(result):
            return min(
                t.dispatch_ns
                for t in result.runner.traces.values()
                if t.request.turn_id == cfg.migration_turn and t.dispatch_ns is not None
            )

        cutoff = first_move_dispatch(ra)
        self.assertEqual(cutoff, first_move_dispatch(rb))

        def before(result):
            return [
                (
                    e["event"],
                    e.get("prefix"),
                    e["action"],
                    e["reason"],
                    e.get("destination_worker"),
                )
                for e in result.trace.events
                if e["event"] in ("offload_decision", "demand_decision")
                and e["decided_at_ns"] < cutoff
            ]

        self.assertGreater(len(before(ra)), 16)
        self.assertEqual(before(ra), before(rb))
        # after the cutoff the two runs legitimately diverge (destinations differ)
        moved = [
            t
            for t in ra.runner.traces.values()
            if t.request.turn_id == cfg.migration_turn
            and t.designated_worker != a.sessions[t.request.session_id].initial_owner
        ]
        self.assertEqual(len(moved), 8)
        self.assertEqual(
            sum(1 for t in rb.runner.traces.values() if not t.target_verified), 0
        )


class M10Capacity(unittest.TestCase):
    def test_budget_rounding_bounds_and_duplicates(self):
        limits = ExecutorLimits()
        blk = limits.chunk_bytes
        cap = StoreCapacity(
            nominal_bytes=10 * blk, metadata_reserve_bytes=blk + 1, block_bytes=blk
        )
        self.assertEqual(
            cap.payload_capacity_bytes, 8 * blk
        )  # reserve + rounding excluded
        setup = build_setup("B2_EAGER", SMALL, capacity=cap)
        store = setup.store
        p1, p2, p3 = (make_prefix(tokens=64, salt=s) for s in "abc")
        r1 = store.reserve(p1, blk + 1, "w0")  # rounds up to 2 blocks
        self.assertEqual(r1.reservation.rounded_bytes, 2 * blk)
        r2 = store.reserve(p2, 5 * blk, "w1")
        self.assertEqual(r2.status, ReserveStatus.RESERVED)
        self.assertEqual(store.usage().occupied_bytes, 7 * blk)  # two copies both count
        self.assertEqual(
            store.reserve(p3, 2 * blk, "w0").status, ReserveStatus.REJECTED_CAPACITY
        )
        self.assertEqual(store.reserve(p3, blk, "w0").status, ReserveStatus.RESERVED)
        self.assertEqual(store.usage().occupied_bytes, 8 * blk)
        mapper = OffsetMapper()
        with self.assertRaises(OffsetError):
            mapper.check_range(64 * GIB - 1, 2)
        with self.assertRaises(OffsetError):
            mapper.check_range(-1, 1)
        with self.assertRaises(OffsetError):
            mapper.device_offset(0, 1)  # origin unconfirmed -> refuse
        self.assertEqual(
            OffsetMapper(provider_maps_slice_relative=False).device_offset(5, 1),
            64 * GIB + 5,
        )
        self.assertEqual(
            OffsetMapper(provider_maps_slice_relative=True).device_offset(5, 1), 5
        )
        # a reader lease on p1 keeps its slot while other slots come and go
        store.mark_writing(r1.reservation)
        proof = CompletionProof(
            setup.ids.job(),
            r1.reservation.allocator_id,
            r1.reservation.generation,
            r1.reservation.reservation_id,
            "c",
            blk + 1,
        )
        self.assertTrue(store.commit_ready(r1.reservation, proof).accepted)
        grant = store.acquire_ready(p1, "w1")
        self.assertEqual(store.request_evict(p2), "REFUSED_WRITER_PROTECTED")
        self.assertEqual(store.request_evict(p1), "DEFERRED_READERS_ACTIVE")
        self.assertEqual(store.usage().occupied_bytes, 8 * blk)
        store.release(grant.lease_id)
        self.assertEqual(store.usage().occupied_bytes, 6 * blk)


class _ScriptedBackend:
    """Backend that replays scripted chunk timelines relative to dispatch."""

    def __init__(self, clock, scripts, misroute=None):
        self.clock = clock
        self.scripts = scripts
        self.misroute = misroute or {}
        self.pending = []
        self.n = 0

    def submit(self, request, destination):
        self.n += 1
        actual = self.misroute.get(destination, destination)
        script = self.scripts[(request.key.session_id, request.key.turn_id)]
        base = self.clock.now_ns()
        for offset_ms, chunk in script:
            self.pending.append((base + offset_ms * MS, request.key, chunk))
        return DispatchReceipt(request.key, destination, actual, f"r{self.n}")

    def poll(self):
        now = self.clock.now_ns()
        due = sorted((p for p in self.pending if p[0] <= now), key=lambda p: p[0])
        self.pending = [p for p in self.pending if p[0] > now]
        return [
            (
                k,
                StreamChunk(
                    c.kind, t, text=c.text, usage=c.usage, worker_id=c.worker_id
                ),
            )
            for t, k, c in due
        ]


class M11Runner(unittest.TestCase):
    def test_no_barrier_ttft_and_admission(self):
        cfg = WorkloadConfig(
            sessions=3,
            turns_per_session=2,
            gap_s=0.0,
            seed=2,
            max_outstanding_requests=2,
            workers=("w0", "w1"),
        )
        manifest = build_manifest(cfg)
        role = StreamChunk(ChunkKind.ROLE, 0, worker_id="w0")
        usage_only = StreamChunk(
            ChunkKind.USAGE,
            0,
            usage={"prompt_tokens": 1, "completion_tokens": 0, "cached_tokens": 0},
        )

        def content(sid, turn):
            return StreamChunk(ChunkKind.CONTENT, 0, text=f"S{sid}T{turn}_OK")

        done = StreamChunk(ChunkKind.DONE, 0)
        scripts = {}
        for sid in range(3):
            for turn in range(2):
                slow = sid == 0
                first = 900 if slow else 30
                scripts[(sid, turn)] = [
                    (1, role),
                    (5, usage_only),
                    (first, content(sid, turn)),
                    (first + 50, done),
                ]
        clock = FakeClock()
        backend = _ScriptedBackend(clock, scripts)
        runner = SessionReplayRunner(
            manifest, RunKey("r", "c", 0), backend, clock, TraceWriter()
        )
        while not runner.done():
            runner.step()
            clock.advance_ms(1)
        traces = runner.traces
        s0t0 = traces[RequestKey(RunKey("r", "c", 0), 0, 0, 0)]
        s1t1 = traces[RequestKey(RunKey("r", "c", 0), 1, 1, 0)]
        s2t0 = traces[RequestKey(RunKey("r", "c", 0), 2, 0, 0)]
        # session 1 finished its second turn while session 0 was still on turn 0
        self.assertLess(s1t1.response_done_ns, s0t0.response_done_ns)
        # TTFT from the first real token, not the role/usage-only chunks
        self.assertEqual(s0t0.ttft_content_ns, 900 * MS)
        self.assertEqual(s0t0.first_role_ns, 1 * MS)
        self.assertTrue(s0t0.usage_observed)
        self.assertTrue(all(t.marker_ok for t in traces.values()))
        # session 2 waited for a slot (max_outstanding=2) -> admission delay recorded
        self.assertGreater(s2t0.admission_delay_ns, 0)
        self.assertEqual(s2t0.ready_ns, 0)
        self.assertEqual(runner.invalid_target, [])

    def test_target_verification_and_chunk_classification(self):
        cfg = WorkloadConfig(sessions=1, turns_per_session=1, seed=2)
        manifest = build_manifest(cfg)
        clock = FakeClock()
        scripts = {
            (0, 0): [
                (1, StreamChunk(ChunkKind.CONTENT, 0, text="S0T0_OK")),
                (2, StreamChunk(ChunkKind.DONE, 0)),
            ]
        }
        backend = _ScriptedBackend(clock, scripts, misroute={"w0": "w1"})
        runner = SessionReplayRunner(
            manifest, RunKey("r", "c", 0), backend, clock, TraceWriter()
        )
        while not runner.done():
            runner.step()
            clock.advance_ms(1)
        self.assertEqual(len(runner.invalid_target), 1)
        self.assertFalse(next(iter(runner.traces.values())).target_verified)
        self.assertEqual(
            classify_chunk({"choices": [{"delta": {"role": "assistant"}}]}),
            (ChunkKind.ROLE, ""),
        )
        self.assertEqual(
            classify_chunk({"choices": [], "usage": {"prompt_tokens": 3}}),
            (ChunkKind.USAGE, ""),
        )
        self.assertEqual(
            classify_chunk({"choices": [{"delta": {"reasoning_content": "hm"}}]}),
            (ChunkKind.REASONING, "hm"),
        )
        self.assertEqual(
            classify_chunk({"choices": [{"delta": {"content": "ok"}}]}),
            (ChunkKind.CONTENT, "ok"),
        )
        self.assertEqual(
            classify_chunk({"choices": [{"delta": {}, "finish_reason": "stop"}]}),
            (ChunkKind.DONE, ""),
        )


class M12IntegrationEvents(unittest.TestCase):
    def test_out_of_order_events_single_store(self):
        trace = TraceWriter()
        setup = build_setup("B2_EAGER", SMALL, trace=trace)
        c, w0 = setup.coordinator, setup.workers["w0"]
        p = make_prefix()
        # transfer result for an unknown consideration: ignored, nothing pending
        setup.policy.on_transfer_result(
            TransferResultEvent("ghost", p, "cons-ghost", True, Reason.PUBLISHED, 0)
        )
        self.assertEqual(setup.policy.pending_prefixes(), [])
        # turn done before CPU ready
        w0.begin_offload(p, 30 * MS)
        d = c.turn_end(req(0, 0), p, "w0")
        self.assertEqual(d.reason, Reason.WAIT_SOURCE_READY)
        # duplicate CPU_READY events -> exactly one STORE_NOW
        setup.clock.advance_ms(40)
        c.poll()
        c.state_change(StateChangeKind.CPU_READY, p)
        c.state_change(StateChangeKind.CPU_READY, p)
        stores = [
            e
            for e in trace.events
            if e["event"] == "offload_decision" and e["action"] == "STORE_NOW"
        ]
        self.assertEqual(len(stores), 1)
        self.assertEqual(len(c.publications), 1)
        # a second turn of the same prefix while writing: no duplicate store
        d2 = c.turn_end(req(0, 1), p, "w0")
        self.assertEqual(d2.reason, Reason.SKIP_ALREADY_WRITING)
        self.assertNotEqual(d2.consideration_id, d.consideration_id)
        self.assertTrue(advance_until(setup, c.quiescent))
        c.refresh_snapshot()
        d3 = c.turn_end(req(0, 2), p, "w0")
        self.assertEqual(d3.reason, Reason.SKIP_ALREADY_READY)
        # every STORE_NOW has exactly one transfer_result with its consideration id
        results = [e for e in trace.events if e["event"] == "transfer_result"]
        self.assertEqual([r["consideration_id"] for r in results], [d.consideration_id])
        self.assertTrue(
            all(
                "consideration_id" in e
                for e in trace.events
                if e["event"] == "offload_decision"
            )
        )
        # stale/duplicate result for the finished consideration is a no-op
        setup.policy.on_transfer_result(
            TransferResultEvent(
                "late", p, d.consideration_id, False, Reason.PUBLICATION_FAILED, 0
            )
        )
        self.assertEqual(setup.store.state_of(p)[0], CxlState.READY)


class CellSmoke(unittest.TestCase):
    def test_all_arms_run_and_return_to_baseline(self):
        cfg = WorkloadConfig(
            sessions=8, scenario="MOVE_GAP", gap_s=1.0, seed=9, migrating_per_owner=2
        )
        manifest = build_manifest(cfg)
        summaries = {}
        for name in ("B0_RECOMPUTE", "B1_NETWORK", "B2_EAGER", "P_DELAY", "P_VALUE"):
            r = run_simulation(name, cfg, manifest=manifest)
            s = summarize_simulation(r)
            summaries[name] = s
            self.assertEqual(s["observed"]["ok"], 32)
            self.assertEqual(s["observed"]["marker_failures"], 0)
            self.assertEqual(s["observed"]["target_unverified"], 0)
            self.assertTrue(
                all(v == 0 for v in s["accounting_at_end"].values()),
                s["accounting_at_end"],
            )
            self.assertEqual(s["migration"]["designated_destination_changes"], 4)
            self.assertEqual(s["migration"]["actual_worker_changes"], 4)
            self.assertTrue(s["mock_only"])
        self.assertEqual(summaries["B0_RECOMPUTE"]["publication"]["enqueued"], 0)
        self.assertEqual(
            summaries["B1_NETWORK"]["reuse_paths"].get("network_import"), 4
        )
        self.assertEqual(summaries["B2_EAGER"]["reuse_paths"].get("cxl_import"), 4)
        self.assertEqual(
            summaries["B0_RECOMPUTE"]["migration"]["external_kv_reuse_success"], 0
        )
        self.assertEqual(
            summaries["B2_EAGER"]["migration"]["external_kv_reuse_success"],
            summaries["B2_EAGER"]["publication"]["ready"],
        )
        # no implicit network fallback in CXL arms
        self.assertNotIn("network_import", summaries["P_VALUE"]["reuse_paths"])

    def test_stay_scenario_pays_publication_without_remote_use(self):
        cfg = WorkloadConfig(sessions=4, scenario="STAY", gap_s=0.5, seed=4)
        r = run_simulation("B2_EAGER", cfg)
        s = summarize_simulation(r)
        self.assertEqual(s["migration"]["designated_destination_changes"], 0)
        self.assertGreater(s["publication"]["ready"], 0)
        self.assertNotIn("cxl_import", s["reuse_paths"])


class CapabilityAudit(unittest.TestCase):
    def test_audit_is_explicit(self):
        report = audit_vllm_0_19_offload()
        self.assertFalse(report.is_mock)
        self.assertEqual(set(report.items), set(REQUIRED_CAPABILITIES))
        for cap in report.items.values():
            self.assertTrue(cap.evidence)
        self.assertEqual(
            report.items["shared_offset_check"].status, CapabilityStatus.BLOCKED
        )
        self.assertEqual(
            report.items["cross_host_visibility_primitive"].status,
            CapabilityStatus.BLOCKED,
        )
        self.assertEqual(
            report.items["cancellation_semantics"].status, CapabilityStatus.UNSUPPORTED
        )
        self.assertIn("exact_target_worker", report.not_supported())
        self.assertTrue(setup_fake_is_labelled())


def setup_fake_is_labelled() -> bool:
    setup = build_setup("B0_RECOMPUTE", SMALL)
    rep = setup.workers["w0"].inspect_capabilities()
    return rep.is_mock and rep.backend == "fake"


class FrozenConfigs(unittest.TestCase):
    """configs/*.json must match the code defaults; edit both or neither."""

    def _load(self, name):
        import json

        return json.loads((HANDOFF / "configs" / name).read_text())

    def test_workload_and_policy_defaults(self):
        w = self._load("workload_default.json")
        cfg = WorkloadConfig()
        for key in (
            "sessions",
            "turns_per_session",
            "reusable_prefix_tokens",
            "max_output_tokens",
            "max_outstanding_requests",
            "max_model_len",
            "offload_block_tokens",
            "migration_turn",
            "migrating_per_owner",
            "bytes_per_token",
        ):
            self.assertEqual(w[key], getattr(cfg, key), key)
        self.assertEqual(tuple(w["workers"]), cfg.workers)
        self.assertEqual(
            sum(w["mixed_length_arm"]["prefix_tokens_per_session"]), 32 * 8192
        )
        p = self._load("policy_pvalue.json")
        pc = PolicyConfig(name="P_VALUE")
        for key in ("defer_budget_ms", "stale_snapshot_ms", "timer_ms", "margin"):
            self.assertEqual(p[key], getattr(pc, key), key)
        self.assertEqual(p["remote_reuse_weight"], pc.remote_reuse_weight)
        e = self._load("executor_limits.json")
        lim = ExecutorLimits()
        self.assertEqual(e["chunk_bytes"], lim.chunk_bytes)
        self.assertEqual(e["staging_budget_bytes"], lim.staging_budget_bytes)
        self.assertEqual(
            e["max_inflight_publications_per_source"],
            lim.max_inflight_publications_per_source,
        )
        self.assertEqual(e["max_inflight_publications"], lim.max_inflight_publications)
        c = self._load("calibration_mock.json")
        self.assertEqual(c["source"], "mock")
        self.assertEqual(c["calibration_id"], MOCK_CALIBRATION.calibration_id)
        self.assertEqual(
            [tuple(pt.values()) for pt in c["points"]],
            [
                (
                    q.prefix_tokens,
                    q.t_net_ms,
                    q.t_recompute_ms,
                    q.t_cxl_read_ms,
                    q.t_cxl_write_ms,
                )
                for q in MOCK_CALIBRATION.points
            ],
        )
        cap = self._load("capacity.json")
        self.assertFalse(cap["slice_origin_confirmed"])
        self.assertEqual(cap["block_bytes"], StoreCapacity().block_bytes)
        self.assertEqual(
            cap["metadata_reserve_bytes_mock"], StoreCapacity().metadata_reserve_bytes
        )


if __name__ == "__main__":
    unittest.main()
