# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""Check pinned vLLM hooks and handoff references with stdlib only; no SSH/GPU."""

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def verify(root: Path = ROOT) -> tuple[int, list[str]]:
    root = root.resolve()
    lock = json.loads((root / "vamp_handoff/SOURCE_LOCK.json").read_text())
    errors = []
    if lock["provider_shared_memory"] != {
        "vendored": False,
        "modifiable": False,
        "api_mutable": False,
    }:
        errors.append("Fixed external shared-memory boundary changed")
    for relative, expected in lock["files"].items():
        rel = Path(relative)
        if rel.is_absolute() or ".." in rel.parts:
            errors.append(f"Unsafe path: {relative}")
            continue
        path = root / rel
        if not path.is_file() or path.is_symlink():
            errors.append(f"Missing/replaced: {relative}")
        elif hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            errors.append(f"Source drift: {relative}")
    return len(lock["files"]), errors


def main() -> None:
    count, errors = verify()
    for error in errors[:30]:
        print(error)
    if errors:
        raise SystemExit(
            f"FAIL: {len(errors)} entries differ; review intentional changes"
        )
    print(f"PASS: {count} source/reference entries; no SSH, vendor imports, GPU or CXL")


if __name__ == "__main__":
    main()
