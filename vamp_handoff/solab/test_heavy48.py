# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""GPU-free tests; checkpoint tests use the installed vLLM import environment."""

import unittest
from collections import deque
from types import SimpleNamespace
from unittest.mock import patch

from heavy48_replay import take_ready
from heavy48_workload import clean_messages, parse_gpu_pool, prefix_length


class WorkloadTests(unittest.TestCase):
    def test_pool_parser_ansi_and_last_boot(self):
        self.assertEqual(
            parse_gpu_pool(
                "GPU KV cache size: 89,392 tokens\n"
                "\x1b[32mINFO GPU KV cache size: 97,408 tokens\x1b[0m\n"
            ),
            97408,
        )

    def test_pool_parser_missing(self):
        with self.assertRaises(ValueError):
            parse_gpu_pool("boot failed")

    def test_owner_limit_preserves_other_order(self):
        ready = deque([(0, 1), (2, 1), (1, 0), (3, 0)])
        self.assertEqual(take_ready(ready, [2, 0], 2), (1, 0))
        self.assertEqual(list(ready), [(0, 1), (2, 1), (3, 0)])

    def test_full_owners_do_not_pop(self):
        ready = deque([(0, 1), (1, 0)])
        self.assertIsNone(take_ready(ready, [2, 2], 2))
        self.assertEqual(len(ready), 2)

    def test_lcp_stops_at_edit(self):
        self.assertEqual(prefix_length([1, 2, 3], [1, 9, 3]), 1)

    def test_lcp_append(self):
        self.assertEqual(prefix_length([1, 2], [1, 2, 3]), 2)
        self.assertEqual(prefix_length([], [1]), 0)

    def test_normalization_no_truncation(self):
        messages = [
            dict(role="tool", content=[dict(text="A"), dict(text="B")]),
            dict(role="user", content="x" * 100000),
        ]
        result = clean_messages(messages)
        self.assertEqual(result[0], dict(role="user", content="AB"))
        self.assertEqual(len(result[1]["content"]), 100000)
        self.assertEqual(messages[0]["role"], "tool")


class CheckpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import heavy48_agent
        except ModuleNotFoundError as exc:
            raise unittest.SkipTest(f"installed vLLM needed: {exc}") from exc
        cls.agent = heavy48_agent

    def setUp(self):
        a = self.agent
        a.REGISTRY.clear()
        a.REGISTRY["fp"] = dict(hashes=["01", "02"], prompt_tokens=32)
        a.legacy.STATE.candidate = None
        a.legacy.STATE.candidate_hashes = None
        self.lease = SimpleNamespace(block_ids=[1])
        self.manager = SimpleNamespace(
            ready_run=lambda hs: int(bytes(hs[0]) == b"\x01"),
            acquire_export_lease=lambda hs: self.lease,
        )
        self.patches = [
            patch.object(a.binding, "current_manager", return_value=self.manager),
            patch.object(
                a.binding,
                "current_bridge",
                return_value=SimpleNamespace(block_bytes=16),
            ),
            patch.object(a.legacy, "_log"),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def test_missing_excludes_ready(self):
        r = self.agent.command(dict(cmd="missing", hashes=["01", "02"]))
        self.assertEqual(r["missing"], ["02"])
        self.assertEqual(r["already_cpu_ready"], 1)

    def test_missing_metadata_rejected(self):
        with self.assertRaises(KeyError):
            self.agent.command(dict(cmd="metadata", fingerprint="unknown"))

    def test_foreign_hash_rejected(self):
        with self.assertRaises(ValueError):
            self.agent.command(dict(cmd="select", fingerprint="fp", hashes=["03"]))
        self.assertIsNone(self.agent.legacy.STATE.candidate)

    def test_not_ready_rejected(self):
        self.manager.acquire_export_lease = lambda hs: None
        with self.assertRaises(RuntimeError):
            self.agent.command(dict(cmd="select", fingerprint="fp", hashes=["02"]))
        self.assertIsNone(self.agent.legacy.STATE.candidate)

    def test_one_lease_only(self):
        result = self.agent.command(dict(cmd="select", fingerprint="fp", hashes=["02"]))
        self.assertEqual(result["nbytes"], 16)
        self.assertIs(self.agent.legacy.STATE.candidate, self.lease)
        with self.assertRaises(RuntimeError):
            self.agent.command(dict(cmd="select", fingerprint="fp", hashes=["02"]))


if __name__ == "__main__":
    unittest.main()
