# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""Cold, sequential arm bundle with immutable per-arm logs and code hashes."""

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

from heavy48_workload import parse_gpu_pool


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", required=True)
    parser.add_argument("--arms", nargs="+", default=["B0", "B1", "B2"])
    parser.add_argument("--turns", type=int, default=8)
    parser.add_argument("--sessions", type=int, default=48)
    parser.add_argument("--per-worker", type=int, default=2)
    parser.add_argument("--concurrency", type=int, default=4)
    args = parser.parse_args()
    home = Path.home()
    probe = home / "vamp/probe"
    root = home / "vamp/heavy48_results" / args.run
    root.mkdir(parents=True, exist_ok=False)
    remote = ["ssh", "-p", "2022", "ucsd@192.168.5.61"]
    sources = root / "sources"
    sources.mkdir()
    for path in sorted(probe.glob("heavy48*.py")) + [probe / "worker_heavy48.sh"]:
        shutil.copy2(path, sources / path.name)
    for name in ("vamp_agent.py", "d3_cell.py"):
        shutil.copy2(probe / name, sources / name)
    for name in ("gd_hook.py", "gf_hook.py", "hook_common.py"):
        shutil.copy2(home / "vamp" / name, sources / name)
    shutil.copy2(home / "vamp/ga/heavy48_fingerprint_audit.json", root / "audit.json")
    manifest = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sources.iterdir()
    }
    (root / "source_sha256.json").write_text(json.dumps(manifest, indent=2))
    for arm in args.arms:
        if arm not in ("B0", "B1", "B2"):
            raise ValueError(arm)
        print("RESET", arm, flush=True)
        with (root / f"{arm}_reset.log").open("x") as f:
            subprocess.run(
                [sys.executable, str(probe / "heavy48_reset.py")],
                stdout=f,
                stderr=subprocess.STDOUT,
                check=True,
                timeout=600,
            )
        audit = json.loads((root / "audit.json").read_text())
        pools = []
        for index, (node, prefix) in enumerate((("s1", remote), ("s2", []))):
            boot = subprocess.check_output(
                prefix + ["cat", str(home / f"vamp/logs/worker_{node}.log")], text=True
            )
            tokens = parse_gpu_pool(boot)
            maximum = max(
                max(r["prompt_tokens"][: args.turns])
                for i, r in enumerate(audit["rows"][: args.sessions])
                if i % 2 == index
            )
            assert args.per_worker * (maximum + 16) < tokens, (node, maximum, tokens)
            pools.append(tokens)
        assert len(set(pools)) == 1, pools
        print("CAPACITY_GATE", pools, "per_worker", args.per_worker, flush=True)
        offsets = {}
        for node, prefix in (("s1", remote), ("s2", [])):
            for kind in ("probe", "wstats"):
                path = str(home / f"vamp/logs/{kind}_{node}.jsonl")
                size = int(subprocess.check_output(prefix + ["stat", "-c", "%s", path]))
                offsets[node, kind] = size
                seed = subprocess.check_output(prefix + ["tail", "-n", "1", path])
                (root / f"{arm}_{kind}_{node}_seed.jsonl").write_bytes(seed)
        print("START", arm, flush=True)
        try:
            with (root / f"{arm}_runner.log").open("x") as f:
                result = subprocess.run(
                    [
                        sys.executable,
                        str(probe / "heavy48_replay.py"),
                        "--arm",
                        arm,
                        "--trace-dir",
                        str(home / "vamp/heavy48"),
                        "--audit",
                        str(root / "audit.json"),
                        "--run-id",
                        args.run + "_" + arm,
                        "--out-dir",
                        str(root / arm),
                        "--turns",
                        str(args.turns),
                        "--sessions",
                        str(args.sessions),
                        "--per-worker",
                        str(args.per_worker),
                        "--concurrency",
                        str(args.concurrency),
                    ],
                    stdout=f,
                    stderr=subprocess.STDOUT,
                    timeout=3600,
                )
        finally:
            time.sleep(2)
            for node, prefix in (("s1", remote), ("s2", [])):
                for kind in ("probe", "wstats"):
                    path = str(home / f"vamp/logs/{kind}_{node}.jsonl")
                    with (root / f"{arm}_{kind}_{node}.jsonl").open("xb") as f:
                        subprocess.run(
                            prefix
                            + ["tail", "-c", f"+{offsets[node, kind] + 1}", path],
                            stdout=f,
                            check=True,
                        )
                with (root / f"{arm}_worker_{node}.log").open("xb") as f:
                    subprocess.run(
                        prefix + ["cat", str(home / f"vamp/logs/worker_{node}.log")],
                        stdout=f,
                        check=True,
                    )
        print("FINISH", arm, result.returncode, flush=True)
        if result.returncode:
            raise RuntimeError(f"{arm} failed; remaining arms not run")
    subprocess.run(remote + [str(home / "vamp/worker_stop.sh")], check=True)
    subprocess.run([str(home / "vamp/worker_stop.sh")], check=True)
    print("ALL_ARMS_OK workers stopped", root, flush=True)


if __name__ == "__main__":
    main()
