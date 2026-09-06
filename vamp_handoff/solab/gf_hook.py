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
P.add_argument("--a-url", default="http://localhost:8080/v1/chat/completions")
P.add_argument("--inject", default=None, choices=[None, "wrong_key", "checksum"], help="failure injection for the cleanup gate")
P.add_argument("--cleanup-only", action="store_true", help="skip export/import; only drain B's pending abort and run the serial release + gate")
args = P.parse_args()

def rpc(addr, req, timeout=900):
    host, port = addr.split(":")
    with socket.create_connection((host, int(port)), timeout=timeout) as s:
        s.sendall((json.dumps(req) + "\n").encode()); return json.loads(s.makefile("rb").readline())

def nudge(tag, url=None):
    b = json.dumps({"model": args.model, "messages": [{"role": "user", "content": f"nudge-{tag} /no_think"}], "max_tokens": 2}).encode()
    r = urllib.request.Request(url or args.b_url, data=b, headers={"Content-Type": "application/json"})
    t0 = time.time(); urllib.request.urlopen(r, timeout=120).read(); return round(time.time() - t0, 3)

def cleanup_and_verify(import_done: bool):
    """Serial release: B import finished/aborted -> B cleanup -> A cleanup (lock, key,
    payload, hash blob; lease release posted) -> nudge A so the release drains -> verify."""
    b = rpc(args.b_agent, {"cmd": "cleanup", "scope": "import"}); step("B_cleanup", **b)
    a = rpc(args.a_agent, {"cmd": "cleanup", "scope": "export", "confirmed": import_done}); step("A_cleanup", **a)
    step("A_nudge_release", latency_s=nudge("release", args.a_url))
    sa = rpc(args.a_agent, {"cmd": "status"}); sb = rpc(args.b_agent, {"cmd": "status"})
    residual = {"A_leases": sa.get("active_leases"), "A_candidate": sa.get("candidate_blocks"),
                "B_leases": sb.get("active_leases"), "B_candidate": sb.get("candidate_blocks"), "B_stage": sb.get("import_stage"),
                "A_counters": sa.get("counters"), "B_counters": sb.get("counters")}
    clean = (sa.get("active_leases") == 0 and sb.get("active_leases") == 0 and not sa.get("candidate_blocks") and not sb.get("candidate_blocks"))
    step("CLEANUP_GATE", ok=clean, **residual)
    return clean

T0 = time.time()
def step(name, **kw):
    kw["step"] = name; kw["t"] = round(time.time() - T0, 3); print(json.dumps(kw), flush=True)

STAGES = ["idle", "reserve_posted", "reserved", "commit_posted", "committed"]

def wait_stage(target, budget):
    # one status call may advance several stages (reserved -> refresh/sha256/copy ->
    # commit_posted), so accept any stage at or beyond the target
    t0 = time.time()
    while time.time() - t0 < budget:
        st = rpc(args.b_agent, {"cmd": "cxl_import_status"})
        stage = st.get("stage")
        if stage == "failed": step("B_cxl_import_failed", **st); sys.exit(3)
        if stage in STAGES and STAGES.index(stage) >= STAGES.index(target): return st
        time.sleep(0.2)
    step("timeout_waiting", target=target); sys.exit(4)

if args.cleanup_only:
    # operator recovery after an aborted cell: drain B's pending abort, then the serial release
    step("B_nudge_abort", latency_s=nudge("abort"))
    ok = cleanup_and_verify(import_done=True)
    print(json.dumps({"hook": "cleanup_only", "cleanup_gate_ok": ok}), flush=True)
    sys.exit(0 if ok else 6)

a = rpc(args.a_agent, {"cmd": "status"})
step("A_status", candidate_blocks=a.get("candidate_blocks"), leases=a.get("active_leases"))
if not a.get("candidate_blocks"): step("abort", reason="A has no pinned candidate"); sys.exit(2)
exp = rpc(args.a_agent, {"cmd": "cxl_export", "key": args.key})
step("A_cxl_export", **exp)
if not exp.get("ok"): sys.exit(3)
key_for_b = args.key + "_MISSING" if args.inject == "wrong_key" else args.key
prep = rpc(args.b_agent, {"cmd": "cxl_import_prepare", "key": key_for_b, "inject": args.inject if args.inject == "checksum" else None})
step("B_cxl_import_prepare", **prep)
if not prep.get("ok"):
    # wrong key: nothing was reserved at B; A still holds everything -> release it
    ok = cleanup_and_verify(import_done=True)
    print(json.dumps({"hook": "failed_as_injected" if args.inject else "failed", "reason": prep.get("error"), "cleanup_gate_ok": ok}), flush=True)
    sys.exit(3)
try:
    step("B_nudge_1", latency_s=nudge("reserve"))
    # one status call may run reserved -> refresh/sha256 -> failed, so the wait for
    # "reserved" itself can observe the injected checksum failure: keep it inside the try
    step("B_reserved", **wait_stage("reserved", 60))
    step("B_commit_posted", **wait_stage("commit_posted", 600))   # refresh + sha256 + CXL->CPU copy happen here
    step("B_nudge_2", latency_s=nudge("commit"))
    step("B_committed", **wait_stage("committed", 60))
except SystemExit as e:
    # injected checksum failure lands here (stage 'failed' -> exit 3): abort was posted; drain it, then clean up
    step("B_nudge_abort", latency_s=nudge("abort"))
    ok = cleanup_and_verify(import_done=True)
    print(json.dumps({"hook": "failed_as_injected" if args.inject else "failed", "cleanup_gate_ok": ok}), flush=True)
    raise
ok = cleanup_and_verify(import_done=True)
print(json.dumps({"hook": "ok", "key": args.key, "total_s": round(time.time() - T0, 3), "cleanup_gate_ok": ok}), flush=True)
sys.exit(0 if ok else 6)
