# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
import unittest
from types import SimpleNamespace

from d3_blocks import GpuReservation, chunk_ranges


class Cache(dict):
    def insert(self, key, block):
        self[key] = block


class Pool:
    def __init__(self, size=8):
        self.blocks = [
            SimpleNamespace(block_id=i, block_hash=None, ref_cnt=0) for i in range(size)
        ]
        self.cached_block_hash_to_block = Cache()

    def get_cached_block(self, h, groups):
        return self.cached_block_hash_to_block.get((h, 0))

    def get_new_blocks(self, n):
        free = [b for b in self.blocks if b.ref_cnt == 0]
        if len(free) < n:
            raise ValueError("capacity")
        for b in free[:n]:
            b.ref_cnt += 1
        return free[:n]

    def _maybe_evict_cached_block(self, b):
        self.cached_block_hash_to_block.pop(b.block_hash, None)
        b.block_hash = None

    def free_blocks(self, blocks):
        for b in blocks:
            b.ref_cnt -= 1
            assert b.ref_cnt >= 0


def reserve(pool):
    return GpuReservation(pool, [b"a", b"b"], lambda h, g: (h, g))


class Tests(unittest.TestCase):
    def test_reservation_invisible(self):
        p = Pool()
        r = reserve(p)
        self.assertFalse(p.get_cached_block(b"a", [0]))
        self.assertEqual(sum(b.ref_cnt for b in r.blocks), 2)

    def test_commit_then_release(self):
        p = Pool()
        r = reserve(p)
        r.commit(True, True)
        r.release()
        r.release()
        self.assertIsNotNone(p.get_cached_block(b"a", [0]))
        self.assertEqual(sum(b.ref_cnt for b in p.blocks), 0)

    def test_checksum_abort(self):
        p = Pool()
        r = reserve(p)
        with self.assertRaises(RuntimeError):
            r.commit(True, False)
        r.abort()
        r.abort()
        self.assertFalse(p.cached_block_hash_to_block)
        self.assertEqual(sum(b.ref_cnt for b in p.blocks), 0)

    def test_incomplete_copy(self):
        p = Pool()
        r = reserve(p)
        with self.assertRaises(RuntimeError):
            r.commit(False, True)
        r.abort()
        self.assertFalse(p.cached_block_hash_to_block)

    def test_capacity(self):
        with self.assertRaises(ValueError):
            reserve(Pool(1))

    def test_duplicate_destination(self):
        p = Pool()
        r = reserve(p)
        r.commit(True, True)
        with self.assertRaises(ValueError):
            reserve(p)

    def test_partial_publish_abort(self):
        p = Pool()
        r = reserve(p)
        original = p.cached_block_hash_to_block.insert

        def fail(key, block):
            if key[0] == b"b":
                raise RuntimeError("injected insertion failure")
            original(key, block)

        p.cached_block_hash_to_block.insert = fail
        with self.assertRaises(RuntimeError):
            r.commit(True, True)
        r.abort()
        self.assertFalse(p.cached_block_hash_to_block)
        self.assertEqual(sum(b.ref_cnt for b in p.blocks), 0)

    def test_chunks(self):
        self.assertEqual(list(chunk_ranges(9, 10, 32)), [(0, 3), (3, 6), (6, 9)])
        self.assertEqual(list(chunk_ranges(8, 10, 32))[-1], (6, 8))
        with self.assertRaises(ValueError):
            list(chunk_ranges(1, 20, 10))


if __name__ == "__main__":
    unittest.main()
