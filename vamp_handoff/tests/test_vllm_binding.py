# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""CPU-only tests of the VAMP binding against the real pinned
``CPUOffloadingManager`` (scheduler side) and CPU tensors of the worker
layout (bridge). Skipped when vLLM/torch are not importable, e.g. under
``python -S``. Run with the full venv:

    .venv/bin/python -m unittest vamp_handoff/tests/test_vllm_binding.py -v
"""

import sys
import threading
import unittest
from pathlib import Path

HANDOFF = Path(__file__).resolve().parents[1]
if str(HANDOFF) not in sys.path:
    sys.path.insert(0, str(HANDOFF))

try:
    import torch
    from vamp_cxl.vllm_binding import (
        CpuPayloadBridge,
        VampCPUOffloadingManager,
    )

    from vllm.v1.core.kv_cache_utils import BlockHash

    HAVE_VLLM = True
except Exception:  # noqa: BLE001 - any import failure means skip
    HAVE_VLLM = False


def bh(name: str):
    return BlockHash(name.encode())


class _Listener:
    def __init__(self):
        self.ready = []
        self.evicted = []

    def on_blocks_ready(self, hashes):
        self.ready.append(list(hashes))

    def on_blocks_evicted(self, hashes):
        self.evicted.append(list(hashes))


@unittest.skipUnless(HAVE_VLLM, "vLLM/torch not importable in this interpreter")
class SchedulerSideBinding(unittest.TestCase):
    def _manager(self, num_blocks=4):
        listener = _Listener()
        m = VampCPUOffloadingManager(
            block_size=32, num_blocks=num_blocks, listener=listener
        )
        m.bind_owner_thread()
        return m, listener

    def _store(self, m, names, success=True):
        hashes = [bh(n) for n in names]
        out = m.prepare_store(hashes)
        self.assertIsNotNone(out)
        m.complete_store(out.block_hashes_to_store, success)
        return hashes

    def test_ready_notification_only_for_newly_ready(self):
        m, listener = self._manager()
        a, b = self._store(m, ["a", "b"])
        self.assertEqual(listener.ready, [[a, b]])
        m.complete_store([a, b])  # already READY: no second notification
        self.assertEqual(listener.ready, [[a, b]])
        self.assertEqual(m.ready_run([a, b]), 2)
        out = m.prepare_store([bh("c")])
        m.complete_store(out.block_hashes_to_store, success=False)
        self.assertEqual(len(listener.ready), 1)
        self.assertEqual(m.ready_run([bh("c")]), 0)

    def test_lease_is_atomic_and_pins_against_eviction(self):
        m, listener = self._manager(num_blocks=3)
        a, b = self._store(m, ["a", "b"])
        pending = m.prepare_store([bh("p")])  # allocated, not READY
        self.assertIsNone(m.acquire_export_lease([a, bh("p")]))  # no partial pin
        self.assertEqual(m.counters["lease_miss"], 1)
        lease = m.acquire_export_lease([a, b])
        self.assertIsNotNone(lease)
        self.assertEqual(len(lease.block_ids), 2)
        self.assertEqual(m.active_leases(), 1)
        m.complete_store(pending.block_hashes_to_store)
        # cache full (a, b pinned; p ready). New store must evict p, never a/b.
        out = m.prepare_store([bh("q")])
        self.assertIsNotNone(out)
        self.assertEqual(out.block_hashes_evicted, [bh("p")])
        self.assertEqual(listener.evicted, [[bh("p")]])
        # another new block cannot be placed while a and b are pinned
        self.assertIsNone(m.prepare_store([bh("r")]))
        m.release_export_lease(lease)
        self.assertEqual(m.active_leases(), 0)
        with self.assertRaises(KeyError):
            m.release_export_lease(lease)
        self.assertIsNotNone(m.prepare_store([bh("r")]))

    def test_mailbox_serialises_foreign_threads(self):
        m, _ = self._manager()
        a, b = self._store(m, ["a", "b"])
        results = {}

        def foreign():
            try:
                m.acquire_export_lease([a])
            except RuntimeError as exc:
                results["direct"] = str(exc)
            results["future"] = m.acquire_export_lease_async([a, b])

        t = threading.Thread(target=foreign)
        t.start()
        t.join()
        self.assertIn("non-owner thread", results["direct"])
        fut = results["future"]
        self.assertFalse(fut.done())  # nothing runs until the owner drains
        m.lookup([a])  # scheduler-thread entry point drains the mailbox
        self.assertTrue(fut.done())
        lease = fut.result()
        self.assertEqual(lease.block_hashes, (a, b))
        rel = m.release_export_lease_async(lease)
        self.assertFalse(rel.done())
        list(m.take_events())  # the other drain point
        self.assertTrue(rel.done())
        self.assertEqual(m.active_leases(), 0)
        bad = m.release_export_lease_async(lease)
        m.lookup([a])
        with self.assertRaises(KeyError):
            bad.result()

    def test_import_reservation_and_commit(self):
        m, listener = self._manager()
        res = m.reserve_import([bh("x"), bh("y")])
        self.assertEqual(len(res.block_ids), 2)
        self.assertEqual(m.ready_run([bh("x"), bh("y")]), 0)  # not READY yet
        self.assertIsNone(m.acquire_export_lease([bh("x")]))
        m.commit_import(res, success=True)
        self.assertEqual(m.ready_run([bh("x"), bh("y")]), 2)
        self.assertEqual(listener.ready[-1], [bh("x"), bh("y")])
        res2 = m.reserve_import([bh("x"), bh("z")])  # x already present
        self.assertEqual(res2.block_hashes, (bh("z"),))
        m.commit_import(res2, success=False)
        self.assertEqual(m.ready_run([bh("z")]), 0)
        with self.assertRaises(KeyError):
            m.commit_import(res2, success=True)


@unittest.skipUnless(HAVE_VLLM, "vLLM/torch not importable in this interpreter")
class WorkerSideBridge(unittest.TestCase):
    def test_gather_import_roundtrip(self):
        # two "layers", 6 CPU blocks, page sizes 96 and 64 bytes (int8)
        t1 = torch.zeros((6, 96), dtype=torch.int8)
        t2 = torch.zeros((6, 64), dtype=torch.int8)
        bridge = CpuPayloadBridge([t1, t2])
        self.assertEqual(bridge.block_bytes, 160)
        src = torch.randint(-128, 127, (6, 96), dtype=torch.int8)
        t1.copy_(src)
        t2.copy_(torch.randint(-128, 127, (6, 64), dtype=torch.int8))
        payload = bridge.gather([1, 4])
        self.assertEqual(len(payload), 320)
        self.assertEqual(payload[:96], bytes(t1[1].numpy()))
        chk = bridge.checksum([1, 4])
        bridge.import_payload([0, 5], payload)
        self.assertTrue(torch.equal(t1[0], t1[1]))
        self.assertTrue(torch.equal(t2[5], t2[4]))
        self.assertEqual(bridge.checksum([0, 5]), chk)
        with self.assertRaises(ValueError):
            bridge.import_payload([0], payload)
        with self.assertRaises(IndexError):
            bridge.gather([6])
        # zero-copy: views alias the tensor storage
        view = next(bridge.export_views([2]))
        t1[2, 0] = 42
        self.assertEqual(view[0], 42)


if __name__ == "__main__":
    unittest.main()
