# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""Assemble the minimal handoff once from the previously verified local bundle.

This is a maintainer capture tool, not a prerequisite for Claude development.
It never connects to JP, downloads source, or changes the repository's vLLM files.
"""

import argparse
import difflib
import hashlib
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HANDOFF = ROOT / "vamp_handoff"
BASE = "2a69949bdadf0e8942b7a1619b229cb475beef20"
DYNAMO_BASE = "65f12d7db4b70b8404d70a726647c164d5f7fe47"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    args = parser.parse_args()
    lock_path = HANDOFF / "SOURCE_LOCK.json"
    if lock_path.exists():
        parser.error("Refusing to replace existing provenance")
    bundle = args.bundle.resolve()
    old_lock = json.loads((bundle / "SOURCE_LOCK.json").read_text())
    if old_lock["upstreams"]["vllm"]["commit"] != BASE:
        parser.error("Unexpected bundle vLLM base")

    locked: dict[str, str] = {}
    baseline_refs: dict[str, str] = {}
    source_refs: dict[str, str] = {}

    def capture(src: Path, dest: Path) -> None:
        key = src.relative_to(bundle).as_posix()
        expected = old_lock["files"][key]["sha256"]
        if digest(src) != expected:
            raise ValueError(f"Input source drift: {key}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        locked[dest.relative_to(ROOT).as_posix()] = digest(dest)

    python_base = Path("components/backends/vllm/src/dynamo/vllm")
    changes = []
    patch = []
    for src in sorted((bundle / "vendor/dynamo" / python_base).glob("*.py")):
        dest = HANDOFF / "source_context/dynamo/vllm" / (src.name + ".ref")
        capture(src, dest)
        source_refs[src.name] = dest.relative_to(ROOT).as_posix()
        original = bundle / "patches/pristine/vendor/dynamo" / python_base / src.name
        if original.is_file():
            pristine = HANDOFF / "patches/pristine/dynamo/vllm" / (src.name + ".ref")
            capture(original, pristine)
            path = (python_base / src.name).as_posix()
            patch.extend(
                difflib.unified_diff(
                    original.read_text().splitlines(keepends=True),
                    src.read_text().splitlines(keepends=True),
                    fromfile="a/" + path,
                    tofile="b/" + path,
                )
            )
            changes.append(
                {
                    "name": src.name,
                    "reference": dest.relative_to(ROOT).as_posix(),
                    "pristine": pristine.relative_to(ROOT).as_posix(),
                    "upstream_path": path,
                }
            )
    for name in ("scheduler.rs", "indexer.rs", "sequence.rs"):
        src = bundle / "vendor/dynamo/lib/llm/src/kv_router" / name
        capture(src, HANDOFF / "source_context/dynamo/kv_router" / (name + ".ref"))
    capture(
        bundle / "vendor/dynamo/LICENSE",
        HANDOFF / "source_context/dynamo/LICENSE",
    )
    for group, names in {
        "legacy_policy": ("cass_policy.py", "cass_mock.py", "routing_shim.py"),
        "solab": (
            "pressure_run.py",
            "routing_smoke.py",
            "worker_offload.sh",
            "wstat2.py",
        ),
    }.items():
        for name in names:
            src = bundle / "baselines" / group / name
            dest = HANDOFF / "baselines" / group / (name + ".ref")
            capture(src, dest)
            baseline_refs[name] = dest.relative_to(ROOT).as_posix()

    patch_path = HANDOFF / "patches/dynamo-solab.patch"
    patch_path.write_text("".join(patch))
    locked[patch_path.relative_to(ROOT).as_posix()] = digest(patch_path)

    hooks = set()
    for directory in (
        "vllm/v1/kv_offload",
        "vllm/distributed/kv_transfer/kv_connector/v1/offloading",
    ):
        hooks.update(
            path.relative_to(ROOT).as_posix()
            for path in (ROOT / directory).rglob("*.py")
        )
    hooks.update(
        {
            "vllm/v1/core/kv_cache_manager.py",
            "vllm/v1/core/sched/scheduler.py",
            "vllm/v1/request.py",
            "vllm/v1/metrics/stats.py",
            "vllm/config/kv_transfer.py",
        }
    )
    for path in sorted(hooks):
        actual = ROOT / path
        expected = bundle / "vendor/vllm" / path
        if digest(actual) != digest(expected):
            raise ValueError(
                f"Existing vLLM source differs from captured release: {path}"
            )
        locked[path] = digest(actual)

    lock = {
        "schema_version": 1,
        "captured_date": "2026-09-05",
        "repository": "https://github.com/UCSD-JP/vllm-vamp",
        "base_vllm_commit": BASE,
        "upstream_vllm_tag": "v0.19.0",
        "upstream_tag_commit_verified_equal": True,
        "dynamo_commit": DYNAMO_BASE,
        "dynamo_tag": "v0.5.0",
        "provider_shared_memory": {
            "vendored": False,
            "modifiable": False,
            "api_mutable": False,
        },
        "root_vllm_modified_by_handoff": False,
        "new_cxl_runtime_implemented": False,
        "vllm_hook_files": sorted(hooks),
        "dynamo_python_references": source_refs,
        "dynamo_changes": changes,
        "baseline_references": baseline_refs,
        "files": locked,
    }
    lock_path.write_text(json.dumps(lock, indent=2) + "\n")
    print(f"Captured {len(locked)} entries; reused {len(hooks)} existing vLLM files")


if __name__ == "__main__":
    main()
