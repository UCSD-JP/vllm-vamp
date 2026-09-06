# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""Audit real Heavy-48 messages with the serving model's tokenizer, offline."""

import argparse
import gzip
import hashlib
import json
from array import array
from pathlib import Path


def clean_messages(messages):
    # Same explicit normalization as the established SWE replay_client.py.
    result = []
    for message in messages:
        content = message.get("content", "")
        if isinstance(content, list):
            content = "".join(x.get("text", "") for x in content if isinstance(x, dict))
        role = "user" if message["role"] == "tool" else message["role"]
        result.append(dict(role=role, content=content))
    return result


def load_trace(path):
    with gzip.open(path, "rt") as f:
        return [json.loads(line) for line in f]


def prefix_length(a, b):
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y:
            return i
    return min(len(a), len(b))


def parse_gpu_pool(log):
    values = []
    for line in log.splitlines():
        _, marker, tail = line.partition("GPU KV cache size: ")
        if marker:
            number, unit, _ = tail.partition(" tokens")
            if not unit:
                raise ValueError("GPU pool log missing token unit")
            values.append(int(number.replace(",", "")))
    if not values:
        raise ValueError("GPU pool not found in boot log")
    return values[-1]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--trace-dir", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--model", default="Qwen/Qwen3-14B")
    args = p.parse_args()
    from transformers import AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(args.model, local_files_only=True)
    rows = []
    for path in sorted(Path(args.trace_dir).glob("*.jsonl.gz")):
        turns = load_trace(path)
        previous = []
        counts, lcp, fingerprints = [], [], []
        for turn in turns:
            ids = tokenizer.apply_chat_template(
                clean_messages(turn["messages"]),
                tokenize=True,
                add_generation_prompt=True,
            )
            counts.append(len(ids))
            fingerprints.append(hashlib.sha256(array("I", ids).tobytes()).hexdigest())
            lcp.append(prefix_length(previous, ids))
            previous = ids
        row = dict(
            task=path.name.removesuffix(".jsonl.gz"),
            n_turns=len(turns),
            prompt_tokens=counts,
            adjacent_lcp_tokens=lcp,
            token_fingerprints=fingerprints,
            source_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        rows.append(row)
        print(
            json.dumps(
                dict(
                    task=row["task"],
                    turns=len(turns),
                    first=counts[0],
                    max=max(counts),
                    last=counts[-1],
                )
            ),
            flush=True,
        )
    report = dict(
        model=args.model,
        source="SWE-agent recorded Heavy-48",
        normalized_roles=True,
        input_truncation=False,
        sessions=len(rows),
        turns=sum(x["n_turns"] for x in rows),
        max_prompt_tokens=max(max(x["prompt_tokens"]) for x in rows),
        total_prompt_tokens=sum(sum(x["prompt_tokens"]) for x in rows),
        rows=rows,
    )
    Path(args.out).write_text(json.dumps(report, indent=2))
    print(json.dumps({k: v for k, v in report.items() if k != "rows"}), flush=True)


if __name__ == "__main__":
    main()
