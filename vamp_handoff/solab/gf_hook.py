#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""G-F gap hook: A -> CXL -> B for the pinned prefix run.

  1. A status (pinned candidate)             4. nudge B (reserve drains)
  2. A cxl_export(key): gather -> payload     5. B cxl_import_status: refresh -> sha256 ->
     arena -> fence -> record under lock         zero-copy import into CPU tier -> commit posted
  3. B cxl_import_prepare(key): record+hashes 6. nudge B -> committed
     under the entry lock -> reserve posted
Any failure exits non-zero so the cell aborts (no silent fallback).
"""
import argparse, json, socket, sys, time, urllib.request

P = argparse.ArgumentParser()
P.add_argument("--a-agent", default="192.168.5.61:7001")
P.add_argument("--b-agent", default="127.0.0.1:7002")
P.add_argument("--b-url", default="http://localhost:8081/v1/chat/completions")
P.add_argument("--model", default="Qwen/Qwen3-14B")
P.add_argument("--key", default=f"VAMP_KV_{int(time.time())}")
args = P.parse_args()

def rpc(addr, req, timeout=900):
    host, port = addr.split(":")
    with socket.create_connection((host, int(port)), timeout=timeout) as s:
        s.sendall((json.dumps(req) + "\n").encode()); return json.loads(s.makefile("rb").readline())

def nudge(tag):
    b = json.dumps({"model": args.model, "messages": [{"role": "user", "content": f"nudge-{tag} /no_think"}], "max_tokens": 2}).encode()
    r = urllib.request.Request(args.b_url, data=b, headers={"Content-Type": "application/json"})
    t0 = time.time(); urllib.request.urlopen(r, timeout=120).read(); return round(time.time() - t0, 3)

T0 = time.time()
def step(name, **kw):
    kw["step"] = name; kw["t"] = round(time.time() - T0, 3); print(json.dumps(kw), flush=True)

def wait_stage(target, budget):
    t0 = time.time()
    while time.time() - t0 < budget:
        st = rpc(args.b_agent, {"cmd": "cxl_import_status"})
        if st.get("stage") == target: return st
        if st.get("stage") == "failed": step("B_cxl_import_failed", **st); sys.exit(3)
        time.sleep(0.2)
    step("timeout_waiting", target=target); sys.exit(4)

a = rpc(args.a_agent, {"cmd": "status"})
step("A_status", candidate_blocks=a.get("candidate_blocks"), leases=a.get("active_leases"))
if not a.get("candidate_blocks"): step("abort", reason="A has no pinned candidate"); sys.exit(2)
exp = rpc(args.a_agent, {"cmd": "cxl_export", "key": args.key})
step("A_cxl_export", **exp)
if not exp.get("ok"): sys.exit(3)
prep = rpc(args.b_agent, {"cmd": "cxl_import_prepare", "key": args.key})
step("B_cxl_import_prepare", **prep)
if not prep.get("ok"): sys.exit(3)
step("B_nudge_1", latency_s=nudge("reserve"))
step("B_reserved", **wait_stage("reserved", 60))
step("B_commit_posted", **wait_stage("commit_posted", 600))   # refresh + sha256 + CXL->CPU copy happen here
step("B_nudge_2", latency_s=nudge("commit"))
step("B_committed", **wait_stage("committed", 60))
print(json.dumps({"hook": "ok", "key": args.key, "total_s": round(time.time() - T0, 3)}), flush=True)
