#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""G-D gap hook (network path): B reserves CPU slots for A's pinned prefix, A streams
the payload over TCP into B's receiver, B verifies/writes/commits, then the serial
cleanup gate runs.

Failure injection: --inject transfer stops the sender with an error marker after
the reservation (chunk 3), so B holds a partial payload + reservation and must abort
and discard both. Exit 0 only when the cleanup gate is clean."""
import argparse, sys, time
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
from hook_common import HookFailure, cleanup_and_verify, drain_b, finish, rpc, nudge, step, wait_stage, T0

P = argparse.ArgumentParser()
P.add_argument("--a-agent", default="192.168.5.61:7001")
P.add_argument("--b-agent", default="127.0.0.1:7002")
P.add_argument("--b-payload-host", default="192.168.5.62")
P.add_argument("--b-payload-port", type=int, default=7102)
P.add_argument("--a-url", default="http://localhost:8080/v1/chat/completions")
P.add_argument("--b-url", default="http://localhost:8081/v1/chat/completions")
P.add_argument("--model", default="Qwen/Qwen3-14B")
P.add_argument("--inject", default=None, choices=[None, "transfer"], help="failure injection after the reservation")
P.add_argument("--cleanup-only", action="store_true")
args = P.parse_args()

STAGES = ["idle", "reserve_posted", "reserved", "payload_received", "written", "commit_posted", "committed"]
def wait(target, budget):
    return wait_stage(args.b_agent, "import_status", STAGES, target, budget, "B_import_failed")

if args.cleanup_only:
    drain_b(args)
    ok, cs = cleanup_and_verify(args)
    finish("cleanup_only", ok, cs); sys.exit(0 if ok else 6)

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
    ok, cs = cleanup_and_verify(args); finish("failed", ok, cs, reason=prep.get("error")); sys.exit(2)
try:
    step("B_nudge_1", latency_s=nudge(args.b_url, args.model, "reserve"))
    step("B_reserved", **wait("reserved", 60))
    req = {"cmd": "export", "host": args.b_payload_host, "port": args.b_payload_port}
    if args.inject == "transfer":
        req["inject_fail_chunk"] = 3
    exp = rpc(args.a_agent, req)
    step("A_export", **{k: v for k, v in exp.items() if k != "events"}, events=exp.get("events"))
    if not exp.get("ok"):
        raise HookFailure(3)
    step("B_written_commit_posted", **wait("commit_posted", 120))
    step("B_nudge_2", latency_s=nudge(args.b_url, args.model, "commit"))
    step("B_committed", **wait("committed", 60))
except HookFailure as e:
    transfer_s = round(time.time() - T0, 3)
    # B is either 'failed' (partial payload -> abort posted by the state machine) or
    # still 'reserved' (nothing arrived): ask it to abort, then drain and clean up
    st = rpc(args.b_agent, {"cmd": "import_status"}); step("B_status_after_failure", **st)
    if st.get("stage") in ("reserve_posted", "reserved"):
        step("B_import_abort", **rpc(args.b_agent, {"cmd": "import_abort"}))
    drain_b(args)
    ok, cs = cleanup_and_verify(args)
    finish("failed_as_injected" if args.inject else "failed", ok, cs, transfer_s)
    sys.exit(e.code)
transfer_s = round(time.time() - T0, 3)
b = rpc(args.b_agent, {"cmd": "status"})
step("B_status", stage=b.get("import_stage"), reservation_blocks=b.get("import_reservation_blocks"), leases=b.get("active_leases"), counters=b.get("counters"))
ok, cs = cleanup_and_verify(args)
finish("ok", ok, cs, transfer_s)
sys.exit(0 if ok else 6)
