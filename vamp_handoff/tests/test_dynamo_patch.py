# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""The Dynamo receipt/targeting patch must apply cleanly to the captured
handlers.py reference and the result must parse and carry the receipt logic.
This checks the patch, not Dynamo at runtime."""

import ast
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HANDOFF = Path(__file__).resolve().parents[1]
REF = HANDOFF / "source_context/dynamo/vllm/handlers.py.ref"
PATCH = HANDOFF / "patches/dynamo-vamp-receipt.patch"
REL = "components/backends/vllm/src/dynamo/vllm/handlers.py"


@unittest.skipUnless(shutil.which("patch"), "GNU patch not available")
class DynamoReceiptPatch(unittest.TestCase):
    def test_patch_applies_and_parses(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / REL
            target.parent.mkdir(parents=True)
            shutil.copy(REF, target)
            run = subprocess.run(
                ["patch", "-p1", "--forward", "-i", str(PATCH)],
                cwd=tmp,
                capture_output=True,
                text=True,
            )
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            src = target.read_text()
            tree = ast.parse(src)
            names = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
            self.assertLessEqual({"_vamp_tag", "_vamp_wrong_worker"}, names)
            self.assertIn('os.environ.get("VAMP_WORKER_ID")', src)
            self.assertIn('"vamp_error": "wrong_worker"', src)
            self.assertEqual(src.count("self._vamp_tag("), 4)
            # applying twice must be refused (no duplicate hunks)
            again = subprocess.run(
                ["patch", "-p1", "--forward", "--dry-run", "-i", str(PATCH)],
                cwd=tmp,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(again.returncode, 0)
        # the reference itself is untouched (source lock) and patch is scoped
        self.assertNotIn("_vamp_tag", REF.read_text())
        header = PATCH.read_text().splitlines()[:2]
        self.assertEqual(header[0], f"--- a/{REL}")
        self.assertEqual(header[1], f"+++ b/{REL}")


if __name__ == "__main__":
    unittest.main()


def _unused(_: int = sys.maxsize) -> None:  # keep sys import meaningful for -S runs
    return None
