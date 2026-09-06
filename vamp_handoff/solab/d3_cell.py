# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""One-session D3 or recompute cell. Requires cold workers per cell.

Control operations are posted before a tiny request wakes the idle EngineCore.
All nudge wall time and checksum/readback time remain in total preparation.
Failure injection skips the B measured request, then cleans B before A.
"""

import argparse
import json
import socket
import time
import urllib.request


def rpc(host, port, cmd):
    with socket.create_connection((host, port), timeout=10) as sock:
        sock.settimeout(20)
        sock.sendall(json.dumps(cmd).encode() + b"\n")
        result = json.loads(sock.makefile("rb").readline())
    if not result.get("ok"):
        raise RuntimeError(result)
    return result


def request(url, text, tokens=24):
    body = json.dumps(
        dict(
            model="Qwen/Qwen3-14B",
            messages=[dict(role="user", content=text)],
            max_tokens=tokens,
            temperature=0,
        )
    ).encode()
    t0 = time.monotonic()
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=600) as response:
        result = json.load(response)
    return dict(
        latency_s=time.monotonic() - t0,
        usage=result.get("usage"),
        content=result["choices"][0]["message"].get("content"),
    )


def job(host, port, url, cmd):
    started = time.monotonic()
    task = rpc(host, port, cmd)
    nudge = request(url, "D3-control-wakeup /no_think", tokens=2)
    while time.monotonic() - started < 600:
        result = rpc(host, port, dict(cmd="job", id=task["id"]))
        if result["done"]:
            return dict(
                result["result"],
                wall_with_nudge_s=time.monotonic() - started,
                nudge_latency_s=nudge["latency_s"],
            )
        time.sleep(0.01)
    raise TimeoutError("D3 scheduler job did not complete")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--arm", choices=["D3", "B0"], default="D3")
    parser.add_argument("--words", type=int, default=1000)
    parser.add_argument("--salt", required=True)
    parser.add_argument("--inject", choices=["checksum"])
    parser.add_argument(
        "--verify",
        choices=["sha256", "gpu64", "none"],
        default="sha256",
        help="sha256 = host readback baseline; gpu64 = per-block GPU sums (non-cryptographic); none = skipped",
    )
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    if args.inject and args.verify == "none":
        parser.error("checksum injection needs a verification mode")
    a = ("192.168.5.61", 7001, "http://localhost:8080/v1/chat/completions")
    b = ("localhost", 7002, "http://localhost:8081/v1/chat/completions")
    rows = []

    def emit(event, **data):
        row = dict(event=event, **data)
        rows.append(row)
        print(json.dumps(row), flush=True)
        with open(args.out, "a") as f:
            f.write(json.dumps(row) + "\n")

    with open(args.out, "x"):
        pass
    passed = False
    clean_b = clean_a = False
    try:
        emit(
            "config",
            **vars(args),
            A=rpc(*a[:2], dict(cmd="status")),
            B=rpc(*b[:2], dict(cmd="status")),
        )
        head = (
            f"[CELL-{args.salt}][SESSION-0] "
            "Repository audit log for project 0. Reference notes:\n"
        )
        body = " ".join(
            f"{args.salt}s0-item{i}-{(i * 7919 + len(args.salt) * 31) % 100003}"
            for i in range(args.words)
        )
        prompt = head + body + "\n\nTurn 0: reply exactly S0T0_OK /no_think"
        source = request(a[2], prompt)
        start = (
            time.monotonic()
        )  # gap=0 arrival at A response completion, including readiness nudge
        emit("A_cold", **source)
        assert "S0T0_OK" in (source["content"] or "")
        # Flush any delayed request-free callback and bind scheduler ownership.
        request(a[2], "D3-ready-wakeup /no_think", tokens=2)
        pre = rpc(*a[:2], dict(cmd="status"))
        assert pre["candidate_blocks"] > 0, pre
        if args.arm == "D3":
            key = "D3_" + args.salt
            ex = job(*a, dict(cmd="export", key=key, verify=args.verify))
            emit("export", **ex)
            assert ex["ok"], ex
            im = job(*b, dict(cmd="import", key=key, inject=args.inject))
            emit("import", **im)
            if im.get("ok"):
                assert im.get("verify") == args.verify, im
                # a skipped verification must never be reported as verified
                assert im.get("verified") == (args.verify != "none"), im
            if args.inject:
                assert not im["ok"], im
                status = rpc(*b[:2], dict(cmd="status"))
                aborts = [
                    r for r in status["records"] if r["event"] == "gpu_import_aborted"
                ]
                assert aborts and aborts[-1]["cached_after"] == 0, status
                emit(
                    "injection_gate", ok=True, B_request_skipped=True, abort=aborts[-1]
                )
                passed = True
                return
            assert im["ok"] and im["digest_match"], im
        prepare = time.monotonic() - start
        result = request(b[2], prompt)
        emit(
            "B_first",
            preparation_s=prepare,
            arrival_to_done_gap0_s=time.monotonic() - start,
            **result,
        )
        assert "S0T0_OK" in (result["content"] or "")
        status = rpc(*b[:2], dict(cmd="status"))
        hits = [
            r
            for r in status["records"]
            if r["event"] == "gpu_prefix_lookup" and r["prompt_tokens"] > 1000
        ]
        assert hits, status
        expected = (
            pre["candidate_blocks"] * pre["block_tokens"] if args.arm == "D3" else 0
        )
        assert hits[-1]["gpu_hit_tokens"] == expected, (hits[-1], expected)
        emit("reuse_gate", ok=True, expected_gpu_hit_tokens=expected, observed=hits[-1])
        external = [
            r
            for r in status["records"]
            if r["event"] == "cpu_prefix_lookup"
            and r["request_id"] == hits[-1]["request_id"]
        ]
        assert external and all(r["cpu_external_tokens"] == 0 for r in external), (
            external
        )
        emit("no_cpu_restore_gate", ok=True, observed=external)
        emit("B_warm", **request(b[2], prompt))
        passed = True
    finally:
        try:
            result = job(*b, dict(cmd="cleanup"))
            emit("B_cleanup", **result)
            clean_b = (
                result["ok"]
                and result["imported_blocks"] == 0
                and result["queue_size"] == 0
            )
            if clean_b:
                result = job(*a, dict(cmd="cleanup", destination_done=True))
                emit("A_cleanup", **result)
                clean_a = (
                    result["ok"]
                    and result["candidate_blocks"] == 0
                    and result["exported_key"] is None
                    and result["queue_size"] == 0
                )
        finally:
            emit(
                "CELL_GATE",
                ok=bool(passed and clean_a and clean_b),
                clean_a=clean_a,
                clean_b=clean_b,
            )
            if not (passed and clean_a and clean_b):
                raise RuntimeError("D3 cell or cleanup gate failed")


if __name__ == "__main__":
    main()
