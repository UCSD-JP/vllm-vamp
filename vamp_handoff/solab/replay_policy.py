# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""Near-complete Heavy-48 replay with a minimal decision layer (arms S / L / LR).

Recorded prompts, not a SWE-correctness run. All turns of every session in
recorded per-session order, closed loop (a session's next turn becomes ready when
its previous response completes; no think gaps). Token-budget admission per worker
(in-flight prompt tokens + this prompt + output cap <= budget) prevents the c=8
concentration stall. Only the decision layer differs between arms:

  S   static owner = session_index % 2; no move.
  L   when the owner is over budget and the other worker is under budget, send
      this turn to the other worker (it recomputes); owner unchanged afterwards.
  LR  like L, but the moved session is re-homed (owner := new worker).

Migration arms (valuation, TCP/CXL transfer) live in policy_replay.py /
policy_router.py (routing-valuation-spec.md), which supersede the M arm that was
sketched in replay-policy-spec.md; this runner provides the S/L baselines.

Per-turn fields: http_latency_s (HTTP dispatch -> response complete), ttft_s,
admission_wait_s (ready -> dispatch), arrival_to_done_s = admission_wait_s +
http_latency_s. rp1 (S, L) was recorded with an earlier revision where the field
named arrival_to_done_s held the HTTP latency and admission_wait_s was separate;
read those runs accordingly. GPU/CPU hit accounting is done post-hoc from probe
logs by request id, not from HTTP.
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
    p.add_argument("--arm", choices=["S", "L", "LR"], required=True)
    p.add_argument("--trace-dir", required=True)
    p.add_argument("--audit", required=True)
    p.add_argument("--out-dir", required=True)
    p.add_argument("--run-id", required=True)
    p.add_argument("--sessions", type=int, default=48)
    p.add_argument("--turns", type=int, default=0, help="0 = all recorded turns (near-complete)")
    p.add_argument("--concurrency", type=int, default=6)
    p.add_argument("--pool-tokens", type=int, required=True, help="measured GPU KV pool per worker")
    p.add_argument("--budget-frac", type=float, default=0.8)
    args = p.parse_args()

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=False)
    audit = json.loads(Path(args.audit).read_text())
    selected = audit["rows"][: args.sessions]
    traces = [load_trace(Path(args.trace_dir) / (r["task"] + ".jsonl.gz")) for r in selected]
    budget = int(args.budget_frac * args.pool_tokens)
    cap = [(min(args.turns, len(t)) if args.turns else len(t)) for t in traces]
    expected = sum(cap)
    rehome = args.arm == "LR"

    inflight_tokens = [0, 0]
    now = time.monotonic
    ready = deque((i, 0, now()) for i in range(len(traces)))   # (session, turn, ready_mono)
    owner = {}
    log = (out / "cell.jsonl").open("a")

    def emit(event, **data):
        row = dict(event=event, wall=time.time(), mono=now(), **data)
        log.write(json.dumps(row) + "\n"); log.flush()
        if event != "request":
            print(json.dumps(row), flush=True)
        return row

    def prompt_tokens(i, turn):
        return selected[i]["prompt_tokens"][turn]

    def destination(i, turn):
        base = owner.get(i, i % 2)
        if args.arm == "S":
            return base, False
        other = 1 - base
        need = prompt_tokens(i, turn) + OUTPUT_TOKENS
        if inflight_tokens[base] + need > budget and inflight_tokens[other] + need <= budget:
            return other, True
        return base, False

    emit("config", **{k: getattr(args, k) for k in vars(args)}, budget=budget,
         expected_requests=expected, output_tokens=OUTPUT_TOKENS, rehome=rehome)
    started = now()
    emit("pressure_start", sessions=len(traces), total_turns=expected)
    rows, active = [], {}
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        while ready or active:
            deferred = deque()
            while ready and len(active) < args.concurrency:
                i, turn, ready_at = ready.popleft()
                worker, moved = destination(i, turn)
                need = prompt_tokens(i, turn) + OUTPUT_TOKENS
                if inflight_tokens[worker] + need > budget:
                    deferred.append((i, turn, ready_at)); continue
                src = owner.get(i, i % 2)
                if i not in owner or (moved and rehome):
                    owner[i] = worker
                inflight_tokens[worker] += need
                dispatch_at = now()
                fut = pool.submit(serving_request, traces[i][turn]["messages"], selected[i]["task"],
                                  turn, worker, args.run_id, OUTPUT_TOKENS)
                active[fut] = (i, turn, worker, moved, need, ready_at, dispatch_at, src)
            ready.extendleft(reversed(deferred))
            if not active:
                time.sleep(0.02); continue
            done, _ = wait(active, return_when=FIRST_COMPLETED)
            for fut in sorted(done, key=lambda f: active[f][:2]):
                i, turn, worker, moved, need, ready_at, dispatch_at, src = active.pop(fut)
                inflight_tokens[worker] -= need
                res = fut.result()
                assert res["usage"]["prompt_tokens"] == prompt_tokens(i, turn), (res, i, turn)
                admission_wait = dispatch_at - ready_at
                row = dict(session=i, task=selected[i]["task"], turn=turn, worker=worker, owner=owner[i],
                           moved=moved, moved_from=src if moved else None,
                           http_latency_s=round(res["latency_s"], 4), ttft_s=round(res["ttft_s"], 4),
                           admission_wait_s=round(admission_wait, 4),
                           arrival_to_done_s=round(admission_wait + res["latency_s"], 4),
                           prompt_tokens=res["usage"]["prompt_tokens"],
                           fingerprint=selected[i]["token_fingerprints"][turn], output_sha256=res["output_sha256"])
                rows.append(emit("request", **row))
                if turn + 1 < cap[i]:
                    ready.append((i, turn + 1, now()))
                if len(rows) % 96 == 0:
                    emit("progress", completed=len(rows), expected=expected, elapsed_s=round(now() - started, 1),
                         moved=sum(r["moved"] for r in rows), inflight_tokens=list(inflight_tokens))
    assert len(rows) == expected, (len(rows), expected)
    emit("pressure_done", completed=len(rows), elapsed_s=round(now() - started, 2),
         moved_turns=sum(r["moved"] for r in rows),
         total_prompt_tokens=sum(r["prompt_tokens"] for r in rows))
    log.close()


if __name__ == "__main__":
    main()
