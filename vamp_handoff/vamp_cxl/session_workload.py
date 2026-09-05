# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""Controlled session replay manifest (spec §3).

The manifest is split into a *public* part (what the runner sends and what a
policy may observe as it happens) and a *ground truth* part (future
destinations, migration list, gaps, last turn) that only the runner reads.
Prefix lengths are measured with the injected tokenizer and chat template,
never estimated from word counts.
"""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from .keys import ModelKey, PrefixKey, block_hash_chain

SCENARIOS = ("STAY", "MOVE_IMMEDIATE", "MOVE_GAP", "MOVE_CONTENDED")


class Tokenizer(Protocol):
    def encode(self, text: str) -> list[int]: ...


class ChatTemplate(Protocol):
    def render(self, messages: list[dict[str, str]]) -> str: ...


class DeterministicTokenizer:
    """Word-piece-like fake tokenizer: each whitespace token maps to 1-3 ids.

    Deterministic across runs; used only where a real tokenizer is not
    available offline. Real manifests must be built with the model's own
    tokenizer and template (spec §3).
    """

    def __init__(self, vocab_size: int = 151_936):
        self.vocab_size = vocab_size

    def encode(self, text: str) -> list[int]:
        ids: list[int] = []
        for word in text.split():
            h = hashlib.blake2b(word.encode(), digest_size=8).digest()
            n = 1 + h[0] % 3
            for i in range(n):
                ids.append(
                    int.from_bytes(h[i * 2 : i * 2 + 2], "little") % self.vocab_size
                )
        return ids


class SimpleChatTemplate:
    def render(self, messages: list[dict[str, str]]) -> str:
        parts = ["<|im_start|>system You are a helpful assistant. <|im_end|>"]
        for m in messages:
            parts.append(f"<|im_start|>{m['role']} {m['content']} <|im_end|>")
        parts.append("<|im_start|>assistant")
        return "\n".join(parts)


@dataclass(frozen=True)
class WorkloadConfig:
    model: str = "Qwen/Qwen3-14B"
    model_key: ModelKey = ModelKey(
        model_revision="unpinned",
        tokenizer_revision="unpinned",
        kv_dtype="bf16",
        layout_version="v0",
    )
    sessions: int = 32
    turns_per_session: int = 4
    reusable_prefix_tokens: int = 8192
    prefix_tokens_per_session: tuple[int, ...] | None = None  # mixed-length arm
    max_output_tokens: int = 32
    max_outstanding_requests: int = 8
    workers: tuple[str, ...] = ("w0", "w1")
    max_model_len: int = 16384
    offload_block_tokens: int = 32
    migration_turn: int = 2
    migrating_per_owner: int = 8
    seed: int = 0
    scenario: str = "STAY"
    gap_s: float = 10.0
    salt: str = "run-salt"
    bytes_per_token: int = 160 * 1024

    def __post_init__(self) -> None:
        if self.scenario not in SCENARIOS:
            raise ValueError(f"unknown scenario {self.scenario}")
        if self.reusable_prefix_tokens % self.offload_block_tokens:
            raise ValueError("reusable prefix must be block aligned")
        if self.prefix_tokens_per_session is not None:
            if len(self.prefix_tokens_per_session) != self.sessions:
                raise ValueError("prefix_tokens_per_session length must equal sessions")
            for n in self.prefix_tokens_per_session:
                if n % self.offload_block_tokens:
                    raise ValueError("per-session prefix must be block aligned")

    def target_prefix(self, session_id: int) -> int:
        if self.prefix_tokens_per_session is not None:
            return self.prefix_tokens_per_session[session_id]
        return self.reusable_prefix_tokens


@dataclass(frozen=True)
class TurnSpec:
    session_id: int
    turn_id: int
    messages: tuple[dict[str, str], ...]
    prompt_token_ids: tuple[int, ...]
    expected_marker: str

    @property
    def prompt_tokens(self) -> int:
        return len(self.prompt_token_ids)


@dataclass(frozen=True)
class SessionPublic:
    session_id: int
    initial_owner: str
    reusable_prefix_tokens: int
    prefix: PrefixKey
    turns: tuple[TurnSpec, ...]


@dataclass(frozen=True)
class SessionGroundTruth:
    """Runner-only. Never passed to a policy observation."""

    session_id: int
    migrates: bool
    destination_by_turn: tuple[str, ...]
    gap_s_by_turn: tuple[float, ...]
    last_turn: int


@dataclass
class Manifest:
    config: WorkloadConfig
    sessions: dict[int, SessionPublic]
    ground_truth: dict[int, SessionGroundTruth]
    cross_session_lcp_tokens: int
    total_reusable_tokens: int
    total_prompt_tokens: int
    unique_reusable_blocks: int
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def public_view(self) -> dict[str, Any]:
        """JSON-safe summary that contains no ground truth.

        The scenario name is ground truth too (it encodes future moves and
        gaps), so it is reported only by :meth:`ground_truth_view`.
        """
        return {
            "sessions": self.config.sessions,
            "turns_per_session": self.config.turns_per_session,
            "reusable_prefix_tokens": {
                s.session_id: s.reusable_prefix_tokens for s in self.sessions.values()
            },
            "initial_owner": {
                s.session_id: s.initial_owner for s in self.sessions.values()
            },
            "prompt_tokens": {
                s.session_id: [t.prompt_tokens for t in s.turns]
                for s in self.sessions.values()
            },
            "cross_session_lcp_tokens": self.cross_session_lcp_tokens,
            "total_reusable_tokens": self.total_reusable_tokens,
            "total_prompt_tokens": self.total_prompt_tokens,
            "unique_reusable_blocks": self.unique_reusable_blocks,
        }

    def ground_truth_view(self) -> dict[str, Any]:
        return {
            "scenario": self.config.scenario,
            "gap_s": self.config.gap_s,
            "sessions": {sid: asdict(gt) for sid, gt in self.ground_truth.items()},
        }


def longest_common_prefix(sequences: list[tuple[int, ...]]) -> int:
    if not sequences:
        return 0
    n = min(len(s) for s in sequences)
    first = sequences[0]
    for i in range(n):
        v = first[i]
        for s in sequences[1:]:
            if s[i] != v:
                return i
    return n


def _session_body(session_id: int, words: int, seed: int) -> str:
    rnd = random.Random((seed << 20) ^ (session_id * 7919 + 17))
    header = (
        f"[SESSION-{session_id}] Repository audit log for project {session_id}. "
        f"Nonce {rnd.randrange(1 << 30)}. Reference notes:"
    )
    items = [f"s{session_id}-item{i}-{rnd.randrange(100003)}" for i in range(words)]
    return header + " " + " ".join(items)


def _build_turn_messages(
    prefix_text: str, session_id: int, turn_id: int
) -> list[dict[str, str]]:
    return [
        {"role": "user", "content": prefix_text},
        {
            "role": "user",
            "content": (
                f"Turn {turn_id}: reply exactly S{session_id}T{turn_id}_OK /no_think"
            ),
        },
    ]


def build_session(
    cfg: WorkloadConfig, tokenizer: Tokenizer, template: ChatTemplate, session_id: int
) -> tuple[SessionPublic, int]:
    """Grow the session-unique body until the measured LCP across all turns
    reaches the target, then fix the target prefix at a block boundary.

    Returns the session and the measured LCP (which may exceed the target).
    """
    target = cfg.target_prefix(session_id)
    words = max(64, target // 2)
    lcp = 0
    turns: list[TurnSpec] = []
    for _ in range(40):
        body = _session_body(session_id, words, cfg.seed)
        turns = []
        for turn_id in range(cfg.turns_per_session):
            messages = _build_turn_messages(body, session_id, turn_id)
            ids = tuple(tokenizer.encode(template.render(messages)))
            turns.append(
                TurnSpec(
                    session_id=session_id,
                    turn_id=turn_id,
                    messages=tuple(messages),
                    prompt_token_ids=ids,
                    expected_marker=f"S{session_id}T{turn_id}_OK",
                )
            )
        lcp = longest_common_prefix([t.prompt_token_ids for t in turns])
        if lcp >= target:
            break
        deficit = target - lcp
        words += max(8, int(deficit * 0.6))
    if lcp < target:
        raise ValueError(f"session {session_id}: measured LCP {lcp} < target {target}")
    prefix_ids = list(turns[0].prompt_token_ids[:target])
    chain = block_hash_chain(prefix_ids, cfg.offload_block_tokens, cfg.salt)
    prefix = PrefixKey(
        model=cfg.model_key,
        salt=cfg.salt,
        hash_chain=chain,
        complete_tokens=target,
    )
    for t in turns:
        if t.prompt_tokens + cfg.max_output_tokens > cfg.max_model_len:
            raise ValueError(
                f"session {session_id} turn {t.turn_id}: {t.prompt_tokens}+"
                f"{cfg.max_output_tokens} exceeds max_model_len {cfg.max_model_len}"
            )
    owner = cfg.workers[session_id % len(cfg.workers)]
    return (
        SessionPublic(
            session_id=session_id,
            initial_owner=owner,
            reusable_prefix_tokens=target,
            prefix=prefix,
            turns=tuple(turns),
        ),
        lcp,
    )


def build_ground_truth(
    cfg: WorkloadConfig, sessions: dict[int, SessionPublic]
) -> dict[int, SessionGroundTruth]:
    rnd = random.Random(cfg.seed * 1_000_003 + 11)
    moving: set[int] = set()
    if cfg.scenario != "STAY":
        by_owner: dict[str, list[int]] = {w: [] for w in cfg.workers}
        for s in sessions.values():
            by_owner[s.initial_owner].append(s.session_id)
        for owner in cfg.workers:
            ids = sorted(by_owner[owner])
            moving.update(rnd.sample(ids, min(cfg.migrating_per_owner, len(ids))))
    gap = 0.0 if cfg.scenario == "MOVE_IMMEDIATE" else cfg.gap_s
    out: dict[int, SessionGroundTruth] = {}
    for s in sessions.values():
        dests: list[str] = []
        for turn_id in range(cfg.turns_per_session):
            if s.session_id in moving and turn_id >= cfg.migration_turn:
                idx = cfg.workers.index(s.initial_owner)
                dests.append(cfg.workers[(idx + 1) % len(cfg.workers)])
            else:
                dests.append(s.initial_owner)
        out[s.session_id] = SessionGroundTruth(
            session_id=s.session_id,
            migrates=s.session_id in moving,
            destination_by_turn=tuple(dests),
            gap_s_by_turn=tuple(gap for _ in range(cfg.turns_per_session)),
            last_turn=cfg.turns_per_session - 1,
        )
    return out


def build_manifest(
    cfg: WorkloadConfig,
    tokenizer: Tokenizer | None = None,
    template: ChatTemplate | None = None,
) -> Manifest:
    tokenizer = tokenizer or DeterministicTokenizer()
    template = template or SimpleChatTemplate()
    sessions: dict[int, SessionPublic] = {}
    measured_lcp: dict[int, int] = {}
    for sid in range(cfg.sessions):
        session, lcp = build_session(cfg, tokenizer, template, sid)
        sessions[sid] = session
        measured_lcp[sid] = lcp
    first_turns = [s.turns[0].prompt_token_ids for s in sessions.values()]
    cross = longest_common_prefix(first_turns) if len(first_turns) > 1 else 0
    ground_truth = build_ground_truth(cfg, sessions)
    unique_blocks = len({h for s in sessions.values() for h in s.prefix.hash_chain})
    return Manifest(
        config=cfg,
        sessions=sessions,
        ground_truth=ground_truth,
        cross_session_lcp_tokens=cross,
        total_reusable_tokens=sum(s.reusable_prefix_tokens for s in sessions.values()),
        total_prompt_tokens=sum(
            t.prompt_tokens for s in sessions.values() for t in s.turns
        ),
        unique_reusable_blocks=unique_blocks,
        diagnostics={"measured_lcp_by_session": measured_lcp},
    )


def manifest_digest(manifest: Manifest, include_ground_truth: bool) -> str:
    payload = manifest.public_view()
    if include_ground_truth:
        payload["ground_truth"] = manifest.ground_truth_view()
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
