#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""KV pressure workload: working set >> GPU pool → DRAM tier로 offload/restore 유발.

설계: S개 세션이 각자 긴 고유 prefix를 갖고, 라운드마다 모든 세션이 한 turn씩 진행한다.
한 라운드 안에서 다른 세션들이 GPU pool을 밀어내므로, 다음 라운드에 자기 prefix를
다시 쓰려면 (a) DRAM tier에서 restore 하거나 (b) 재계산해야 한다.
working_set = S x prefix_tokens 를 GPU pool(2 worker 합계)의 배수로 조절한다.
"""
import argparse, json, time, urllib.request
from concurrent.futures import ThreadPoolExecutor

P = argparse.ArgumentParser()
P.add_argument("--url", default="http://localhost:8080/v1/chat/completions")
P.add_argument("--model", default="Qwen/Qwen3-14B")
P.add_argument("--sessions", type=int, default=32)
P.add_argument("--turns", type=int, default=4)
P.add_argument("--prefix-words", type=int, default=1000, help="~2.8 tokens/word")
P.add_argument("--concurrency", type=int, default=8)
P.add_argument("--out", default="/tmp/pressure_run.jsonl")
P.add_argument("--salt", default="", help="cell-unique tag mixed into every prefix so cells never share cache")
args = P.parse_args()

def make_prefix(sid):
    head = f"[CELL-{args.salt}][SESSION-{sid}] Repository audit log for project {sid}. Reference notes:\n"
    body = " ".join(f"{args.salt}s{sid}-item{i}-{(i*7919+sid*104729+len(args.salt)*31) % 100003}" for i in range(args.prefix_words))
    return head + body

prefixes = {s: make_prefix(s) for s in range(args.sessions)}

def call(sid, turn):
    msg = prefixes[sid] + f"\n\nTurn {turn}: reply exactly S{sid}T{turn}_OK /no_think"
    body = json.dumps({"model": args.model,
                       "messages": [{"role": "user", "content": msg}],
                       "max_tokens": 24, "temperature": 0}).encode()
    req = urllib.request.Request(args.url, data=body, headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        d = json.load(urllib.request.urlopen(req, timeout=600))
        return {"session": sid, "turn": turn, "latency_s": round(time.time()-t0, 3),
                "ok": True, "prompt_tokens": d.get("usage", {}).get("prompt_tokens")}
    except Exception as e:
        return {"session": sid, "turn": turn, "latency_s": round(time.time()-t0, 3),
                "ok": False, "error": f"{type(e).__name__}: {str(e)[:120]}"}

rows = []
for turn in range(args.turns):
    t_start = time.time()
    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        for r in ex.map(lambda s: call(s, turn), range(args.sessions)):
            rows.append(r)
    lat = sorted(r["latency_s"] for r in rows[-args.sessions:] if r["ok"])
    nok = sum(1 for r in rows[-args.sessions:] if r["ok"])
    if lat:
        print(f"[turn {turn}] ok={nok}/{args.sessions} wall={time.time()-t_start:.1f}s "
              f"lat p50={lat[len(lat)//2]:.2f}s p95={lat[int(len(lat)*0.95)-1]:.2f}s max={lat[-1]:.2f}s", flush=True)
    else:
        print(f"[turn {turn}] ok=0/{args.sessions} — {rows[-1].get('error')}", flush=True)

with open(args.out, "w") as f:
    for r in rows:
        f.write(json.dumps(r) + "\n")

ok = [r for r in rows if r["ok"]]
pt = ok[0]["prompt_tokens"] if ok else 0
print(f"\n=== {len(ok)}/{len(rows)} ok | prompt_tokens/req≈{pt} "
      f"| working_set≈{args.sessions*(pt or 0):,} tokens | out={args.out}")
