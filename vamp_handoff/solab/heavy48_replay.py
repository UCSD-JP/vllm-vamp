# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""Fixed Heavy-48 window, then one quiescent staged migration checkpoint.

Recorded prompts, not a SWE correctness run. No live concurrent migration or D3.
The extra source anchor is explicit, identical in all arms, and separately timed.
"""

import argparse
import hashlib
import json
import subprocess
import sys
import time
import urllib.request
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

from d3_cell import job, request
from heavy48_workload import clean_messages, load_trace

URLS = [f"http://localhost:{p}/v1/chat/completions" for p in (8080, 8081)]


def take_ready(ready, counts, limit):
    for index, item in enumerate(ready):
        if counts[item[0] % 2] < limit:
            del ready[index]
            return item
    return None


def serving_request(messages, task, turn, worker, run_id, tokens=16):
    body = json.dumps(
        dict(
            model="Qwen/Qwen3-14B",
            messages=clean_messages(messages),
            max_tokens=tokens,
            temperature=0,
            stream=True,
            stream_options=dict(include_usage=True),
        )
    ).encode()
    req = urllib.request.Request(
        URLS[worker],
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Task-Id": f"{run_id}:{task}",
            "X-Request-Id": f"{run_id}:{task}:{turn}",
        },
    )
    start_wall, start = time.time(), time.monotonic()
    first = None
    usage = None
    pieces = []
    with urllib.request.urlopen(req, timeout=1200) as response:
        for line in response:
            if not line.startswith(b"data:"):
                continue
            payload = line[5:].strip()
            if payload == b"[DONE]":
                break
            item = json.loads(payload)
            if item.get("usage"):
                usage = item["usage"]
            for choice in item.get("choices", []):
                delta = choice.get("delta", {})
                content = delta.get("content") or delta.get("reasoning_content") or ""
                if content:
                    if first is None:
                        first = time.monotonic()
                    pieces.append(content)
    end = time.monotonic()
    if usage is None or first is None:
        raise RuntimeError(f"missing usage or generated text for {task}:{turn}")
    return dict(
        task=task,
        turn=turn,
        worker=worker,
        start_wall=start_wall,
        end_wall=time.time(),
        latency_s=end - start,
        ttft_s=first - start,
        usage=usage,
        output_sha256=hashlib.sha256("".join(pieces).encode()).hexdigest(),
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--arm", choices=["B0", "B1", "B2"], required=True)
    parser.add_argument("--trace-dir", required=True)
    parser.add_argument("--audit", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--turns", type=int, default=8)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--per-worker", type=int, default=2)
    parser.add_argument("--sessions", type=int, default=48)
    args = parser.parse_args()
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=False)
    audit = json.loads(Path(args.audit).read_text())
    if not 2 <= args.sessions <= len(audit["rows"]):
        raise ValueError("select 2-48 sessions for the two-worker checkpoint")
    if min(args.concurrency, args.per_worker) < 1:
        raise ValueError("concurrency limits must be positive")
    if not 1 <= args.turns < audit["rows"][0]["n_turns"]:
        raise ValueError("window must leave a recorded next turn for the focus task")
    selected = audit["rows"][: args.sessions]
    traces = [
        load_trace(Path(args.trace_dir) / (r["task"] + ".jsonl.gz")) for r in selected
    ]
    for meta in selected:
        path = Path(args.trace_dir) / (meta["task"] + ".jsonl.gz")
        assert hashlib.sha256(path.read_bytes()).hexdigest() == meta["source_sha256"]
        assert len(meta["token_fingerprints"]) == meta["n_turns"]
    expected = sum(min(args.turns, len(t)) for t in traces)
    rows = []

    def emit(event, **data):
        record = dict(event=event, wall=time.time(), **data)
        with (out / "cell.jsonl").open("a") as f:
            f.write(json.dumps(record) + "\n")
        if event != "request":
            print(json.dumps(record), flush=True)
        return record

    emit(
        "config",
        **vars(args),
        expected_requests=expected,
        assignment="sorted index modulo 2",
        source_anchor="repeat focus last window turn before checkpoint",
        output_tokens=16,
        input_truncation=False,
        replay_gaps=False,
        verify="sha256",
        direct_dma=False,
    )
    passed = False
    try:
        started = time.monotonic()
        emit("pressure_start")
        ready = deque((i, 0) for i in range(len(traces)))
        active = {}
        counts = [0, 0]
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            while ready or active:
                while ready and len(active) < args.concurrency:
                    item = take_ready(ready, counts, args.per_worker)
                    if item is None:
                        break
                    i, turn = item
                    future = pool.submit(
                        serving_request,
                        traces[i][turn]["messages"],
                        selected[i]["task"],
                        turn,
                        i % 2,
                        args.run_id,
                    )
                    active[future] = (i, turn)
                    counts[i % 2] += 1
                done, _ = wait(active, return_when=FIRST_COMPLETED)
                # Stable handling of simultaneous completions, not a per-turn barrier.
                for future in sorted(done, key=lambda f: active[f]):
                    i, turn = active.pop(future)
                    counts[i % 2] -= 1
                    result = future.result()
                    assert (
                        result["usage"]["prompt_tokens"]
                        == selected[i]["prompt_tokens"][turn]
                    ), result
                    result["fingerprint"] = selected[i]["token_fingerprints"][turn]
                    rows.append(emit("request", **result))
                    if turn + 1 < min(args.turns, len(traces[i])):
                        ready.append((i, turn + 1))
                    if len(rows) % 48 == 0:
                        emit(
                            "progress",
                            completed=len(rows),
                            expected=expected,
                            elapsed_s=time.monotonic() - started,
                        )
        assert len(rows) == expected
        emit(
            "pressure_done",
            completed=len(rows),
            elapsed_s=time.monotonic() - started,
            total_prompt_tokens=sum(r["usage"]["prompt_tokens"] for r in rows),
        )
        focus = selected[0]
        previous, following = args.turns - 1, args.turns
        assert following < len(traces[0])
        anchor = serving_request(
            traces[0][previous]["messages"],
            focus["task"],
            previous,
            0,
            args.run_id + "-anchor",
        )
        assert anchor["usage"]["prompt_tokens"] == focus["prompt_tokens"][previous]
        emit("source_anchor", **anchor)
        arrival = time.monotonic()
        request(URLS[0], "Heavy ready wakeup /no_think", tokens=2)
        request(URLS[1], "Heavy ready wakeup /no_think", tokens=2)
        fp = focus["token_fingerprints"][previous]
        a = ("192.168.5.61", 7201, URLS[0])
        b = ("localhost", 7202, URLS[1])
        metadata = job(*a, dict(cmd="metadata", fingerprint=fp))
        assert metadata["ok"], metadata
        n = min(
            len(metadata["hashes"]),
            focus["adjacent_lcp_tokens"][following] // metadata["block_tokens"],
        )
        hashes = metadata["hashes"][:n]
        missing = job(*b, dict(cmd="missing", hashes=hashes))
        assert missing["ok"] and missing["missing"], missing
        emit(
            "checkpoint_plan",
            task=focus["task"],
            source_fingerprint=fp,
            next_fingerprint=focus["token_fingerprints"][following],
            reusable_blocks=n,
            already_cpu_ready=missing["already_cpu_ready"],
            transfer_blocks=len(missing["missing"]),
            common_prefix_tokens=focus["adjacent_lcp_tokens"][following],
        )
        # Same selection work in all arms; B0 releases without copying.
        selection = job(
            *a, dict(cmd="select", fingerprint=fp, hashes=missing["missing"])
        )
        emit("selection", **selection)
        assert selection["ok"] and selection["selected"] > 0, selection
        script = "gf_hook.py" if args.arm == "B2" else "gd_hook.py"
        command = [sys.executable, str(Path.home() / "vamp" / script)]
        if args.arm == "B0":
            command.append("--cleanup-only")
        if args.arm == "B2":
            command += ["--key", "VAMP_HEAVY_" + args.run_id]
        hook_start = time.monotonic()
        with (out / "hook.jsonl").open("x") as f:
            result = subprocess.run(
                command, stdout=f, stderr=subprocess.STDOUT, timeout=900
            )
        emit(
            "hook_done",
            returncode=result.returncode,
            hook_wall_s=time.monotonic() - hook_start,
        )
        assert result.returncode == 0
        preparation = time.monotonic() - arrival
        response = serving_request(
            traces[0][following]["messages"],
            focus["task"],
            following,
            1,
            args.run_id + "-migrate",
        )
        assert response["usage"]["prompt_tokens"] == focus["prompt_tokens"][following]
        emit(
            "destination",
            preparation_s=preparation,
            arrival_to_done_s=time.monotonic() - arrival,
            fingerprint=focus["token_fingerprints"][following],
            **response,
        )
        passed = True
    finally:
        # Existing B-before-A cleanup refuses to discard the source on an unsafe abort.
        with (out / "final_cleanup.jsonl").open("x") as f:
            cleanup = subprocess.run(
                [
                    sys.executable,
                    str(Path.home() / "vamp/gd_hook.py"),
                    "--cleanup-only",
                ],
                stdout=f,
                stderr=subprocess.STDOUT,
                timeout=900,
            )
        emit(
            "CELL_GATE",
            ok=passed and cleanup.returncode == 0,
            cleanup_returncode=cleanup.returncode,
        )
        if not passed or cleanup.returncode:
            raise RuntimeError("Heavy cell failed; do not continue the next arm")


if __name__ == "__main__":
    main()
