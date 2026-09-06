# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""Cold-reset per arm, run the near-complete replay, snapshot logs and source hashes.

Runs on s2. Retains manager/frontends/etcd/NATS; restarts only experiment workers
per arm via heavy48_reset.py (heavy48_agent, MML 65536, PIN 0). Archives each arm's
client cell.jsonl, per-node probe/sidecar slices, worker boot logs and a manifest.
usage: replay_suite.py --run rp1 --arms S L --sessions 48 --turns 0
"""
import argparse
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

from heavy48_workload import parse_gpu_pool


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--run", required=True)
    p.add_argument("--arms", nargs="+", default=["S", "L"])
    p.add_argument("--sessions", type=int, default=48)
    p.add_argument("--turns", type=int, default=0)
    p.add_argument("--concurrency", type=int, default=6)
    p.add_argument("--budget-frac", type=float, default=0.8)
    args = p.parse_args()
    home = Path.home()
    probe = home / "vamp/probe"
    root = home / "vamp/replay_results" / args.run
    root.mkdir(parents=True, exist_ok=False)
    remote = ["ssh", "-p", "2022", "ucsd@192.168.5.61"]
    audit = home / "vamp/ga/heavy48_fingerprint_audit.json"
    sources = root / "sources"
    sources.mkdir()
    for name in ("replay_policy.py", "heavy48_replay.py", "heavy48_workload.py",
                 "heavy48_agent.py", "vamp_agent.py", "worker_heavy48.sh", "heavy48_reset.py"):
        src = probe / name
        if src.exists():
            (sources / name).write_bytes(src.read_bytes())
    (root / "source_sha256.json").write_text(json.dumps(
        {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sources.iterdir()}, indent=2))

    for arm in args.arms:
        print("RESET", arm, flush=True)
        with (root / f"{arm}_reset.log").open("x") as f:
            subprocess.run([sys.executable, str(probe / "heavy48_reset.py")],
                           stdout=f, stderr=subprocess.STDOUT, check=True, timeout=900)
        pools = []
        for node, prefix in (("s1", remote), ("s2", [])):
            boot = subprocess.check_output(prefix + ["cat", str(home / f"vamp/logs/worker_{node}.log")], text=True)
            pools.append(parse_gpu_pool(boot))
        assert len(set(pools)) == 1, pools
        pool = pools[0]
        print("POOL", pool, flush=True)
        offsets = {}
        for node, prefix in (("s1", remote), ("s2", [])):
            for kind in ("probe", "wstats"):
                path = str(home / f"vamp/logs/{kind}_{node}.jsonl")
                offsets[node, kind] = int(subprocess.check_output(prefix + ["stat", "-c", "%s", path]))
        out = root / arm
        print("START", arm, flush=True)
        started = time.time()
        with (root / f"{arm}_runner.log").open("x") as f:
            rc = subprocess.run([
                sys.executable, str(probe / "replay_policy.py"),
                "--arm", arm, "--trace-dir", str(home / "vamp/heavy48"),
                "--audit", str(audit), "--out-dir", str(out), "--run-id", f"{args.run}_{arm}",
                "--sessions", str(args.sessions), "--turns", str(args.turns),
                "--concurrency", str(args.concurrency), "--pool-tokens", str(pool),
                "--budget-frac", str(args.budget_frac),
            ], stdout=f, stderr=subprocess.STDOUT, timeout=14400).returncode
        for node, prefix in (("s1", remote), ("s2", [])):
            for kind in ("probe", "wstats"):
                path = str(home / f"vamp/logs/{kind}_{node}.jsonl")
                with (root / f"{arm}_{kind}_{node}.jsonl").open("xb") as f:
                    subprocess.run(prefix + ["tail", "-c", f"+{offsets[node, kind] + 1}", path], stdout=f, check=True)
            with (root / f"{arm}_worker_{node}.log").open("xb") as f:
                subprocess.run(prefix + ["cat", str(home / f"vamp/logs/worker_{node}.log")], stdout=f, check=True)
        (root / f"{arm}_manifest.json").write_text(json.dumps(
            dict(arm=arm, pool_tokens=pool, rc=rc, start_wall=started, end_wall=time.time()), indent=2))
        print("FINISH", arm, rc, flush=True)
        if rc:
            raise RuntimeError(f"{arm} failed; remaining arms not run")
    subprocess.run(remote + [str(home / "vamp/worker_stop.sh")], check=True)
    subprocess.run([str(home / "vamp/worker_stop.sh")], check=True)
    print("REPLAY_SUITE_OK", root, flush=True)


if __name__ == "__main__":
    main()
