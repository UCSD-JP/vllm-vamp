# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""Run on s2: restart only experiment workers, keep CXL manager/frontends."""

import os
import subprocess
import time
import urllib.error
from pathlib import Path

from d3_cell import request, rpc

home = Path.home()
s1 = ["ssh", "-p", "2022", "ucsd@192.168.5.61"]
subprocess.run(s1 + [str(home / "vamp/worker_stop.sh")], check=True)
subprocess.run([str(home / "vamp/worker_stop.sh")], check=True)
subprocess.run(
    s1
    + [
        "env SPEC_MODULE=d3_agent NS=vampA TAG=s1 PORT=6880 CPU_GB=64 PIN=1 "
        "setsid /home/ucsd/vamp/worker_vamp.sh </dev/null >/dev/null 2>&1 &"
    ],
    check=True,
)
env = dict(
    os.environ,
    SPEC_MODULE="d3_agent",
    NS="vampB",
    TAG="s2",
    PORT="6881",
    CPU_GB="64",
    PIN="0",
)
subprocess.Popen(
    [str(home / "vamp/worker_vamp.sh")],
    env=env,
    start_new_session=True,
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
for tick in range(80):
    time.sleep(5)
    try:
        a = rpc("192.168.5.61", 7001, dict(cmd="status"))
        b = rpc("localhost", 7002, dict(cmd="status"))
        if a["bridge_ready"] and b["bridge_ready"]:
            break
    except (OSError, RuntimeError):
        pass
    if tick % 4 == 0:
        print(f"boot wait {5 * (tick + 1)}s", flush=True)
else:
    raise RuntimeError("D3 workers failed to boot")
for port in (8080, 8081):
    for attempt in range(30):
        try:
            result = request(
                f"http://localhost:{port}/v1/chat/completions",
                "D3-boot /no_think",
                tokens=2,
            )
            print(result, flush=True)
            break
        except urllib.error.HTTPError:
            # Agent registration precedes Dynamo endpoint publication at startup.
            time.sleep(2)
    else:
        raise RuntimeError(f"frontend {port} did not become ready")
for ns in ("vampA", "vampB"):
    result = subprocess.check_output(
        [
            str(home / "bin/etcdctl"),
            "--endpoints=http://localhost:2379",
            "get",
            f"instances/{ns}/backend/generate",
            "--prefix",
            "--keys-only",
        ],
        env=dict(os.environ, ETCDCTL_API="3"),
        text=True,
    )
    assert len(result.split()) == 1, (ns, result)
a = rpc("192.168.5.61", 7001, dict(cmd="status"))
b = rpc("localhost", 7002, dict(cmd="status"))
assert (
    a["pool_blocks"] == b["pool_blocks"]
    and a["candidate_blocks"] == b["candidate_blocks"] == 0
)
assert a["staging_bytes"] == b["staging_bytes"] <= 128 << 20
print(
    "D3_RESET_OK", a["pool_blocks"], a["block_tokens"], a["staging_bytes"], flush=True
)
