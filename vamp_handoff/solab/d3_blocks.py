# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""Single-group GPU reservations. Called only on the scheduler owner thread."""


class GpuReservation:
    def __init__(self, pool, hashes, make_key):
        self.pool = pool
        self.hashes = list(hashes)
        self.make_key = make_key
        if not self.hashes or len(set(self.hashes)) != len(self.hashes):
            raise ValueError("empty or duplicate prefix hashes")
        if any(pool.get_cached_block(h, [0]) for h in self.hashes):
            raise ValueError("D3 destination must be cold for the imported prefix")
        self.blocks = pool.get_new_blocks(len(self.hashes))
        self.committed = False
        self.released = False

    def commit(self, copy_complete, digest_match):
        if self.released or self.committed or not copy_complete or not digest_match:
            raise RuntimeError("GPU import cannot be published")
        # No request can interleave: the caller owns the scheduler thread.
        for block, h in zip(self.blocks, self.hashes):
            if block.block_hash is not None:
                raise RuntimeError("reserved block was unexpectedly published")
            key = self.make_key(h, 0)
            block.block_hash = key
            self.pool.cached_block_hash_to_block.insert(key, block)
        self.committed = True

    def abort(self):
        if self.released:
            return
        # Also undo a partially completed publication if insertion raised.
        for block in self.blocks:
            if block.block_hash is not None:
                self.pool._maybe_evict_cached_block(block)
                if block.block_hash is not None:
                    block.reset_hash()
        self.pool.free_blocks(self.blocks)
        self.released = True

    def release(self):
        if not self.committed:
            raise RuntimeError("cannot release an uncommitted import as cached")
        if not self.released:
            self.pool.free_blocks(self.blocks)
            self.released = True


def chunk_ranges(count, block_bytes, capacity):
    if count <= 0 or block_bytes <= 0 or capacity < block_bytes:
        raise ValueError("invalid staging geometry")
    per_chunk = capacity // block_bytes
    for start in range(0, count, per_chunk):
        yield start, min(count, start + per_chunk)
