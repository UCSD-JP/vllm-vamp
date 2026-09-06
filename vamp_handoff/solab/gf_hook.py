#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""G-F gap hook (CXL path): A exports its pinned prefix into the shared CXL slice,
B imports it into its CPU tier, then the serial cleanup gate runs.

Failure injection: --inject wrong_key (B looks up a missing key; nothing reserved)
or --inject checksum (B's expected digest is corrupted after the reservation, so the
post-refresh verify fails and the abort/cleanup path runs). --cleanup-only is the
operator recovery after an aborted cell. Exit 0 only when the cleanup gate is clean."""
import argparse, sys, time
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
from hook_common import HookFailure, cleanup_and_verify, drain_b, finish, rpc, nudge, step, wait_stage, T0

P = argparse.ArgumentParser()
P.add_argument("--a-agent", default="192.168.5.61:7001")
P.add_argument("--b-agent", default="127.0.0.1:7002")
P.add_argument("--a-url", default="http://localhost:8080/v1/chat/completions")
P.add_argument("--b-url", default="http://localhost:8081/v1/chat/completions")
P.add_argument("--model", default="Qwen/Qwen3-14B")
P.add_argument("--key", default="VAMP_KV_gf")
P.add_argument("--inject", default=None, choices=[None, "wrong_key", "checksum"], help="failure injection for the cleanup gate")
P.add_argument("--cleanup-only", action="store_true", help="skip export/import; only drain B's pending abort and run the serial release + gate")
args = P.parse_args()

STAGES = ["idle", "reserve_posted", "reserved", "commit_posted", "committed"]
def wait(target, budget):
    return wait_stage(args.b_agent, "cxl_import_status", STAGES, target, budget, "B_cxl_import_failed")

if args.cleanup_only:
    drain_b(args)
    ok, cs = cleanup_and_verify(args)
    finish("cleanup_only", ok, cs); sys.exit(0 if ok else 6)

a = rpc(args.a_agent, {"cmd": "status"})
step("A_status", candidate_blocks=a.get("candidate_blocks"), leases=a.get("active_leases"))
if not a.get("candidate_blocks"):
    step("abort", reason="A has no pinned candidate"); sys.exit(2)
exp = rpc(args.a_agent, {"cmd": "cxl_export", "key": args.key})
step("A_cxl_export", **exp)
if not exp.get("ok"):
    ok, cs = cleanup_and_verify(args); finish("failed", ok, cs, reason="export failed"); sys.exit(3)
key_for_b = args.key + "_MISSING" if args.inject == "wrong_key" else args.key
prep = rpc(args.b_agent, {"cmd": "cxl_import_prepare", "key": key_for_b, "inject": args.inject if args.inject == "checksum" else None})
step("B_cxl_import_prepare", **prep)
if not prep.get("ok"):
    # wrong key: nothing was reserved at B; A still holds everything -> release it
    transfer_s = round(time.time() - T0, 3)
    ok, cs = cleanup_and_verify(args)
    finish("failed_as_injected" if args.inject else "failed", ok, cs, transfer_s, reason=prep.get("error")); sys.exit(3)
try:
    step("B_nudge_1", latency_s=nudge(args.b_url, args.model, "reserve"))
    # one status call may run reserved -> refresh/sha256 -> failed, so even the wait for
    # "reserved" can observe the injected checksum failure: everything stays inside the try
    step("B_reserved", **wait("reserved", 60))
    step("B_commit_posted", **wait("commit_posted", 600))   # refresh + sha256 + CXL->CPU copy happen here
    step("B_nudge_2", latency_s=nudge(args.b_url, args.model, "commit"))
    step("B_committed", **wait("committed", 60))
except HookFailure as e:
    # failed import (injected checksum or real): the abort was posted; drain it, then clean up
    transfer_s = round(time.time() - T0, 3)
    drain_b(args)
    ok, cs = cleanup_and_verify(args)
    finish("failed_as_injected" if args.inject else "failed", ok, cs, transfer_s)
    sys.exit(e.code)
transfer_s = round(time.time() - T0, 3)
ok, cs = cleanup_and_verify(args)
finish("ok", ok, cs, transfer_s, key=args.key)
sys.exit(0 if ok else 6)
