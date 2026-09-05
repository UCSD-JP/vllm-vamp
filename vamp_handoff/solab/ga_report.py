# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
#!/usr/bin/env python3
"""G-A/G-B cell report: joins runner rows, worker sidecar (Σ delta), and probe events.

usage: ga_report.py --cell <name> --runner <jsonl> --wstats <jsonl> [--probe <jsonl>]
"""
import argparse, json, statistics as st

P = argparse.ArgumentParser()
P.add_argument("--cell", required=True)
P.add_argument("--runner", required=True)
P.add_argument("--wstats", required=True)
P.add_argument("--probe", default=None)
a = P.parse_args()

def rows(p):
    out = []
    for line in open(p):
        line = line.strip()
        if not line: continue
        try: out.append(json.loads(line))
        except json.JSONDecodeError: pass
    return out

R = rows(a.runner)
by_turn = {}
for r in R:
    by_turn.setdefault(r["turn"], []).append(r)
print(f"=== cell {a.cell} ===")
print(f"runner: {sum(r['ok'] for r in R)}/{len(R)} ok, sessions={len({r['session'] for r in R})}, turns={len(by_turn)}")
for t in sorted(by_turn):
    L = [r["latency_s"] for r in by_turn[t] if r["ok"]]
    bad = [r for r in by_turn[t] if not r["ok"]]
    pt = {r.get("usage", {}).get("prompt_tokens") or r.get("prompt_tokens") for r in by_turn[t] if r["ok"]}
    ct = {(r.get("content") or "")[:24] for r in by_turn[t] if r["ok"]}
    print(f"  turn {t}: n={len(L)} p50={st.median(L) if L else float('nan'):.3f}s max={max(L) if L else float('nan'):.3f}s"
          f" prompt_tokens={sorted(pt)} content={sorted(ct)}" + (f" ERR={len(bad)}" if bad else ""))

W = rows(a.wstats)
h=q=ch=cq=0; maxu=0.0; maxrun=0; maxwait=0
for d in W:
    h+=d.get("hits") or 0; q+=d.get("queries") or 0
    ch+=d.get("conn_hits") or 0; cq+=d.get("conn_queries") or 0
    maxu=max(maxu, d.get("usage") or 0.0); maxrun=max(maxrun, d.get("running") or 0); maxwait=max(maxwait, d.get("waiting") or 0)
print(f"sidecar: records={len(W)}")
print(f"  GPU prefix : hits={h:,} queries={q:,} rate={100*h/q if q else 0:.1f}%")
print(f"  connector  : hits={ch:,} queries={cq:,} rate={100*ch/cq if cq else 0:.1f}%  (None-> connector off)" if cq or ch else "  connector  : no connector stats (offload OFF or not reported)")
print(f"  peak_kv_usage={maxu:.3f} peak_running={maxrun} peak_waiting={maxwait}")

if a.probe:
    Pv = rows(a.probe)
    ev = {}
    for d in Pv: ev[d["event"]] = ev.get(d["event"], 0) + 1
    last = Pv[-1] if Pv else {}
    print(f"probe: events={ev}")
    for d in Pv:
        if d["event"] in ("manager_created", "handlers_registered", "listener_registered"):
            print("  " + json.dumps({k: v for k, v in d.items() if k not in ('ts','tid')}))
    rb = sum(d.get("n_blocks", 0) for d in Pv if d["event"] == "ready")
    eb = sum(d.get("n_blocks", 0) for d in Pv if d["event"] == "evicted")
    print(f"  ready_blocks_total={rb} evicted_blocks_total={eb} last_counters={last.get('counters')} active_leases={last.get('active_leases')}")
