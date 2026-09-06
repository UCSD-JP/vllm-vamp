# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""Verification-cost ablation for the direct GPU->CXL->GPU path (D3), gap=0.

Cells (cold reset before each, identical prompt salt/words):
  B0        recompute at B
  D3 sha256 direct path, host readback + SHA-256 (correctness baseline)
  D3 gpu64  direct path, per-block GPU sums hashed (non-cryptographic, no readback)
  D3 none   direct path, verification skipped (recorded as skipped)
Raw archive per cell as in d3_suite.py. usage: d3_verify_suite.py [--salt v1] [--words 1000]
"""

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

P = argparse.ArgumentParser()
P.add_argument("--salt", default="v1")
P.add_argument("--words", type=int, default=1000)
P.add_argument("--cells", default="B0,sha256,gpu64,none")
args = P.parse_args()

home = Path.home()
probe = home / "vamp/probe"
root = home / "vamp/d3_results" / ("verify_" + time.strftime("%Y%m%dT%H%M%S"))
root.mkdir(parents=True)
print("ARCHIVE", root, flush=True)
for index, cell in enumerate(args.cells.split(",")):
    arm, verify = ("B0", "sha256") if cell == "B0" else ("D3", cell)
    name = "recompute" if cell == "B0" else f"direct_{verify}"
    dest = root / name
    dest.mkdir()
    with (dest / "reset.log").open("w") as log:
        subprocess.run(
            [sys.executable, str(probe / "d3_reset.py")],
            stdout=log,
            stderr=subprocess.STDOUT,
            check=True,
        )
    cmd = [
        sys.executable,
        str(probe / "d3_cell.py"),
        "--arm",
        arm,
        "--words",
        str(args.words),
        "--salt",
        args.salt,
        "--verify",
        verify,
        "--out",
        str(dest / "cell.jsonl"),
    ]
    start = time.time()
    with (dest / "console.log").open("w") as log:
        result = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT)
    sources = dest / "sources"
    sources.mkdir()
    for f in sorted(probe.glob("d3_*.py")):
        shutil.copy2(f, sources / f.name)
    manifest = dict(
        start_wall=start,
        end_wall=time.time(),
        command=cmd,
        rc=result.returncode,
        provider_modified=False,
        source_sha256={
            f.name: hashlib.sha256(f.read_bytes()).hexdigest() for f in sources.iterdir()
        },
    )
    for tag in ("s1", "s2"):
        for filename in (f"worker_{tag}.log", f"probe_{tag}.jsonl", f"wstats_{tag}.jsonl"):
            source = home / "vamp/logs" / filename
            if tag == "s1":
                subprocess.run(
                    ["scp", "-P", "2022", f"ucsd@192.168.5.61:{source}", str(dest)],
                    check=True,
                )
            elif source.exists():
                shutil.copy2(source, dest / filename)
    (dest / "manifest.json").write_text(json.dumps(manifest, indent=2))
    summary = {}
    for line in (dest / "cell.jsonl").read_text().splitlines():
        row = json.loads(line)
        if row["event"] in ("export", "import"):
            summary[row["event"]] = {
                k: row.get(k)
                for k in ("operation_s", "wall_with_nudge_s", "verify", "verified", "verify_s", "dma_s", "register_s", "gather_s", "scatter_s", "visibility_s")
            }
        if row["event"] == "B_first":
            summary["B_first"] = {k: row.get(k) for k in ("preparation_s", "arrival_to_done_gap0_s", "latency_s")}
        if row["event"] == "CELL_GATE":
            summary["gate"] = row.get("ok")
    print("CELL", name, "rc", result.returncode, json.dumps(summary), flush=True)
    if result.returncode:
        print((dest / "console.log").read_text()[-4000:], flush=True)
        raise SystemExit(result.returncode)
print("D3_VERIFY_SUITE_PASS", root, flush=True)
