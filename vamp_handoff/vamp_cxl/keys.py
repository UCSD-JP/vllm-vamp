# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""Identifiers shared by policy, adapters, runner and analyzer (spec §5).

Nothing here is an existing vLLM/TraCT API. Raw virtual pointers and local
block IDs never cross a host boundary: cross-host references are
:class:`PayloadRef` values carrying allocator identity and generation.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

GIB = 1 << 30
MIB = 1 << 20


@dataclass(frozen=True)
class RunKey:
    run_id: str
    cell_id: str
    seed: int

    def namespace(self) -> str:
        return f"{self.run_id}/{self.cell_id}/{self.seed}"


@dataclass(frozen=True)
class RequestKey:
    run: RunKey
    session_id: int
    turn_id: int
    attempt_id: int = 0

    def label(self) -> str:
        return (
            f"{self.run.namespace()}:s{self.session_id}"
            f":t{self.turn_id}:a{self.attempt_id}"
        )


@dataclass(frozen=True)
class ModelKey:
    model_revision: str
    tokenizer_revision: str
    kv_dtype: str
    layout_version: str
    tp: int = 1
    pp: int = 1


@dataclass(frozen=True)
class PrefixKey:
    """Identity of a complete-block reusable prefix.

    ``hash_chain`` is the engine prefix block hash chain (the last block hash
    is sufficient for identity once the chain is verified, but we keep the
    full tuple so partial matches can be resolved at complete block
    boundaries). ``complete_tokens`` counts only tokens in complete blocks.
    """

    model: ModelKey
    salt: str
    hash_chain: tuple[str, ...]
    complete_tokens: int

    def short(self) -> str:
        tail = self.hash_chain[-1][:12] if self.hash_chain else "empty"
        return f"{self.salt}:{tail}:{self.complete_tokens}"

    def __repr__(self) -> str:
        return f"PrefixKey({self.short()}, blocks={len(self.hash_chain)})"


@dataclass(frozen=True)
class LayoutDescriptor:
    block_tokens: int
    bytes_per_token: int
    layout_version: str

    def bytes_for_tokens(self, tokens: int) -> int:
        return tokens * self.bytes_per_token


@dataclass(frozen=True)
class PayloadRef:
    prefix: PrefixKey
    allocator_id: str
    generation: int
    relative_offset: int
    length: int
    layout: LayoutDescriptor


@dataclass(frozen=True)
class LeaseId:
    value: str


@dataclass(frozen=True)
class JobId:
    value: str


def block_hash_chain(
    token_ids: list[int], block_tokens: int, salt: str
) -> tuple[str, ...]:
    """Deterministic per-block hash chain for complete blocks only.

    This mirrors the *shape* of the engine's prefix hashing (each block hash
    depends on the parent hash and the block's tokens). It is not the engine
    hash: real PrefixKeys must be built from the engine's own block hashes
    when the adapter is bound.
    """
    chain: list[str] = []
    parent = hashlib.sha256(salt.encode()).hexdigest()
    for start in range(0, len(token_ids) - block_tokens + 1, block_tokens):
        block = token_ids[start : start + block_tokens]
        h = hashlib.sha256()
        h.update(parent.encode())
        h.update(",".join(str(t) for t in block).encode())
        parent = h.hexdigest()
        chain.append(parent)
    return tuple(chain)


@dataclass
class IdSource:
    """Monotonic unique ID source; injected so tests are deterministic."""

    prefix: str = "id"
    _next: int = field(default=0, repr=False)

    def next(self, kind: str) -> str:
        self._next += 1
        return f"{self.prefix}-{kind}-{self._next}"

    def lease(self) -> LeaseId:
        return LeaseId(self.next("lease"))

    def job(self) -> JobId:
        return JobId(self.next("job"))
