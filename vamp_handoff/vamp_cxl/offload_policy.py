# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""Pure offload/demand policies (spec §4, §7). No clock, I/O or transport.

Every decision carries the observation snapshot ID, the consideration ID and
a reason string. Calling a policy never performs work: the coordinator in
:mod:`vamp_cxl.kv_transfer_adapter` executes STORE_NOW / CXL_RESTORE /
NETWORK_COPY and reports real completion back via ``on_transfer_result``.

Observations never contain future destinations, migration lists, next gaps
or last-turn numbers (spec §3). The runner keeps those as ground truth.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from bisect import bisect_left
from dataclasses import dataclass, field
from enum import Enum

from .keys import PrefixKey, RequestKey

MS_NS = 1_000_000


class CpuState(str, Enum):
    ABSENT = "ABSENT"
    PENDING = "PENDING"
    READY = "READY"
    EVICTED = "EVICTED"


class CxlState(str, Enum):
    ABSENT = "ABSENT"
    RESERVED = "RESERVED"
    WRITING = "WRITING"
    READY = "READY"
    EVICTING = "EVICTING"
    ABORTING = "ABORTING"


class OffloadAction(str, Enum):
    STORE_NOW = "STORE_NOW"
    DEFER = "DEFER"
    SKIP = "SKIP"


class DemandAction(str, Enum):
    LOCAL = "LOCAL"
    NETWORK_COPY = "NETWORK_COPY"
    CXL_RESTORE = "CXL_RESTORE"
    RECOMPUTE = "RECOMPUTE"


class Reason(str, Enum):
    # offload
    STORE_NOW = "STORE_NOW"
    WAIT_SOURCE_READY = "WAIT_SOURCE_READY"
    DEFER_STALE = "DEFER_STALE"
    DEFER_READ_PRESSURE = "DEFER_READ_PRESSURE"
    SKIP_CLOSED = "SKIP_CLOSED"
    SKIP_ALREADY_READY = "SKIP_ALREADY_READY"
    SKIP_ALREADY_WRITING = "SKIP_ALREADY_WRITING"
    SKIP_SLOT_BUSY = "SKIP_SLOT_BUSY"
    SKIP_DEFER_TIMEOUT = "SKIP_DEFER_TIMEOUT"
    SKIP_UNCALIBRATED = "SKIP_UNCALIBRATED"
    SKIP_INVALID_CALIBRATION = "SKIP_INVALID_CALIBRATION"
    SKIP_LOW_VALUE = "SKIP_LOW_VALUE"
    SKIP_SOURCE_EVICTED = "SKIP_SOURCE_EVICTED"
    SKIP_SOURCE_ABSENT = "SKIP_SOURCE_ABSENT"
    SKIP_BASELINE_NO_PUBLICATION = "SKIP_BASELINE_NO_PUBLICATION"
    FAIL_CLOSED_MODEL_MISMATCH = "FAIL_CLOSED_MODEL_MISMATCH"
    # demand
    LOCAL_HIT = "LOCAL_HIT"
    CXL_READY = "CXL_READY"
    RECOMPUTE_NOT_READY = "RECOMPUTE_NOT_READY"
    RECOMPUTE_ABSENT = "RECOMPUTE_ABSENT"
    RECOMPUTE_BASELINE = "RECOMPUTE_BASELINE"
    RECOMPUTE_SOURCE_MISS = "RECOMPUTE_SOURCE_MISS"
    NETWORK_SOURCE_READY = "NETWORK_SOURCE_READY"
    # transfer results (reported by the coordinator, never by the policy)
    PUBLISHED = "PUBLISHED"
    PUBLICATION_FAILED = "PUBLICATION_FAILED"
    PUBLICATION_CANCELLED = "PUBLICATION_CANCELLED"
    RESERVE_REJECTED_CAPACITY = "RESERVE_REJECTED_CAPACITY"
    IMPORT_FAILED = "IMPORT_FAILED"
    NETWORK_COPY_FAILED = "NETWORK_COPY_FAILED"
    CHECKSUM_MISMATCH = "CHECKSUM_MISMATCH"
    # destination actually imported the KV (the only events that count as
    # external reuse success; PUBLISHED is a source-side write, not a reuse)
    CXL_IMPORTED = "CXL_IMPORTED"
    NETWORK_IMPORTED = "NETWORK_IMPORTED"


class StateChangeKind(str, Enum):
    TIMER = "TIMER"
    CPU_READY = "CPU_READY"
    SOURCE_EVICTED = "SOURCE_EVICTED"
    NEW_REQUEST = "NEW_REQUEST"
    SESSION_CLOSED = "SESSION_CLOSED"
    TRANSFER_COMPLETE = "TRANSFER_COMPLETE"
    SNAPSHOT = "SNAPSHOT"


# --------------------------------------------------------------------------
# calibration and value score
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CalibrationPoint:
    prefix_tokens: int
    t_net_ms: float
    t_recompute_ms: float
    t_cxl_read_ms: float
    t_cxl_write_ms: float

    def is_finite_nonnegative(self) -> bool:
        vals = (
            self.t_net_ms,
            self.t_recompute_ms,
            self.t_cxl_read_ms,
            self.t_cxl_write_ms,
        )
        return all(math.isfinite(v) and v >= 0 for v in vals)


@dataclass(frozen=True)
class CalibrationTable:
    """Per-size service-time table; interpolation only inside measured range.

    ``source`` must be ``"mock"`` for anything not measured on the real path.
    """

    calibration_id: str
    source: str
    points: tuple[CalibrationPoint, ...]

    def __post_init__(self) -> None:
        sizes = [p.prefix_tokens for p in self.points]
        if sizes != sorted(sizes) or len(set(sizes)) != len(sizes):
            raise ValueError("calibration points must be strictly increasing")

    def lookup(self, prefix_tokens: int) -> CalibrationPoint | None:
        if not self.points:
            return None
        sizes = [p.prefix_tokens for p in self.points]
        if prefix_tokens < sizes[0] or prefix_tokens > sizes[-1]:
            return None  # never extrapolate
        idx = bisect_left(sizes, prefix_tokens)
        if sizes[idx] == prefix_tokens:
            return self.points[idx]
        lo, hi = self.points[idx - 1], self.points[idx]
        frac = (prefix_tokens - lo.prefix_tokens) / (
            hi.prefix_tokens - lo.prefix_tokens
        )

        def lerp(a: float, b: float) -> float:
            return a + (b - a) * frac

        return CalibrationPoint(
            prefix_tokens=prefix_tokens,
            t_net_ms=lerp(lo.t_net_ms, hi.t_net_ms),
            t_recompute_ms=lerp(lo.t_recompute_ms, hi.t_recompute_ms),
            t_cxl_read_ms=lerp(lo.t_cxl_read_ms, hi.t_cxl_read_ms),
            t_cxl_write_ms=lerp(lo.t_cxl_write_ms, hi.t_cxl_write_ms),
        )


@dataclass(frozen=True)
class ScoreBreakdown:
    valid: bool
    avoided_remote_service_ms: float
    weighted_benefit_ms: float
    publication_charge_ms: float
    threshold_ms: float
    eligible: bool
    remote_reuse_weight: float
    margin: float
    invalid_reason: str | None = None


def value_score(
    point: CalibrationPoint, remote_reuse_weight: float, margin: float
) -> ScoreBreakdown:
    """Conservative ranking surrogate (spec §7). Not a TTFT prediction."""
    invalid = None
    if not point.is_finite_nonnegative():
        invalid = "calibration has NaN/inf/negative cost"
    elif not (math.isfinite(remote_reuse_weight) and 0 <= remote_reuse_weight <= 1):
        invalid = "remote_reuse_weight outside [0, 1]"
    elif not (math.isfinite(margin) and margin >= 0):
        invalid = "margin must be finite and non-negative"
    if invalid is not None:
        return ScoreBreakdown(
            valid=False,
            avoided_remote_service_ms=math.nan,
            weighted_benefit_ms=math.nan,
            publication_charge_ms=math.nan,
            threshold_ms=math.nan,
            eligible=False,
            remote_reuse_weight=remote_reuse_weight,
            margin=margin,
            invalid_reason=invalid,
        )
    avoided = max(0.0, min(point.t_net_ms, point.t_recompute_ms) - point.t_cxl_read_ms)
    weighted = remote_reuse_weight * avoided
    charge = point.t_cxl_write_ms
    threshold = (1.0 + margin) * charge
    return ScoreBreakdown(
        valid=True,
        avoided_remote_service_ms=avoided,
        weighted_benefit_ms=weighted,
        publication_charge_ms=charge,
        threshold_ms=threshold,
        eligible=weighted > threshold,
        remote_reuse_weight=remote_reuse_weight,
        margin=margin,
    )


# --------------------------------------------------------------------------
# observations, events, decisions
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PolicyObservation:
    """Everything a policy may see. Built by the harness from present state.

    ``snapshot_taken_ns`` is the time the shared-tier snapshot was taken, in
    the same monotonic domain as ``now_ns``.
    """

    now_ns: int
    snapshot_id: str
    snapshot_taken_ns: int
    session_closed: bool
    model_matches: bool
    source_cpu_state: CpuState
    cxl_state: CxlState
    cxl_generation: int | None
    demand_reads_pending: int
    calibration: CalibrationPoint | None
    calibration_id: str | None
    local_ready_at_destination: bool = False
    source_workers_cpu_ready: tuple[str, ...] = ()

    @property
    def snapshot_age_ns(self) -> int:
        return self.now_ns - self.snapshot_taken_ns


@dataclass(frozen=True)
class TurnEndEvent:
    request: RequestKey
    prefix: PrefixKey
    turn_done_ns: int
    consideration_id: str
    source_worker: str


@dataclass(frozen=True)
class StateChangeEvent:
    kind: StateChangeKind
    now_ns: int
    prefix: PrefixKey | None = None


@dataclass(frozen=True)
class RequestEvent:
    request: RequestKey
    prefix: PrefixKey
    destination_worker: str
    arrival_ns: int


@dataclass(frozen=True)
class TransferResultEvent:
    job_id: str | None
    prefix: PrefixKey
    consideration_id: str | None
    success: bool
    reason: Reason
    now_ns: int


@dataclass(frozen=True)
class OffloadDecision:
    action: OffloadAction
    reason: Reason
    prefix: PrefixKey
    consideration_id: str
    snapshot_id: str
    decided_at_ns: int
    deadline_ns: int | None
    source_worker: str
    estimate_valid: bool
    score: ScoreBreakdown | None = None
    calibration_id: str | None = None
    shadow_failed: bool = False

    def is_pending(self) -> bool:
        return self.action == OffloadAction.DEFER


@dataclass(frozen=True)
class DemandDecision:
    action: DemandAction
    reason: Reason
    prefix: PrefixKey
    request: RequestKey
    destination_worker: str
    snapshot_id: str
    decided_at_ns: int
    source_worker: str | None = None
    cxl_generation: int | None = None
    shadow_failed: bool = False


@dataclass(frozen=True)
class PolicyConfig:
    name: str
    defer_budget_ms: int = 5000
    stale_snapshot_ms: int = 1000
    timer_ms: int = 100
    remote_reuse_weight: float = 0.5
    margin: float = 0.10


@dataclass
class Consideration:
    consideration_id: str
    prefix: PrefixKey
    request: RequestKey
    source_worker: str
    first_considered_ns: int
    started: bool = False
    history: list[OffloadDecision] = field(default_factory=list)


# --------------------------------------------------------------------------
# policies
# --------------------------------------------------------------------------


class OffloadPolicy(ABC):
    def __init__(self, config: PolicyConfig):
        self.config = config
        self._pending: dict[PrefixKey, Consideration] = {}
        self.decision_log: list[OffloadDecision | DemandDecision] = []
        self.transfer_results: list[TransferResultEvent] = []

    # -- public API (spec §5) ----------------------------------------------

    def on_turn_end(
        self, event: TurnEndEvent, observation: PolicyObservation
    ) -> OffloadDecision:
        # A later turn of the same prefix opens a fresh consideration with a
        # new ID; the previous attempt's pin/job stays attributed to its own ID.
        cons = Consideration(
            consideration_id=event.consideration_id,
            prefix=event.prefix,
            request=event.request,
            source_worker=event.source_worker,
            first_considered_ns=event.turn_done_ns,
        )
        self._pending[event.prefix] = cons
        return self._evaluate(cons, observation)

    def on_state_change(
        self, event: StateChangeEvent, observation: PolicyObservation
    ) -> list[OffloadDecision]:
        if event.prefix is None:
            return []
        cons = self._pending.get(event.prefix)
        if cons is None or cons.started:
            return []
        if event.kind == StateChangeKind.SOURCE_EVICTED:
            return [
                self._finish(
                    cons, observation, OffloadAction.SKIP, Reason.SKIP_SOURCE_EVICTED
                )
            ]
        return [self._evaluate(cons, observation)]

    @abstractmethod
    def on_request(
        self, event: RequestEvent, observation: PolicyObservation
    ) -> DemandDecision:
        raise NotImplementedError

    def on_transfer_result(self, event: TransferResultEvent) -> None:
        self.transfer_results.append(event)
        cons = self._pending.get(event.prefix)
        if cons is not None and cons.consideration_id == event.consideration_id:
            # Success or failure: the consideration is closed. No silent retry.
            del self._pending[event.prefix]

    def pending_prefixes(self) -> list[PrefixKey]:
        return [p for p, c in self._pending.items() if not c.started]

    def consideration(self, prefix: PrefixKey) -> Consideration | None:
        return self._pending.get(prefix)

    # -- internals ----------------------------------------------------------

    @abstractmethod
    def _evaluate(self, cons: Consideration, obs: PolicyObservation) -> OffloadDecision:
        raise NotImplementedError

    def _deadline_ns(self, cons: Consideration) -> int:
        return cons.first_considered_ns + self.config.defer_budget_ms * MS_NS

    def _finish(
        self,
        cons: Consideration,
        obs: PolicyObservation,
        action: OffloadAction,
        reason: Reason,
        *,
        score: ScoreBreakdown | None = None,
        estimate_valid: bool = False,
        deadline_ns: int | None = None,
    ) -> OffloadDecision:
        decision = OffloadDecision(
            action=action,
            reason=reason,
            prefix=cons.prefix,
            consideration_id=cons.consideration_id,
            snapshot_id=obs.snapshot_id,
            decided_at_ns=obs.now_ns,
            deadline_ns=deadline_ns,
            source_worker=cons.source_worker,
            estimate_valid=estimate_valid,
            score=score,
            calibration_id=obs.calibration_id,
        )
        cons.history.append(decision)
        self.decision_log.append(decision)
        if action == OffloadAction.SKIP:
            self._pending.pop(cons.prefix, None)
        elif action == OffloadAction.STORE_NOW:
            cons.started = True
        return decision

    def _demand(
        self,
        event: RequestEvent,
        obs: PolicyObservation,
        action: DemandAction,
        reason: Reason,
        source_worker: str | None = None,
    ) -> DemandDecision:
        decision = DemandDecision(
            action=action,
            reason=reason,
            prefix=event.prefix,
            request=event.request,
            destination_worker=event.destination_worker,
            snapshot_id=obs.snapshot_id,
            decided_at_ns=obs.now_ns,
            source_worker=source_worker,
            cxl_generation=obs.cxl_generation,
        )
        self.decision_log.append(decision)
        return decision

    # shared pre-checks for the publishing arms (B2, P_DELAY, P_VALUE)
    def _common_skip(
        self, cons: Consideration, obs: PolicyObservation
    ) -> OffloadDecision | None:
        if obs.session_closed:
            return self._finish(cons, obs, OffloadAction.SKIP, Reason.SKIP_CLOSED)
        if obs.cxl_state == CxlState.READY:
            return self._finish(
                cons, obs, OffloadAction.SKIP, Reason.SKIP_ALREADY_READY
            )
        if obs.cxl_state in (CxlState.RESERVED, CxlState.WRITING):
            return self._finish(
                cons, obs, OffloadAction.SKIP, Reason.SKIP_ALREADY_WRITING
            )
        if obs.cxl_state in (CxlState.EVICTING, CxlState.ABORTING):
            return self._finish(cons, obs, OffloadAction.SKIP, Reason.SKIP_SLOT_BUSY)
        if not obs.model_matches:
            return self._finish(
                cons, obs, OffloadAction.SKIP, Reason.FAIL_CLOSED_MODEL_MISMATCH
            )
        if obs.source_cpu_state == CpuState.EVICTED:
            return self._finish(
                cons, obs, OffloadAction.SKIP, Reason.SKIP_SOURCE_EVICTED
            )
        if obs.source_cpu_state == CpuState.ABSENT:
            return self._finish(
                cons, obs, OffloadAction.SKIP, Reason.SKIP_SOURCE_ABSENT
            )
        return None

    def _shared_demand(
        self, event: RequestEvent, obs: PolicyObservation
    ) -> DemandDecision:
        """Demand rule common to B2 and P (spec §4): no network fallback."""
        if obs.local_ready_at_destination:
            return self._demand(event, obs, DemandAction.LOCAL, Reason.LOCAL_HIT)
        if obs.cxl_state == CxlState.READY:
            return self._demand(event, obs, DemandAction.CXL_RESTORE, Reason.CXL_READY)
        if obs.cxl_state == CxlState.ABSENT:
            return self._demand(
                event, obs, DemandAction.RECOMPUTE, Reason.RECOMPUTE_ABSENT
            )
        # RESERVED / WRITING / EVICTING / ABORTING: wait_budget_ms = 0
        return self._demand(
            event, obs, DemandAction.RECOMPUTE, Reason.RECOMPUTE_NOT_READY
        )


class B0RecomputePolicy(OffloadPolicy):
    def _evaluate(self, cons: Consideration, obs: PolicyObservation) -> OffloadDecision:
        return self._finish(
            cons, obs, OffloadAction.SKIP, Reason.SKIP_BASELINE_NO_PUBLICATION
        )

    def on_request(self, event: RequestEvent, obs: PolicyObservation) -> DemandDecision:
        if obs.local_ready_at_destination:
            return self._demand(event, obs, DemandAction.LOCAL, Reason.LOCAL_HIT)
        return self._demand(
            event, obs, DemandAction.RECOMPUTE, Reason.RECOMPUTE_BASELINE
        )


class B1NetworkPolicy(OffloadPolicy):
    def _evaluate(self, cons: Consideration, obs: PolicyObservation) -> OffloadDecision:
        return self._finish(
            cons, obs, OffloadAction.SKIP, Reason.SKIP_BASELINE_NO_PUBLICATION
        )

    def on_request(self, event: RequestEvent, obs: PolicyObservation) -> DemandDecision:
        if obs.local_ready_at_destination:
            return self._demand(event, obs, DemandAction.LOCAL, Reason.LOCAL_HIT)
        sources = [
            w for w in obs.source_workers_cpu_ready if w != event.destination_worker
        ]
        if sources:
            return self._demand(
                event,
                obs,
                DemandAction.NETWORK_COPY,
                Reason.NETWORK_SOURCE_READY,
                source_worker=sorted(sources)[0],
            )
        return self._demand(
            event, obs, DemandAction.RECOMPUTE, Reason.RECOMPUTE_SOURCE_MISS
        )


class B2EagerPolicy(OffloadPolicy):
    def _evaluate(self, cons: Consideration, obs: PolicyObservation) -> OffloadDecision:
        skip = self._common_skip(cons, obs)
        if skip is not None:
            return skip
        if obs.source_cpu_state != CpuState.READY:
            return self._finish(
                cons, obs, OffloadAction.DEFER, Reason.WAIT_SOURCE_READY
            )
        return self._finish(cons, obs, OffloadAction.STORE_NOW, Reason.STORE_NOW)

    def on_request(self, event: RequestEvent, obs: PolicyObservation) -> DemandDecision:
        return self._shared_demand(event, obs)


class PDelayPolicy(OffloadPolicy):
    """Same admission as B2; only the write start is delayed (spec §7)."""

    def _evaluate(self, cons: Consideration, obs: PolicyObservation) -> OffloadDecision:
        deadline = self._deadline_ns(cons)
        if obs.now_ns >= deadline:
            return self._finish(
                cons,
                obs,
                OffloadAction.SKIP,
                Reason.SKIP_DEFER_TIMEOUT,
                deadline_ns=deadline,
            )
        skip = self._common_skip(cons, obs)
        if skip is not None:
            return skip
        if obs.snapshot_age_ns > self.config.stale_snapshot_ms * MS_NS:
            return self._finish(
                cons, obs, OffloadAction.DEFER, Reason.DEFER_STALE, deadline_ns=deadline
            )
        if obs.demand_reads_pending > 0:
            return self._finish(
                cons,
                obs,
                OffloadAction.DEFER,
                Reason.DEFER_READ_PRESSURE,
                deadline_ns=deadline,
            )
        if obs.source_cpu_state != CpuState.READY:
            return self._finish(
                cons,
                obs,
                OffloadAction.DEFER,
                Reason.WAIT_SOURCE_READY,
                deadline_ns=deadline,
            )
        return self._finish(
            cons, obs, OffloadAction.STORE_NOW, Reason.STORE_NOW, deadline_ns=deadline
        )

    def on_request(self, event: RequestEvent, obs: PolicyObservation) -> DemandDecision:
        return self._shared_demand(event, obs)


class PValuePolicy(OffloadPolicy):
    """Explicit value heuristic for implementation validation (spec §7).

    Decision order is fixed and identical on every timer/state re-evaluation.
    The 5 s clock starts at turn end and is never restarted.
    """

    def _evaluate(self, cons: Consideration, obs: PolicyObservation) -> OffloadDecision:
        # 1. closed / already ready / writing
        skip = self._common_skip(cons, obs)
        if skip is not None:
            return skip
        deadline = self._deadline_ns(cons)
        # 3. deadline before any DEFER/WAIT
        if obs.now_ns >= deadline:
            return self._finish(
                cons,
                obs,
                OffloadAction.SKIP,
                Reason.SKIP_DEFER_TIMEOUT,
                deadline_ns=deadline,
            )
        # 4. calibration
        if obs.calibration is None:
            return self._finish(
                cons,
                obs,
                OffloadAction.SKIP,
                Reason.SKIP_UNCALIBRATED,
                deadline_ns=deadline,
            )
        score = value_score(
            obs.calibration, self.config.remote_reuse_weight, self.config.margin
        )
        if not score.valid:
            return self._finish(
                cons,
                obs,
                OffloadAction.SKIP,
                Reason.SKIP_INVALID_CALIBRATION,
                score=score,
                deadline_ns=deadline,
            )
        # 5. value
        if not score.eligible:
            return self._finish(
                cons,
                obs,
                OffloadAction.SKIP,
                Reason.SKIP_LOW_VALUE,
                score=score,
                estimate_valid=True,
                deadline_ns=deadline,
            )
        # 6. stale snapshot / read pressure
        if obs.snapshot_age_ns > self.config.stale_snapshot_ms * MS_NS:
            return self._finish(
                cons,
                obs,
                OffloadAction.DEFER,
                Reason.DEFER_STALE,
                score=score,
                estimate_valid=True,
                deadline_ns=deadline,
            )
        if obs.demand_reads_pending > 0:
            return self._finish(
                cons,
                obs,
                OffloadAction.DEFER,
                Reason.DEFER_READ_PRESSURE,
                score=score,
                estimate_valid=True,
                deadline_ns=deadline,
            )
        # 7. source readiness
        if obs.source_cpu_state != CpuState.READY:
            return self._finish(
                cons,
                obs,
                OffloadAction.DEFER,
                Reason.WAIT_SOURCE_READY,
                score=score,
                estimate_valid=True,
                deadline_ns=deadline,
            )
        return self._finish(
            cons,
            obs,
            OffloadAction.STORE_NOW,
            Reason.STORE_NOW,
            score=score,
            estimate_valid=True,
            deadline_ns=deadline,
        )

    def on_request(self, event: RequestEvent, obs: PolicyObservation) -> DemandDecision:
        return self._shared_demand(event, obs)


class ShadowedPolicy(OffloadPolicy):
    """Baseline decisions with a shadow policy evaluated for logging only.

    Any exception in the shadow is captured; the baseline decision is returned
    unchanged (spec §7: B0/B1 never depend on the P_VALUE shadow).
    """

    def __init__(self, primary: OffloadPolicy, shadow: OffloadPolicy):
        super().__init__(primary.config)
        self.primary = primary
        self.shadow = shadow
        self.shadow_errors: list[str] = []
        self.shadow_decisions: list[OffloadDecision | DemandDecision] = []

    def _run_shadow(self, fn, *args):
        try:
            result = fn(*args)
        except Exception as exc:  # noqa: BLE001 - isolation is the point
            self.shadow_errors.append(f"{type(exc).__name__}: {exc}")
            return None
        if isinstance(result, list):
            self.shadow_decisions.extend(result)
        elif result is not None:
            self.shadow_decisions.append(result)
        return result

    def on_turn_end(
        self, event: TurnEndEvent, observation: PolicyObservation
    ) -> OffloadDecision:
        decision = self.primary.on_turn_end(event, observation)
        shadow = self._run_shadow(self.shadow.on_turn_end, event, observation)
        self.decision_log.append(decision)
        if shadow is None:
            return OffloadDecision(**{**decision.__dict__, "shadow_failed": True})
        return decision

    def on_state_change(
        self, event: StateChangeEvent, observation: PolicyObservation
    ) -> list[OffloadDecision]:
        decisions = self.primary.on_state_change(event, observation)
        self._run_shadow(self.shadow.on_state_change, event, observation)
        self.decision_log.extend(decisions)
        return decisions

    def on_request(
        self, event: RequestEvent, observation: PolicyObservation
    ) -> DemandDecision:
        decision = self.primary.on_request(event, observation)
        shadow = self._run_shadow(self.shadow.on_request, event, observation)
        self.decision_log.append(decision)
        if shadow is None:
            return DemandDecision(**{**decision.__dict__, "shadow_failed": True})
        return decision

    def on_transfer_result(self, event: TransferResultEvent) -> None:
        self.primary.on_transfer_result(event)
        self._run_shadow(self.shadow.on_transfer_result, event)

    def pending_prefixes(self) -> list[PrefixKey]:
        return self.primary.pending_prefixes()

    def consideration(self, prefix: PrefixKey) -> Consideration | None:
        return self.primary.consideration(prefix)

    def _evaluate(self, cons: Consideration, obs: PolicyObservation) -> OffloadDecision:
        raise AssertionError("ShadowedPolicy delegates evaluation")


POLICY_NAMES = ("B0_RECOMPUTE", "B1_NETWORK", "B2_EAGER", "P_DELAY", "P_VALUE")


def make_policy(name: str, config: PolicyConfig | None = None) -> OffloadPolicy:
    cfg = config or PolicyConfig(name=name)
    if name == "B0_RECOMPUTE":
        return B0RecomputePolicy(cfg)
    if name == "B1_NETWORK":
        return B1NetworkPolicy(cfg)
    if name == "B2_EAGER":
        return B2EagerPolicy(cfg)
    if name == "P_DELAY":
        return PDelayPolicy(cfg)
    if name == "P_VALUE":
        return PValuePolicy(cfg)
    raise ValueError(f"unknown policy {name!r}; expected one of {POLICY_NAMES}")
