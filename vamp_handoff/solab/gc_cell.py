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
P.add_argument("--gap-s", type=float, default=0.0, help="next-turn arrival at the controller = (A last response complete) + gap; same in every arm")
P.add_argument("--hook", default="", help="shell command run during the gap (e.g. trigger the export/import)")
P.add_argument("--post-hook", default="", help="shell command run after the B turns (e.g. release A's pin); rc!=0 -> cell invalid")
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
    t0 = time.monotonic()
    try:
        d = json.load(urllib.request.urlopen(req, timeout=600))
        c = (d["choices"][0]["message"].get("content") or "").strip()
        return {"session": sid, "turn": turn, "endpoint": ep, "latency_s": round(time.monotonic()-t0, 3), "ok": True,
                "prompt_tokens": d.get("usage", {}).get("prompt_tokens"), "marker_ok": f"S{sid}T{turn}_OK" in c}
    except Exception as e:
        return {"session": sid, "turn": turn, "endpoint": ep, "latency_s": round(time.monotonic()-t0, 3), "ok": False,
                "error": f"{type(e).__name__}: {str(e)[:120]}"}

rows = []
for t in range(args.turns_a):
    r = call(args.url_a, t, "A"); rows.append(r); print(json.dumps(r), flush=True)
# Gap protocol (review 2026-09-06): t0 = A's last response complete. The hook (export ->
# import -> cleanup) starts in the background at t0. The next turn "arrives" at the
# controller at t0 + gap in every arm. WAIT policy: an arm with a hook dispatches to B
# only after the hook finished successfully (cleanup included); B0 dispatches at arrival.
# Primary metric = arrival -> response complete (waiting is part of the cost).
# Clocks (review 2026-09-06): deadlines and elapsed times use the monotonic clock; the
# metric is measured from the *scheduled* arrival t0+gap, not from the runner's wakeup
# (the wakeup lag is recorded separately as wakeup_late_s). The hook end is observed by
# polling, so hook_wall_s has HOOK_POLL_S granularity.
HOOK_POLL_S = 0.01
t0_wall = time.time(); t0 = time.monotonic()
hook_proc = None
if args.hook:
    import subprocess
    hook_proc = subprocess.Popen(args.hook, shell=True)
arrival = t0 + args.gap_s                     # scheduled arrival of the next turn (same in every arm)
hook_done = None
while time.monotonic() < arrival:
    if hook_proc is not None and hook_done is None and hook_proc.poll() is not None:
        hook_done = time.monotonic()
    time.sleep(min(HOOK_POLL_S, max(0.0, arrival - time.monotonic())))
wakeup = time.monotonic()
hook_rc, hook_wall = None, None
if hook_proc is not None:
    while hook_proc.poll() is None:            # WAIT policy: dispatch only after the hook succeeded
        time.sleep(HOOK_POLL_S)
    hook_rc = hook_proc.returncode
    if hook_done is None:
        hook_done = time.monotonic()
    hook_wall = round(hook_done - t0, 3)       # observed with HOOK_POLL_S granularity
    print(json.dumps({"hook": args.hook, "rc": hook_rc, "seconds": hook_wall, "started_at": "t0", "poll_s": HOOK_POLL_S}), flush=True)
    if hook_rc != 0:
        # spec §9: a failed migration hook fails the cell; never run B on an unverified import
        with open(args.out, "w") as f:
            for r in rows: f.write(json.dumps(r) + "\n")
            f.write(json.dumps({"cell_invalid": True, "reason": f"hook rc={hook_rc}"}) + "\n")
        print(f"\n=== CELL_INVALID: hook failed rc={hook_rc}; B turns skipped | out={args.out}")
        raise SystemExit(1)
dispatch = time.monotonic()
timeline = {"timeline": True, "gap_s": args.gap_s, "t0_wall": round(t0_wall, 3), "clock": "monotonic",
            "scheduled_arrival_after_t0_s": args.gap_s, "wakeup_late_s": round(wakeup - arrival, 4),
            "hook_wall_s": hook_wall, "hook_poll_s": HOOK_POLL_S,
            "wait_s": round(dispatch - arrival, 3), "dispatch_after_t0_s": round(dispatch - t0, 3)}
for i, t in enumerate(range(args.turns_a, args.turns_a + args.turns_b)):
    r = call(args.url_b, t, "B")
    if i == 0:
        r["gap_s"] = args.gap_s; r["wait_s"] = timeline["wait_s"]
        r["arrival_to_done_s"] = round(time.monotonic() - arrival, 3)   # from the scheduled arrival
        timeline["first_b_arrival_to_done_s"] = r["arrival_to_done_s"]; timeline["first_b_http_latency_s"] = r["latency_s"]
    rows.append(r); print(json.dumps(r), flush=True)
print(json.dumps(timeline), flush=True)
rows.append(timeline)

with open(args.out, "w") as f:
    for r in rows:
        f.write(json.dumps(r) + "\n")
if args.post_hook:
    # e.g. the B0 arm releases A's auto-pin here so no arm ends with a held lease
    import subprocess
    t0 = time.monotonic(); rc = subprocess.call(args.post_hook, shell=True)
    print(json.dumps({"post_hook": args.post_hook, "rc": rc, "seconds": round(time.monotonic()-t0, 3)}), flush=True)
    if rc != 0:
        with open(args.out, "a") as f:
            f.write(json.dumps({"cell_invalid": True, "reason": f"post_hook rc={rc}"}) + "\n")
        print(f"\n=== CELL_INVALID: post-hook failed rc={rc} | out={args.out}")
        raise SystemExit(1)
turns = [r for r in rows if "turn" in r]
ok = sum(r["ok"] for r in turns)
bad = [r for r in turns if not r["ok"] or not r.get("marker_ok")]
print(f"\n=== {ok}/{len(turns)} ok, {len(bad)} bad (http error or marker mismatch) | A turns {args.turns_a} -> B turns {args.turns_b} | gap {args.gap_s}s | out={args.out}")
if bad:
    with open(args.out, "a") as f:
        f.write(json.dumps({"cell_invalid": True, "reason": f"{len(bad)} request(s) failed or wrong marker"}) + "\n")
    raise SystemExit(1)
