# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""Six offline handoff checks; not the future shared-CXL M1-M12 gates."""

import ast
import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HANDOFF = ROOT / "vamp_handoff"


def load_verifier():
    spec = importlib.util.spec_from_file_location(
        "handoff_verifier", HANDOFF / "tools/verify_source_lock.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class HandoffTests(unittest.TestCase):
    def test_01_source_lock(self):
        count, errors = load_verifier().verify(ROOT)
        self.assertGreater(count, 30)
        self.assertEqual(errors, [])
        lock = json.loads((HANDOFF / "SOURCE_LOCK.json").read_text())
        self.assertEqual(
            lock["base_vllm_commit"], "2a69949bdadf0e8942b7a1619b229cb475beef20"
        )
        self.assertTrue(lock["upstream_tag_commit_verified_equal"])

    def test_02_fixed_provider_boundary(self):
        lock = json.loads((HANDOFF / "SOURCE_LOCK.json").read_text())
        self.assertEqual(
            lock["provider_shared_memory"],
            {
                "vendored": False,
                "modifiable": False,
                "api_mutable": False,
            },
        )
        forbidden = {"libcxl_shm.so", "api.h", "cacheline.h", "start_cxl_manager"}
        self.assertEqual(
            [str(p) for p in HANDOFF.rglob("*") if p.name in forbidden], []
        )

    def test_03_dynamo_patch_scope(self):
        lock = json.loads((HANDOFF / "SOURCE_LOCK.json").read_text())
        self.assertEqual(
            {item["name"] for item in lock["dynamo_changes"]},
            {"args.py", "main.py", "protocol.py", "publisher.py"},
        )
        self.assertFalse(lock["root_vllm_modified_by_handoff"])
        self.assertFalse(lock["new_cxl_runtime_implemented"])

    def test_04_existing_vllm_hook_interfaces(self):
        targets = {
            "vllm/v1/kv_offload/cpu/manager.py": {
                "prepare_store",
                "complete_store",
                "prepare_load",
                "complete_load",
            },
            "vllm/v1/kv_offload/worker/cpu_gpu.py": {"transfer_async", "get_finished"},
            "vllm/distributed/kv_transfer/kv_connector/v1/offloading/scheduler.py": {
                "get_num_new_matched_tokens",
                "_get_reqs_to_store",
                "request_finished",
            },
        }
        for relative, required in targets.items():
            with self.subTest(path=relative):
                tree = ast.parse((ROOT / relative).read_text())
                names = {
                    node.name
                    for node in ast.walk(tree)
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                }
                self.assertLessEqual(required, names)

    def test_05_captured_dynamo_sources_parse(self):
        lock = json.loads((HANDOFF / "SOURCE_LOCK.json").read_text())
        refs = lock["dynamo_python_references"]
        for relative in refs.values():
            ast.parse((ROOT / relative).read_text())
        self.assertIn("VAMP_STATS_FILE", (ROOT / refs["publisher.py"]).read_text())

    def test_06_legacy_cass_regression(self):
        previous = sys.modules.get("cass_policy")
        try:
            loaded = {}
            for name in ("cass_policy", "cass_mock"):
                path = HANDOFF / "baselines/legacy_policy" / (name + ".py.ref")
                module = types.ModuleType(name)
                module.__file__ = str(path)
                if name == "cass_policy":
                    sys.modules[name] = module
                exec(compile(path.read_text(), str(path), "exec"), module.__dict__)
                loaded[name] = module
            mock = loaded["cass_mock"]
            scenarios = (
                "m1_homogeneous",
                "m2_owner_skew",
                "m2b_queue_pressure",
                "m3_provisional_clock",
                "m4_error_invariant",
                "m5_new_task_flood",
                "m6_stale_metrics",
                "m7_mode_equivalence",
            )
            for name in scenarios:
                with self.subTest(scenario=name):
                    ok, message = getattr(mock, name)()
                    self.assertTrue(ok, message)
        finally:
            if previous is None:
                sys.modules.pop("cass_policy", None)
            else:
                sys.modules["cass_policy"] = previous


if __name__ == "__main__":
    unittest.main()
