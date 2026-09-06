# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""Shared pieces of the gap hooks (gd_hook = network path, gf_hook = CXL path).

The cleanup gate is strict (review 2026-09-06): both cleanup RPCs must return ok,
and afterwards neither side may hold a lease, a candidate, a CXL export, a network
or CXL import in any stage but idle, a received payload buffer, or a queued payload.
Timings are reported separately: ``transfer_s`` (hook start -> destination
committed / failed) and ``cleanup_s`` (the serial release + verification)."""
import json
import socket
import time
import urllib.request

T0 = time.time()


def rpc(addr, req, timeout=900):
    host, port = addr.split(":")
    with socket.create_connection((host, int(port)), timeout=timeout) as s:
        s.sendall((json.dumps(req) + "\n").encode())
        return json.loads(s.makefile("rb").readline())


def nudge(url, model, tag):
    b = json.dumps({"model": model, "messages": [{"role": "user", "content": f"nudge-{tag} /no_think"}], "max_tokens": 2}).encode()
    r = urllib.request.Request(url, data=b, headers={"Content-Type": "application/json"})
    t0 = time.time(); urllib.request.urlopen(r, timeout=120).read(); return round(time.time() - t0, 3)


def step(name, **kw):
    kw["step"] = name; kw["t"] = round(time.time() - T0, 3); print(json.dumps(kw), flush=True)


class HookFailure(SystemExit):
    pass


def wait_stage(agent, cmd, stages, target, budget, fail_step):
    """Poll ``cmd`` until the stage is at or beyond ``target`` (one status call may
    advance several stages). Stage 'failed' raises HookFailure(3), timeout HookFailure(4)."""
    t0 = time.time()
    while time.time() - t0 < budget:
        st = rpc(agent, {"cmd": cmd})
        stage = st.get("stage")
        if stage == "failed":
            step(fail_step, **st); raise HookFailure(3)
        if stage in stages and stages.index(stage) >= stages.index(target):
            return st
        time.sleep(0.2)
    step("timeout_waiting", target=target); raise HookFailure(4)


def gate_checks(a, b, sa, sb):
    return {
        "A_cleanup_ok": bool(a.get("ok")), "B_cleanup_ok": bool(b.get("ok")),
        "A_leases_0": sa.get("active_leases") == 0, "B_leases_0": sb.get("active_leases") == 0,
        "A_no_candidate": not sa.get("candidate_blocks"), "B_no_candidate": not sb.get("candidate_blocks"),
        "A_no_cxl_export_held": sa.get("cxl_export_held") is None,
        "B_net_import_idle": sb.get("import_stage") == "idle",
        "B_cxl_import_idle": sb.get("cxl_import_stage") == "idle",
        "B_no_payload_buffer": sb.get("payload_received") is None,
        "B_receive_queue_empty": sb.get("received_queue") in (0, None),
    }


def cleanup_and_verify(args, import_done=True):
    """Serial release: destination first (import state, received buffers), then the
    source (CXL lock/key/payload/hashes, export lease release posted), nudge A so the
    release drains, then verify both sides. Returns (ok, cleanup_s)."""
    t0 = time.time()
    b = rpc(args.b_agent, {"cmd": "cleanup", "scope": "import"}); step("B_cleanup", **b)
    if not b.get("ok"):
        # review 2026-09-06: the destination may still be reading the shared payload; never
        # release A's lock/key/payload under it. Preserve A and fail the gate (operator
        # recovery: drain B, then --cleanup-only).
        sa = rpc(args.a_agent, {"cmd": "status"}); sb = rpc(args.b_agent, {"cmd": "status"})
        cleanup_s = round(time.time() - t0, 3)
        step("CLEANUP_GATE", ok=False, cleanup_s=cleanup_s, failed_checks=["B_cleanup_ok"],
             note="A cleanup skipped, A resources preserved", A_cxl_export_held=sa.get("cxl_export_held"),
             A_leases=sa.get("active_leases"), B_import_stage=sb.get("import_stage"), B_cxl_import_stage=sb.get("cxl_import_stage"))
        return False, cleanup_s
    a = rpc(args.a_agent, {"cmd": "cleanup", "scope": "export", "confirmed": import_done}); step("A_cleanup", **a)
    step("A_nudge_release", latency_s=nudge(args.a_url, args.model, "release"))
    sa = rpc(args.a_agent, {"cmd": "status"}); sb = rpc(args.b_agent, {"cmd": "status"})
    checks = gate_checks(a, b, sa, sb)
    ok = all(checks.values())
    cleanup_s = round(time.time() - t0, 3)
    step("CLEANUP_GATE", ok=ok, cleanup_s=cleanup_s, failed_checks=[k for k, v in checks.items() if not v],
         A_counters=sa.get("counters"), B_counters=sb.get("counters"))
    return ok, cleanup_s


def drain_b(args, tag="abort"):
    step(f"B_nudge_{tag}", latency_s=nudge(args.b_url, args.model, tag))


def finish(kind, ok, cleanup_s, transfer_s=None, **extra):
    out = {"hook": kind, "cleanup_gate_ok": ok, "cleanup_s": cleanup_s, "total_s": round(time.time() - T0, 3)}
    if transfer_s is not None:
        out["transfer_s"] = transfer_s
    out.update(extra)
    print(json.dumps(out), flush=True)
