# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""Cold worker reset for Heavy replay; retain manager and fixed frontends."""

import os
import subprocess
import time
from pathlib import Path

from d3_cell import request, rpc


def main():
    home = Path.home()
    remote = ["ssh", "-p", "2022", "ucsd@192.168.5.61"]
    subprocess.run(remote + [str(home / "vamp/worker_stop.sh")], check=True)
    subprocess.run([str(home / "vamp/worker_stop.sh")], check=True)
    subprocess.run(
        remote
        + [
            "env SPEC_MODULE=heavy48_agent NS=vampA TAG=s1 PORT=6880 CPU_GB=64 PIN=0 "
            "MML=65536 MNS=16 UTIL=0.90 "
            "setsid bash /home/ucsd/vamp/probe/worker_heavy48.sh "
            "</dev/null >/dev/null 2>&1 &"
        ],
        check=True,
    )
    env = dict(
        os.environ,
        SPEC_MODULE="heavy48_agent",
        NS="vampB",
        TAG="s2",
        PORT="6881",
        CPU_GB="64",
        PIN="0",
        MML="65536",
        MNS="16",
        UTIL="0.90",
    )
    subprocess.Popen(
        ["bash", str(home / "vamp/probe/worker_heavy48.sh")],
        env=env,
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for tick in range(100):
        time.sleep(5)
        try:
            a = rpc("192.168.5.61", 7201, dict(cmd="status"))
            b = rpc("localhost", 7202, dict(cmd="status"))
            if a["bridge_ready"] and b["bridge_ready"]:
                break
        except (OSError, RuntimeError):
            pass
        if tick % 6 == 0:
            print("BOOT_WAIT", (tick + 1) * 5, flush=True)
    else:
        raise RuntimeError("Heavy workers failed to boot")
    for port in (8080, 8081):
        for _ in range(30):
            try:
                print(
                    request(
                        f"http://localhost:{port}/v1/chat/completions",
                        "Heavy boot /no_think",
                        tokens=2,
                    ),
                    flush=True,
                )
                break
            except OSError:
                time.sleep(2)
        else:
            raise RuntimeError("frontend not ready")
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
    for host, port in (("192.168.5.61", 7001), ("localhost", 7002)):
        result = rpc(host, port, dict(cmd="status"))
        assert result["active_leases"] == 0 and not result["candidate_blocks"], result
    print("HEAVY_RESET_OK MML=65536 CPU_GB=64 PIN=0", flush=True)


if __name__ == "__main__":
    main()
