#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""G-D gap hook: drive A's pinned prefix into B's CPU tier before B's first turn.

Steps (each recorded with timing; any failure exits non-zero so the cell aborts):
  1. A status  -> pinned candidate (hashes, block count)
  2. B import_prepare(hashes)  -> reserve posted to B's mailbox
  3. nudge B (tiny request) so the idle engine runs a step and drains -> reserved
  4. A export -> B payload port (bridge.gather -> TCP -> PayloadReceiver)
  5. B import_status advances: payload_received -> written -> commit posted
  6. nudge B again -> committed
"""
import argparse, json, socket, sys, time, urllib.request

P = argparse.ArgumentParser()
P.add_argument("--a-agent", default="192.168.5.61:7001")
P.add_argument("--b-agent", default="127.0.0.1:7002")
P.add_argument("--b-payload-host", default="192.168.5.62")
P.add_argument("--b-payload-port", type=int, default=7102)
P.add_argument("--b-url", default="http://localhost:8081/v1/chat/completions")
P.add_argument("--a-url", default="http://localhost:8080/v1/chat/completions")
P.add_argument("--model", default="Qwen/Qwen3-14B")
P.add_argument("--timeout-s", type=float, default=600)
args = P.parse_args()

def rpc(addr, req, timeout=650):
    host, port = addr.split(":")
    with socket.create_connection((host, int(port)), timeout=timeout) as s:
        s.sendall((json.dumps(req) + "\n").encode())
        f = s.makefile("rb")
        return json.loads(f.readline())

def nudge(tag, url=None):
    b = json.dumps({"model": args.model, "messages": [{"role": "user", "content": f"nudge-{tag} /no_think"}], "max_tokens": 2}).encode()
    r = urllib.request.Request(url or args.b_url, data=b, headers={"Content-Type": "application/json"})
    t0 = time.time(); urllib.request.urlopen(r, timeout=120).read(); return round(time.time() - t0, 3)

def step(name, **kw):
    kw["step"] = name; kw["t"] = round(time.time() - T0, 3); print(json.dumps(kw), flush=True)

def cleanup_and_verify():
    """Same serial release as gf_hook: B import finished/aborted -> B cleanup -> A cleanup
    (no CXL objects on the network path; the export lease release is posted) -> nudge A
    so the release drains -> verify nothing is pinned or reserved on either side."""
    b = rpc(args.b_agent, {"cmd": "cleanup", "scope": "import"}); step("B_cleanup", **b)
    a = rpc(args.a_agent, {"cmd": "cleanup", "scope": "export", "confirmed": True}); step("A_cleanup", **a)
    step("A_nudge_release", latency_s=nudge("release", args.a_url))
    sa = rpc(args.a_agent, {"cmd": "status"}); sb = rpc(args.b_agent, {"cmd": "status"})
    residual = {"A_leases": sa.get("active_leases"), "A_candidate": sa.get("candidate_blocks"),
                "B_leases": sb.get("active_leases"), "B_candidate": sb.get("candidate_blocks"), "B_stage": sb.get("import_stage"),
                "A_counters": sa.get("counters"), "B_counters": sb.get("counters")}
    clean = (sa.get("active_leases") == 0 and sb.get("active_leases") == 0 and not sa.get("candidate_blocks") and not sb.get("candidate_blocks"))
    step("CLEANUP_GATE", ok=clean, **residual)
    return clean

def wait_stage(target, budget):
    t0 = time.time()
    while time.time() - t0 < budget:
        st = rpc(args.b_agent, {"cmd": "import_status"})
        if st.get("stage") == target: return st
        if st.get("stage") == "failed": step("B_import_failed", **st); sys.exit(3)
        time.sleep(0.2)
    step("timeout_waiting", target=target); sys.exit(4)

T0 = time.time()
a = rpc(args.a_agent, {"cmd": "status"})
step("A_status", candidate_blocks=a.get("candidate_blocks"), head=a.get("candidate_hashes_head"), leases=a.get("active_leases"))
if not a.get("candidate_blocks"):
    step("abort", reason="A has no pinned candidate"); sys.exit(2)
hashes_resp = rpc(args.a_agent, {"cmd": "hashes"})
if not hashes_resp.get("ok"):
    step("abort", reason="A did not return hashes", resp=hashes_resp); sys.exit(2)
hashes = hashes_resp["hashes"]
step("A_hashes", n=len(hashes), head=[h[:16] for h in hashes[:3]])

prep = rpc(args.b_agent, {"cmd": "import_prepare", "hashes": hashes})
step("B_import_prepare", **prep)
if not prep.get("ok"):
    cleanup_and_verify(); sys.exit(2)
try:
    step("B_nudge_1", latency_s=nudge("reserve"))
    res = wait_stage("reserved", 60)
    step("B_reserved", **res)

    exp = rpc(args.a_agent, {"cmd": "export", "host": args.b_payload_host, "port": args.b_payload_port})
    step("A_export", **{k: v for k, v in exp.items() if k != "events"}, events=exp.get("events"))
    if not exp.get("ok"): sys.exit(3)
    res = wait_stage("commit_posted", 120)
    step("B_written_commit_posted", **res)
    step("B_nudge_2", latency_s=nudge("commit"))
    res = wait_stage("committed", 60)
    step("B_committed", **res)
except SystemExit:
    # a failed or timed-out import: the abort (if any) was posted; drain it, then release
    step("B_nudge_abort", latency_s=nudge("abort"))
    ok = cleanup_and_verify()
    print(json.dumps({"hook": "failed", "cleanup_gate_ok": ok}), flush=True)
    raise
b = rpc(args.b_agent, {"cmd": "status"})
step("B_status", stage=b.get("import_stage"), reservation_blocks=b.get("import_reservation_blocks"), leases=b.get("active_leases"), counters=b.get("counters"))
ok = cleanup_and_verify()
print(json.dumps({"hook": "ok", "total_s": round(time.time() - T0, 3), "cleanup_gate_ok": ok}), flush=True)
sys.exit(0 if ok else 6)
