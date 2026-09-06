# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""GPU-free checks using the installed vLLM pool, not a look-alike mock."""

import unittest

from d3_blocks import GpuReservation
from vllm.v1.core.block_pool import BlockPool
from vllm.v1.core.kv_cache_utils import BlockHash, make_block_hash_with_group_id


class Tests(unittest.TestCase):
    def test_real_pool(self):
        pool = BlockPool(
            num_gpu_blocks=16,
            enable_caching=True,
            hash_block_size=16,
            enable_kv_cache_events=False,
        )
        hashes = [BlockHash(bytes([i]) * 32) for i in (1, 2)]
        initial = pool.get_num_free_blocks()
        r = GpuReservation(pool, hashes, make_block_hash_with_group_id)
        self.assertIsNone(pool.get_cached_block(hashes[0], [0]))
        r.commit(True, True)
        self.assertEqual(
            pool.get_cached_block(hashes[0], [0])[0].block_id, r.blocks[0].block_id
        )
        r.release()
        self.assertEqual(pool.get_num_free_blocks(), initial)

    def test_real_partial_abort(self):
        pool = BlockPool(
            num_gpu_blocks=16,
            enable_caching=True,
            hash_block_size=16,
            enable_kv_cache_events=False,
        )
        hashes = [BlockHash(bytes([i]) * 32) for i in (1, 2)]
        r = GpuReservation(pool, hashes, make_block_hash_with_group_id)
        # Model an exception after assigning a block hash but before index insertion.
        r.blocks[0].block_hash = make_block_hash_with_group_id(hashes[0], 0)
        r.abort()
        self.assertIsNone(r.blocks[0].block_hash)
        self.assertEqual(pool.get_num_free_blocks(), 15)


if __name__ == "__main__":
    unittest.main()
