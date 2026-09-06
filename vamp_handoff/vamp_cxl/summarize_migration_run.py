# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""Join request, decision and transfer traces into a run summary (spec §11).

Baseline fallbacks (source miss, copy failure, recompute after a failed
import) are reported separately from normal cache misses and are never
counted as migration successes. Observed values and estimates are kept in
separate sections. Durations are subtracted only inside one clock domain.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from collections.abc import Iterable
from typing import Any

NS_PER_MS = 1_000_000


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    vals = sorted(values)
    idx = min(len(vals) - 1, max(0, int(round(q * (len(vals) - 1)))))
    return vals[idx]


def _stats(values: list[float]) -> dict[str, Any]:
    return {
        "n": len(values),
        "p50": percentile(values, 0.50),
        "p95": percentile(values, 0.95),
        "max": max(values) if values else None,
    }


def summarize_events(events: Iterable[dict[str, Any]]) -> dict[str, Any]:
    responses: list[dict[str, Any]] = []
    dispatches: list[dict[str, Any]] = []
    offload_decisions: list[dict[str, Any]] = []
    demand_decisions: list[dict[str, Any]] = []
    transfer_results: list[dict[str, Any]] = []
    publication_enqueued: list[dict[str, Any]] = []
    for ev in events:
        kind = ev.get("event")
        if kind == "response_done":
            responses.append(ev)
        elif kind == "dispatch":
            dispatches.append(ev)
        elif kind == "offload_decision":
            offload_decisions.append(ev)
        elif kind == "demand_decision":
            demand_decisions.append(ev)
        elif kind == "transfer_result":
            transfer_results.append(ev)
        elif kind == "publication_enqueued":
            publication_enqueued.append(ev)

    ok = [r for r in responses if r.get("status") == "ok"]
    ttft = [
        r["ttft_content_ns"] / NS_PER_MS
        for r in ok
        if r.get("ttft_content_ns") is not None
    ]
    ttft_reasoning = [
        r["ttft_reasoning_ns"] / NS_PER_MS
        for r in ok
        if r.get("ttft_reasoning_ns") is not None
    ]
    admission = [
        r["admission_delay_ns"] / NS_PER_MS
        for r in responses
        if r.get("admission_delay_ns") is not None
    ]
    e2e = [r["e2e_ns"] / NS_PER_MS for r in ok if r.get("e2e_ns") is not None]

    designated_change = 0
    actual_change = 0
    last_designated: dict[int, str] = {}
    last_actual: dict[int, str | None] = {}
    for r in sorted(responses, key=lambda r: (r["session_id"], r["turn_id"])):
        sid = r["session_id"]
        if sid in last_designated and last_designated[sid] != r["designated_worker"]:
            designated_change += 1
        if sid in last_actual and last_actual[sid] != r.get("actual_worker"):
            actual_change += 1
        last_designated[sid] = r["designated_worker"]
        last_actual[sid] = r.get("actual_worker")

    demand_actions = Counter(d["action"] for d in demand_decisions)
    demand_reasons = Counter(d["reason"] for d in demand_decisions)
    offload_actions = Counter(d["action"] for d in offload_decisions)
    offload_reasons = Counter(d["reason"] for d in offload_decisions)
    result_reasons = Counter(t["reason"] for t in transfer_results)

    # Only a destination import counts as external KV reuse. PUBLISHED is a
    # source-side write into the shared tier and is reported under
    # publication.ready; a STAY run publishes without ever reusing remotely.
    external_reuse_success = result_reasons.get("CXL_IMPORTED", 0) + result_reasons.get(
        "NETWORK_IMPORTED", 0
    )
    fallbacks = {
        k: v
        for k, v in result_reasons.items()
        if k
        in (
            "NETWORK_COPY_FAILED",
            "IMPORT_FAILED",
            "CHECKSUM_MISMATCH",
            "PUBLICATION_FAILED",
            "PUBLICATION_CANCELLED",
        )
    }
    shadow_failed = sum(
        1 for d in offload_decisions + demand_decisions if d.get("shadow_failed")
    )

    return {
        "observed": {
            "requests": len(responses),
            "ok": len(ok),
            "errors": sum(1 for r in responses if r.get("status") == "error"),
            "target_unverified": sum(
                1 for r in responses if not r.get("target_verified")
            ),
            "marker_failures": sum(1 for r in ok if r.get("marker_ok") is False),
            "usage_missing": sum(1 for r in ok if not r.get("usage_observed")),
            "ttft_content_ms": _stats(ttft),
            "ttft_reasoning_ms": _stats(ttft_reasoning),
            "admission_delay_ms": _stats(admission),
            "e2e_ms": _stats(e2e),
        },
        "migration": {
            "designated_destination_changes": designated_change,
            "actual_worker_changes": actual_change,
            "external_kv_reuse_success": external_reuse_success,
            "demand_actions": dict(demand_actions),
            "demand_reasons": dict(demand_reasons),
        },
        "publication": {
            "considered": len({d["consideration_id"] for d in offload_decisions}),
            "offload_actions": dict(offload_actions),
            "offload_reasons": dict(offload_reasons),
            "enqueued": len(publication_enqueued),
            "ready": result_reasons.get("PUBLISHED", 0),
            "cancelled": result_reasons.get("PUBLICATION_CANCELLED", 0),
            "failed": result_reasons.get("PUBLICATION_FAILED", 0),
            "capacity_rejected": result_reasons.get("RESERVE_REJECTED_CAPACITY", 0),
        },
        "fallbacks": fallbacks,
        "correctness": {
            "checksum_mismatch": result_reasons.get("CHECKSUM_MISMATCH", 0),
            "import_failed": result_reasons.get("IMPORT_FAILED", 0),
            "shadow_failed_decisions": shadow_failed,
        },
        "estimates": {
            "calibration_ids": sorted(
                {
                    d.get("calibration_id")
                    for d in offload_decisions
                    if d.get("calibration_id")
                }
            ),
            "scored_decisions": sum(1 for d in offload_decisions if d.get("score")),
        },
    }


def summarize_simulation(result: Any) -> dict[str, Any]:
    """Summary for an in-memory :class:`SimulationResult` (adds reuse paths
    and capacity accounting from the fake store)."""
    summary = summarize_events(result.trace.events)
    paths = Counter(p["path"] for p in result.backend.reuse_paths)
    fallback_paths = Counter(
        p["fallback_reason"]
        for p in result.backend.reuse_paths
        if p.get("fallback_reason")
    )
    usage = result.coordinator.store.usage()
    summary["reuse_paths"] = dict(paths)
    summary["reuse_fallbacks"] = dict(fallback_paths)
    summary["capacity"] = {
        "payload_capacity_bytes": usage.payload_capacity_bytes,
        "occupied_bytes_at_end": usage.occupied_bytes,
        "staging_high_watermark_bytes": (
            result.coordinator.executor.staging_high_watermark
        ),
        "cpu_evictions_refused_pinned": result.backend.cpu_evictions_refused,
        # transfers that waited in the executor queue while already holding a
        # source pin (comparison-fairness flag; expected 0 for one-at-a-time)
        "transfers_queued_with_pin": result.coordinator.executor.queued_with_pin,
    }
    summary["accounting_at_end"] = result.coordinator.accounting()
    summary["counters"] = dict(result.coordinator.counters)
    summary["mock_only"] = True
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace", help="JSONL trace produced by TraceWriter")
    args = parser.parse_args(argv)
    with open(args.trace, encoding="utf-8") as fh:
        events = [json.loads(line) for line in fh if line.strip()]
    json.dump(summarize_events(events), sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
