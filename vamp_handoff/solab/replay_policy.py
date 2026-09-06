# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""Near-complete Heavy-48 replay with a decision layer (arms S/L; M added later).

Recorded prompts, not a SWE-correctness run. All turns of every session in
recorded per-session order. Token-budget admission per worker prevents the c=8
concentration stall. The decision layer chooses the destination worker per turn;
only that layer changes between arms.

  S: static owner = session_index % 2, no rebalance.
  L: static owner, but when the owner is over budget and the other worker is
     under budget, send this turn to the other worker (it recomputes the prefix).

Per-turn cost (arrival->completion, TTFT) is client-side; GPU/CPU hit accounting
is done post-hoc from the probe logs (heavy48_report style), not from HTTP.
"""
import argparse
import json
import time
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

from heavy48_replay import serving_request
from heavy48_workload import load_trace

URLS = ["http://localhost:8080/v1/chat/completions", "http://localhost:8081/v1/chat/completions"]
OUTPUT_TOKENS = 16


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--arm", choices=["S", "L"], required=True)
    p.add_argument("--trace-dir", required=True)
    p.add_argument("--audit", required=True)
    p.add_argument("--out-dir", required=True)
    p.add_argument("--run-id", required=True)
    p.add_argument("--sessions", type=int, default=48)
    p.add_argument("--turns", type=int, default=0, help="0 = all recorded turns (near-complete)")
    p.add_argument("--concurrency", type=int, default=6)
    p.add_argument("--pool-tokens", type=int, required=True, help="measured GPU KV pool per worker")
    p.add_argument("--budget-frac", type=float, default=0.8)
    p.add_argument("--rebalance-margin", type=int, default=2, help="L: min ready-queue depth advantage to move")
    args = p.parse_args()

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=False)
    audit = json.loads(Path(args.audit).read_text())
    selected = audit["rows"][: args.sessions]
    traces = [load_trace(Path(args.trace_dir) / (r["task"] + ".jsonl.gz")) for r in selected]
    budget = int(args.budget_frac * args.pool_tokens)
    cap = [(min(args.turns, len(t)) if args.turns else len(t)) for t in traces]
    expected = sum(cap)

    inflight_tokens = [0, 0]          # prompt tokens in flight per worker
    now = time.monotonic
    ready = deque((i, 0, now()) for i in range(len(traces)))  # (session, turn, enqueue_mono)
    owner = {}                        # session -> worker that served its first turn

    log = (out / "cell.jsonl").open("a")

    def emit(event, **data):
        row = dict(event=event, wall=time.time(), mono=time.monotonic(), **data)
        log.write(json.dumps(row) + "\n"); log.flush()
        if event != "request":
            print(json.dumps(row), flush=True)
        return row

    def prompt_tokens(i, turn):
        return selected[i]["prompt_tokens"][turn]

    def destination(i, turn):
        """Decision layer. Returns (worker, moved)."""
        base = owner.get(i, i % 2)
        if args.arm == "S":
            return base, False
        # L: if the base worker cannot admit but the other can, and the other is the
        # lighter queue, move this turn there (recompute on the other worker).
        other = 1 - base
        need = prompt_tokens(i, turn) + OUTPUT_TOKENS
        base_fits = inflight_tokens[base] + need <= budget
        other_fits = inflight_tokens[other] + need <= budget
        if not base_fits and other_fits:
            return other, True
        return base, False

    emit("config", **{k: getattr(args, k) for k in vars(args)}, budget=budget,
         expected_requests=expected, output_tokens=OUTPUT_TOKENS)
    started = time.monotonic()
    emit("pressure_start", sessions=len(traces), total_turns=expected)
    rows = []
    active = {}
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        while ready or active:
            # admit ready turns whose chosen worker has token budget; keep the rest queued
            admitted_any = False
            deferred = deque()
            while ready and len(active) < args.concurrency:
                i, turn, enq = ready.popleft()
                worker, moved = destination(i, turn)
                need = prompt_tokens(i, turn) + OUTPUT_TOKENS
                if inflight_tokens[worker] + need > budget:
                    deferred.append((i, turn, enq))   # cannot admit yet; requeue after this sweep
                    continue
                if i not in owner:
                    owner[i] = worker
                inflight_tokens[worker] += need
                wait_s = now() - enq
                fut = pool.submit(serving_request, traces[i][turn]["messages"],
                                  selected[i]["task"], turn, worker, args.run_id, OUTPUT_TOKENS)
                active[fut] = (i, turn, worker, moved, need, wait_s)
                admitted_any = True
            ready.extendleft(reversed(deferred))       # preserve order for the waiters
            if not active:
                time.sleep(0.02)                       # everything waiting on budget; yield
                continue
            done, _ = wait(active, return_when=FIRST_COMPLETED)
            for fut in sorted(done, key=lambda f: active[f][:2]):
                i, turn, worker, moved, need, wait_s = active.pop(fut)
                inflight_tokens[worker] -= need
                res = fut.result()
                assert res["usage"]["prompt_tokens"] == prompt_tokens(i, turn), (res, i, turn)
                row = dict(session=i, task=selected[i]["task"], turn=turn, worker=worker,
                           owner=owner[i], moved=moved,
                           arrival_to_done_s=res["latency_s"], ttft_s=res["ttft_s"],
                           admission_wait_s=round(wait_s, 4),
                           prompt_tokens=res["usage"]["prompt_tokens"],
                           fingerprint=selected[i]["token_fingerprints"][turn],
                           output_sha256=res["output_sha256"])
                rows.append(emit("request", **row))
                if turn + 1 < cap[i]:
                    ready.append((i, turn + 1, now()))
                if len(rows) % 96 == 0:
                    emit("progress", completed=len(rows), expected=expected,
                         elapsed_s=round(now() - started, 1), inflight_tokens=list(inflight_tokens))
    assert len(rows) == expected, (len(rows), expected)
    emit("pressure_done", completed=len(rows), elapsed_s=round(now() - started, 2),
         moved_turns=sum(r["moved"] for r in rows),
         total_prompt_tokens=sum(r["prompt_tokens"] for r in rows))
    log.close()


if __name__ == "__main__":
    main()
