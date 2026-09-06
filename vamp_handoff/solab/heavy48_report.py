# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""Read immutable Heavy bundle logs; keep admission counts and mechanisms separate."""

import argparse
import json
from collections import Counter
from pathlib import Path

from heavy48_workload import parse_gpu_pool


def read(path):
    return [
        json.loads(line)
        for line in path.read_text().splitlines()
        if line.startswith("{")
    ]


def percentile(values, q):
    ordered = sorted(values)
    if not ordered:
        return None
    position = (len(ordered) - 1) * q
    lo = int(position)
    hi = min(lo + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (position - lo)


def analyze(root, arm):
    cell = read(root / arm / "cell.jsonl")
    begin = next(r for r in cell if r["event"] == "pressure_start")["wall"]
    end = next(r for r in cell if r["event"] == "pressure_done")
    requests = [r for r in cell if r["event"] == "request"]
    destination = next(r for r in cell if r["event"] == "destination")
    workers = {}
    for index, node in enumerate(("s1", "s2")):
        stats = [
            r
            for r in read(root / f"{arm}_wstats_{node}.jsonl")
            if begin <= r["ts"] <= end["wall"]
        ]
        probe = read(root / f"{arm}_probe_{node}.jsonl")
        phase = [r for r in probe if begin <= r["ts"] <= end["wall"]]
        hits = sum(r.get("hits") or 0 for r in stats)
        queries = sum(r.get("queries") or 0 for r in stats)
        conn_hits = sum(r.get("conn_hits") or 0 for r in stats)
        conn_queries = sum(r.get("conn_queries") or 0 for r in stats)
        expected = sum(
            r["usage"]["prompt_tokens"] for r in requests if r["worker"] == index
        )
        pool_tokens = parse_gpu_pool((root / f"{arm}_worker_{node}.log").read_text())
        events = Counter(r["event"] for r in phase)
        lookups = [r for r in phase if r["event"] == "heavy_gpu_lookup"]
        last_lookup = {r["request_id"]: r for r in lookups}
        admitted_gpu_hits = sum(r["gpu_hit_tokens"] for r in last_lookup.values())
        server_fps = Counter(r["fingerprint"] for r in last_lookup.values())
        client_fps = Counter(r["fingerprint"] for r in requests if r["worker"] == index)
        workers[node] = dict(
            pool_tokens=pool_tokens,
            request_count=sum(r["worker"] == index for r in requests),
            client_prompt_tokens=expected,
            gpu_queries=queries,
            gpu_hits=hits,
            query_matches_client=queries == expected,
            gpu_hit_rate=hits / queries if queries else None,
            last_lookup_gpu_hits=admitted_gpu_hits,
            last_lookup_gpu_hit_rate=admitted_gpu_hits / expected,
            accounting_identity=admitted_gpu_hits + conn_queries == expected,
            fingerprint_multiset_matches=server_fps == client_fps,
            max_active_leases=max(r.get("active_leases") or 0 for r in phase),
            cpu_queries=conn_queries,
            cpu_hits=conn_hits,
            cpu_hit_rate=conn_hits / conn_queries if conn_queries else None,
            usage_max=max(r["usage"] for r in stats),
            running_max=max(r["running"] for r in stats),
            waiting_max=max(r["waiting"] for r in stats),
            cpu_evicted_blocks=sum(
                r["n_blocks"] for r in phase if r["event"] == "evicted"
            ),
            cpu_ready_blocks=sum(r["n_blocks"] for r in phase if r["event"] == "ready"),
            lookup_events=len(lookups),
            lookup_unique_request_ids=len(set(r["request_id"] for r in lookups)),
            events=dict(events),
        )
        if node == "s2":
            matched = [
                r
                for r in probe
                if r.get("fingerprint") == destination["fingerprint"]
                and destination["start_wall"] <= r["ts"] <= destination["end_wall"]
                and r["event"] in ("heavy_gpu_lookup", "heavy_cpu_lookup")
            ]
            destination["mechanism_events"] = matched
    per_turn = []
    for turn in sorted(set(r["turn"] for r in requests)):
        selected = [r for r in requests if r["turn"] == turn]
        values = [r["latency_s"] for r in selected]
        per_turn.append(
            dict(
                turn=turn,
                n=len(selected),
                p50_s=percentile(values, 0.5),
                p95_s=percentile(values, 0.95),
                ttft_p50_s=percentile([r["ttft_s"] for r in selected], 0.5),
            )
        )
    return dict(
        arm=arm,
        requests=len(requests),
        pressure_s=end["elapsed_s"],
        workers=workers,
        per_turn=per_turn,
        checkpoint=next(r for r in cell if r["event"] == "checkpoint_plan"),
        selection=next(r for r in cell if r["event"] == "selection"),
        destination=destination,
        hook=next(r for r in cell if r["event"] == "hook_done"),
        gate=next(r for r in cell if r["event"] == "CELL_GATE"),
    )


def main():
    p = argparse.ArgumentParser()
    p.add_argument("root", type=Path)
    p.add_argument("--arms", nargs="+", default=["B0", "B1", "B2"])
    args = p.parse_args()
    results = [analyze(args.root, arm) for arm in args.arms]
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
