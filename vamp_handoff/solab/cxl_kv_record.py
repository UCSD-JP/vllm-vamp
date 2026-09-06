# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""Fixed-layout record published under a key for one exported KV prefix run
(G-F demo: single manager writes, one reader per record). Pure stdlib so it is
testable without vLLM.

All fields little-endian uint64 except the raw 32-byte sha256:
  magic, generation, nbytes, n_blocks, block_bytes, lockptr,
  payload_off, hashes_off, hashes_len, state, sha256[32]
Offsets are provider slice-relative (from cxl_shm_get_offset). ``hashes`` is a
blob of n_blocks x 32-byte vLLM BlockHash values in prefix order.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

MAGIC = 0x56414D504B5631  # "VAMPKV1"
STATE_WRITING = 0
STATE_READY = 1
_REC = struct.Struct("<10Q32s")
SIZE = _REC.size  # 112


@dataclass(frozen=True)
class KvRecord:
    generation: int
    nbytes: int
    n_blocks: int
    block_bytes: int
    lockptr: int
    payload_off: int
    hashes_off: int
    hashes_len: int
    state: int
    sha256: bytes  # 32 raw bytes

    def pack(self) -> bytes:
        if len(self.sha256) != 32:
            raise ValueError("sha256 must be 32 raw bytes")
        return _REC.pack(
            MAGIC, self.generation, self.nbytes, self.n_blocks, self.block_bytes,
            self.lockptr, self.payload_off, self.hashes_off, self.hashes_len,
            self.state, self.sha256,
        )

    @classmethod
    def unpack(cls, raw: bytes) -> KvRecord:
        (magic, gen, nbytes, nb, bb, lockptr, poff, hoff, hlen, state, sha) = _REC.unpack(raw)
        if magic != MAGIC:
            raise ValueError(f"not a VAMP KV record (magic {magic:#x})")
        return cls(gen, nbytes, nb, bb, lockptr, poff, hoff, hlen, state, sha)

    def consistent(self) -> bool:
        return (
            self.n_blocks > 0
            and self.nbytes == self.n_blocks * self.block_bytes
            and self.hashes_len == self.n_blocks * 32
        )


def pack_hashes(hashes: list[bytes]) -> bytes:
    for h in hashes:
        if len(h) != 32:
            raise ValueError("BlockHash must be 32 bytes")
    return b"".join(hashes)


def unpack_hashes(blob: bytes, n_blocks: int) -> list[bytes]:
    if len(blob) != n_blocks * 32:
        raise ValueError("hash blob length mismatch")
    return [bytes(blob[i * 32 : (i + 1) * 32]) for i in range(n_blocks)]
