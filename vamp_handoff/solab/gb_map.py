#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""G-B mapping: zip sequential runner rows with frontend router decisions.

Valid only when the runner issued requests strictly sequentially (concurrency 1)
so the i-th router decision in the cell window corresponds to the i-th runner row.
Reports per-session worker set (affinity), per-worker session count, and any
mismatch in row/decision counts (which invalidates the mapping, not the cell).
"""
import argparse, json, re, collections

P = argparse.ArgumentParser()
P.add_argument("--runner", required=True)
P.add_argument("--router", required=True)
a = P.parse_args()

rows = [json.loads(l) for l in open(a.runner) if l.strip()]
dec = []
for l in open(a.router):
    m = re.search(r"Selected worker: (\d+), logit: ([0-9.]+), cached blocks: (\d+)", l)
    if m: dec.append((m.group(1), float(m.group(2)), int(m.group(3))))

print(f"runner rows={len(rows)} router decisions={len(dec)}")
if len(rows) != len(dec):
    print("MAPPING_INVALID: count mismatch (concurrency>1 or extra requests in window)")
n = min(len(rows), len(dec))
sess_workers = collections.defaultdict(list)
worker_sessions = collections.defaultdict(set)
cached_by_turn = collections.defaultdict(list)
for r, (w, logit, cb) in zip(rows[:n], dec[:n]):
    sess_workers[r["session"]].append(w)
    worker_sessions[w].add(r["session"])
    cached_by_turn[r["turn"]].append(cb)

short = {w: f"W{i}" for i, w in enumerate(sorted(worker_sessions))}
print("\nper-session worker sequence (turn order):")
moved = 0
for s in sorted(sess_workers):
    seq = [short[w] for w in sess_workers[s]]
    sticky = len(set(seq)) == 1
    moved += 0 if sticky else 1
    print(f"  session {s:2d}: {' '.join(seq)}  {'sticky' if sticky else 'MOVED'}")
print(f"\nsessions that changed worker: {moved}/{len(sess_workers)}")
print("per-worker distinct sessions: " + ", ".join(f"{short[w]}({w[-4:]})={len(v)}" for w, v in sorted(worker_sessions.items())))
print("router 'cached blocks' by turn (router-side view): " +
      ", ".join(f"t{t}:{sorted(set(v))}" for t, v in sorted(cached_by_turn.items())))
