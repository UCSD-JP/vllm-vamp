# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""Cold-reset D3 large, matched recompute, and failed-import cells; archive raw."""

import hashlib
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

home = Path.home()
probe = home / "vamp/probe"
root = home / "vamp/d3_results" / time.strftime("%Y%m%dT%H%M%S")
root.mkdir(parents=True)
print("ARCHIVE", root, flush=True)
for index, (name, arm, words, salt, injection) in enumerate(
    [
        ("large_direct", "D3", 1000, "e1", None),
        ("large_recompute", "B0", 1000, "e1", None),
        ("checksum_abort", "D3", 120, "d4", "checksum"),
    ]
):
    dest = root / name
    dest.mkdir()
    if index:
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
        str(words),
        "--salt",
        salt,
        "--out",
        str(dest / "cell.jsonl"),
    ]
    if injection:
        cmd += ["--inject", injection]
    start = time.time()
    with (dest / "console.log").open("w") as log:
        result = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT)
    manifest = dict(
        start_wall=start,
        end_wall=time.time(),
        command=cmd,
        rc=result.returncode,
        base_commit="7a01f560f",
        provider_modified=False,
    )
    sources = dest / "sources"
    sources.mkdir()
    for f in sorted(probe.glob("d3_*.py")):
        shutil.copy2(f, sources / f.name)
    manifest["source_sha256"] = {
        f.name: hashlib.sha256(f.read_bytes()).hexdigest() for f in sources.iterdir()
    }
    for tag in ("s1", "s2"):
        for filename in (
            f"worker_{tag}.log",
            f"probe_{tag}.jsonl",
            f"wstats_{tag}.jsonl",
        ):
            source = home / "vamp/logs" / filename
            if tag == "s1":
                subprocess.run(
                    ["scp", "-P", "2022", f"ucsd@192.168.5.61:{source}", str(dest)],
                    check=True,
                )
            elif source.exists():
                shutil.copy2(source, dest / filename)
    (dest / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print("CELL", name, "rc", result.returncode, "archive", dest, flush=True)
    if result.returncode:
        print((dest / "console.log").read_text()[-6000:], flush=True)
        raise SystemExit(result.returncode)
print("D3_SUITE_PASS", root, flush=True)
