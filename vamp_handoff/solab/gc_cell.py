#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""G-C / G-D runner: one session, forced A -> B move via two fixed endpoints.

Turns 0..A-1 go to endpoint A (its namespace holds exactly one worker), then
turns A..A+B-1 go to endpoint B. The destination is fixed by topology, so no
per-request pinning or receipt patch is needed; gate_cell verifies each
namespace has exactly one registered instance.

Expected for G-C (no migration): B's first turn is local-cold (recompute).
Expected for G-D (network import before the move): B's first turn restores.
"""
import argparse, json, time, urllib.request

P = argparse.ArgumentParser()
P.add_argument("--url-a", default="http://localhost:8080/v1/chat/completions")
P.add_argument("--url-b", default="http://localhost:8081/v1/chat/completions")
P.add_argument("--model", default="Qwen/Qwen3-14B")
P.add_argument("--turns-a", type=int, default=3)
P.add_argument("--turns-b", type=int, default=2)
P.add_argument("--prefix-words", type=int, default=1000)
P.add_argument("--salt", required=True)
P.add_argument("--gap-s", type=float, default=0.0, help="pause between the last A turn and the first B turn")
P.add_argument("--hook", default="", help="shell command run during the gap (e.g. trigger the export/import)")
P.add_argument("--out", default="/tmp/gc_cell.jsonl")
args = P.parse_args()

sid = 0
head = f"[CELL-{args.salt}][SESSION-{sid}] Repository audit log for project {sid}. Reference notes:\n"
body = " ".join(f"{args.salt}s{sid}-item{i}-{(i*7919+sid*104729+len(args.salt)*31) % 100003}" for i in range(args.prefix_words))
prefix = head + body

def call(url, turn, ep):
    msg = prefix + f"\n\nTurn {turn}: reply exactly S{sid}T{turn}_OK /no_think"
    b = json.dumps({"model": args.model, "messages": [{"role": "user", "content": msg}],
                    "max_tokens": 24, "temperature": 0}).encode()
    req = urllib.request.Request(url, data=b, headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        d = json.load(urllib.request.urlopen(req, timeout=600))
        c = (d["choices"][0]["message"].get("content") or "").strip()
        return {"session": sid, "turn": turn, "endpoint": ep, "latency_s": round(time.time()-t0, 3), "ok": True,
                "prompt_tokens": d.get("usage", {}).get("prompt_tokens"), "marker_ok": f"S{sid}T{turn}_OK" in c}
    except Exception as e:
        return {"session": sid, "turn": turn, "endpoint": ep, "latency_s": round(time.time()-t0, 3), "ok": False,
                "error": f"{type(e).__name__}: {str(e)[:120]}"}

rows = []
for t in range(args.turns_a):
    r = call(args.url_a, t, "A"); rows.append(r); print(json.dumps(r), flush=True)
if args.hook:
    import subprocess
    t0 = time.time(); rc = subprocess.call(args.hook, shell=True)
    print(json.dumps({"hook": args.hook, "rc": rc, "seconds": round(time.time()-t0, 3)}), flush=True)
    if rc != 0:
        # spec §9: a failed migration hook fails the cell; never run B on an unverified import
        with open(args.out, "w") as f:
            for r in rows: f.write(json.dumps(r) + "\n")
            f.write(json.dumps({"cell_invalid": True, "reason": f"hook rc={rc}"}) + "\n")
        print(f"\n=== CELL_INVALID: hook failed rc={rc}; B turns skipped | out={args.out}")
        raise SystemExit(1)
if args.gap_s > 0:
    time.sleep(args.gap_s)
for t in range(args.turns_a, args.turns_a + args.turns_b):
    r = call(args.url_b, t, "B"); rows.append(r); print(json.dumps(r), flush=True)

with open(args.out, "w") as f:
    for r in rows:
        f.write(json.dumps(r) + "\n")
ok = sum(r["ok"] for r in rows)
bad = [r for r in rows if not r["ok"] or not r.get("marker_ok")]
print(f"\n=== {ok}/{len(rows)} ok, {len(bad)} bad (http error or marker mismatch) | A turns {args.turns_a} -> B turns {args.turns_b} | out={args.out}")
if bad:
    with open(args.out, "a") as f:
        f.write(json.dumps({"cell_invalid": True, "reason": f"{len(bad)} request(s) failed or wrong marker"}) + "\n")
    raise SystemExit(1)
