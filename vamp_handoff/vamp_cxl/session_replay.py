# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""Per-session closed-loop replay runner with streaming trace (spec §3, §11).

Admission is per session: ``next_ready = previous_response_done + gap``.
There is no global turn barrier. TTFT is fixed only by the first chunk that
carries a real generated token; role-only and usage-only SSE chunks never
set it. Ground truth (future destination, gaps, last turn) is read only by
the runner and passed to hooks one turn at a time.
"""

from __future__ import annotations

import json
import queue
import threading
import time
import urllib.request
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import IO, Any, Protocol

from .keys import PrefixKey, RequestKey, RunKey
from .kv_transfer_adapter import Clock, DispatchReceipt
from .session_workload import Manifest, TurnSpec


class ChunkKind(str, Enum):
    ROLE = "ROLE"
    REASONING = "REASONING"
    CONTENT = "CONTENT"
    USAGE = "USAGE"
    DONE = "DONE"
    ERROR = "ERROR"


@dataclass(frozen=True)
class StreamChunk:
    kind: ChunkKind
    now_ns: int
    text: str = ""
    usage: dict[str, int] | None = None
    worker_id: str | None = None
    error: str | None = None


def classify_chunk(obj: dict[str, Any]) -> tuple[ChunkKind, str]:
    """Classify one OpenAI-compatible streaming chunk.

    Returns (kind, text). A chunk with only ``usage`` and no choice deltas
    is USAGE. A delta that carries only ``role`` is ROLE. Reasoning text is
    kept separate from content so both first-token times are preserved.
    """
    choices = obj.get("choices") or []
    if not choices:
        if obj.get("usage"):
            return ChunkKind.USAGE, ""
        return ChunkKind.ROLE, ""
    delta = choices[0].get("delta") or {}
    reasoning = delta.get("reasoning_content") or delta.get("reasoning") or ""
    content = delta.get("content") or ""
    if content:
        return ChunkKind.CONTENT, content
    if reasoning:
        return ChunkKind.REASONING, reasoning
    if choices[0].get("finish_reason"):
        return ChunkKind.DONE, ""
    return ChunkKind.ROLE, ""


def parse_sse_data(line: str) -> dict[str, Any] | None:
    """Parse a single ``data: ...`` SSE line. Returns None for keep-alives."""
    if not line.startswith("data:"):
        return None
    payload = line[5:].strip()
    if not payload or payload == "[DONE]":
        return {"__done__": True} if payload == "[DONE]" else None
    return json.loads(payload)


@dataclass
class RequestTrace:
    request: RequestKey
    designated_worker: str
    actual_worker: str | None = None
    target_verified: bool | None = None
    ready_ns: int = 0
    dispatch_ns: int | None = None
    first_role_ns: int | None = None
    first_reasoning_ns: int | None = None
    first_content_ns: int | None = None
    response_done_ns: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cached_tokens: int | None = None
    usage_observed: bool = False
    output_text: str = ""
    marker_ok: bool | None = None
    status: str = "pending"
    error: str | None = None

    @property
    def admission_delay_ns(self) -> int | None:
        if self.dispatch_ns is None:
            return None
        return self.dispatch_ns - self.ready_ns

    @property
    def ttft_content_ns(self) -> int | None:
        if self.dispatch_ns is None or self.first_content_ns is None:
            return None
        return self.first_content_ns - self.dispatch_ns

    @property
    def ttft_reasoning_ns(self) -> int | None:
        if self.dispatch_ns is None or self.first_reasoning_ns is None:
            return None
        return self.first_reasoning_ns - self.dispatch_ns

    @property
    def e2e_ns(self) -> int | None:
        if self.dispatch_ns is None or self.response_done_ns is None:
            return None
        return self.response_done_ns - self.dispatch_ns

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["request"] = self.request.label()
        d["session_id"] = self.request.session_id
        d["turn_id"] = self.request.turn_id
        d["admission_delay_ns"] = self.admission_delay_ns
        d["ttft_content_ns"] = self.ttft_content_ns
        d["ttft_reasoning_ns"] = self.ttft_reasoning_ns
        d["e2e_ns"] = self.e2e_ns
        return d


class TraceWriter:
    """Event-level JSONL writer; every event is written and flushed as it
    happens (spec §11). ``stream=None`` keeps events in memory only."""

    def __init__(self, stream: IO[str] | None = None):
        self.stream = stream
        self.events: list[dict[str, Any]] = []

    def write(self, kind: str, **fields: Any) -> None:
        event = {"event": kind, **fields}
        self.events.append(event)
        if self.stream is not None:
            self.stream.write(json.dumps(event, sort_keys=True, default=str) + "\n")
            self.stream.flush()


@dataclass(frozen=True)
class ReplayRequest:
    key: RequestKey
    turn: TurnSpec
    prefix: PrefixKey
    max_output_tokens: int


class ReplayBackend(Protocol):
    def submit(self, request: ReplayRequest, destination: str) -> DispatchReceipt: ...

    def poll(self) -> list[tuple[RequestKey, StreamChunk]]: ...


class RunnerHooks(Protocol):
    def on_dispatch(
        self, request: RequestKey, prefix: PrefixKey, destination: str
    ) -> None: ...

    def on_response_done(
        self, request: RequestKey, prefix: PrefixKey, actual_worker: str | None
    ) -> None: ...

    def on_session_closed(self, session_id: int, prefix: PrefixKey) -> None: ...


@dataclass
class _SessionState:
    next_turn: int = 0
    next_ready_ns: int = 0
    in_flight: RequestKey | None = None
    closed: bool = False


class SessionReplayRunner:
    def __init__(
        self,
        manifest: Manifest,
        run: RunKey,
        backend: ReplayBackend,
        clock: Clock,
        trace: TraceWriter,
        hooks: RunnerHooks | None = None,
        notify_close: bool = True,
    ):
        self.manifest = manifest
        self.run = run
        self.backend = backend
        self.clock = clock
        self.trace = trace
        self.hooks = hooks
        self.notify_close = notify_close
        self.traces: dict[RequestKey, RequestTrace] = {}
        self._sessions: dict[int, _SessionState] = {
            sid: _SessionState(next_ready_ns=clock.now_ns())
            for sid in manifest.sessions
        }
        self.outstanding = 0
        self.max_outstanding = manifest.config.max_outstanding_requests
        self.invalid_target: list[RequestKey] = []

    # -- admission -----------------------------------------------------------

    def _ready_sessions(self) -> list[int]:
        now = self.clock.now_ns()
        ready = [
            (st.next_ready_ns, sid)
            for sid, st in self._sessions.items()
            if not st.closed and st.in_flight is None and st.next_ready_ns <= now
        ]
        return [sid for _, sid in sorted(ready)]

    def _dispatch(self, sid: int) -> None:
        st = self._sessions[sid]
        session = self.manifest.sessions[sid]
        truth = self.manifest.ground_truth[sid]  # runner-only
        turn = session.turns[st.next_turn]
        key = RequestKey(self.run, sid, turn.turn_id, 0)
        destination = truth.destination_by_turn[turn.turn_id]
        trace = RequestTrace(
            request=key, designated_worker=destination, ready_ns=st.next_ready_ns
        )
        self.traces[key] = trace
        req = ReplayRequest(
            key, turn, session.prefix, self.manifest.config.max_output_tokens
        )
        if self.hooks is not None:
            self.hooks.on_dispatch(key, session.prefix, destination)
        receipt = self.backend.submit(req, destination)
        trace.dispatch_ns = self.clock.now_ns()
        trace.actual_worker = receipt.actual_worker
        trace.status = "in_flight"
        if receipt.actual_worker in (None, "unknown"):
            trace.target_verified = None  # decided by the stream receipt
        else:
            trace.target_verified = receipt.target_verified
            if not receipt.target_verified:
                self.invalid_target.append(key)
        st.in_flight = key
        self.outstanding += 1
        self.trace.write(
            "dispatch",
            request=key.label(),
            session_id=sid,
            turn_id=turn.turn_id,
            designated_worker=destination,
            actual_worker=receipt.actual_worker,
            ready_ns=trace.ready_ns,
            dispatch_ns=trace.dispatch_ns,
            admission_delay_ns=trace.admission_delay_ns,
            prompt_tokens=turn.prompt_tokens,
        )

    def admit(self) -> int:
        n = 0
        for sid in self._ready_sessions():
            if self.outstanding >= self.max_outstanding:
                break
            self._dispatch(sid)
            n += 1
        return n

    # -- streaming -----------------------------------------------------------

    def _on_chunk(self, key: RequestKey, chunk: StreamChunk) -> None:
        trace = self.traces[key]
        # Usage may ride on any chunk (Dynamo 0.5.0 attaches it to every
        # delta, OpenAI sends a trailing usage-only chunk); record it wherever
        # it appears so the written response_done carries the final values.
        if chunk.usage:
            trace.usage_observed = True
            trace.prompt_tokens = chunk.usage.get("prompt_tokens")
            trace.completion_tokens = chunk.usage.get("completion_tokens")
            trace.cached_tokens = chunk.usage.get("cached_tokens")
        if chunk.kind == ChunkKind.ROLE:
            if trace.first_role_ns is None:
                trace.first_role_ns = chunk.now_ns
        elif chunk.kind == ChunkKind.REASONING and chunk.text:
            if trace.first_reasoning_ns is None:
                trace.first_reasoning_ns = chunk.now_ns
        elif chunk.kind == ChunkKind.CONTENT and chunk.text:
            if trace.first_content_ns is None:
                trace.first_content_ns = chunk.now_ns
            trace.output_text += chunk.text
        elif chunk.kind == ChunkKind.USAGE:
            pass  # recorded above
        elif chunk.kind == ChunkKind.ERROR:
            trace.error = chunk.error
            trace.status = "error"
            self._finish(key, chunk.now_ns)
        elif chunk.kind == ChunkKind.DONE:
            if trace.status != "error":
                trace.status = "ok"
            self._finish(key, chunk.now_ns)
        if chunk.worker_id and trace.actual_worker in (None, "unknown"):
            trace.actual_worker = chunk.worker_id
            trace.target_verified = chunk.worker_id == trace.designated_worker
            if not trace.target_verified:
                self.invalid_target.append(key)

    def _finish(self, key: RequestKey, now_ns: int) -> None:
        trace = self.traces[key]
        if trace.response_done_ns is not None:
            return
        trace.response_done_ns = now_ns
        if trace.target_verified is None:
            # no receipt ever arrived: never assume the target was honoured
            trace.target_verified = False
            self.invalid_target.append(key)
        session = self.manifest.sessions[key.session_id]
        turn = session.turns[key.turn_id]
        trace.marker_ok = turn.expected_marker in trace.output_text
        st = self._sessions[key.session_id]
        st.in_flight = None
        self.outstanding -= 1
        truth = self.manifest.ground_truth[key.session_id]
        gap_ns = int(truth.gap_s_by_turn[key.turn_id] * 1_000_000_000)
        st.next_turn += 1
        st.next_ready_ns = now_ns + gap_ns
        self.trace.write("response_done", **trace.as_dict())
        if self.hooks is not None:
            self.hooks.on_response_done(key, session.prefix, trace.actual_worker)
        if st.next_turn > truth.last_turn:
            st.closed = True
            if self.notify_close and self.hooks is not None:
                self.hooks.on_session_closed(key.session_id, session.prefix)
            self.trace.write("session_closed", session_id=key.session_id, now_ns=now_ns)

    def step(self) -> None:
        self.admit()
        for key, chunk in self.backend.poll():
            self._on_chunk(key, chunk)
        self.admit()

    def done(self) -> bool:
        return all(st.closed for st in self._sessions.values())

    def session_progress(self) -> dict[int, int]:
        return {sid: st.next_turn for sid, st in self._sessions.items()}


# --------------------------------------------------------------------------
# HTTP streaming backend: not exercised GPU-free; used only on approved gates
# --------------------------------------------------------------------------


@dataclass
class _HttpJob:
    key: RequestKey
    thread: threading.Thread


class HttpStreamingBackend:
    """OpenAI-compatible SSE client. One thread per in-flight request.

    ``worker_header`` names the response header that carries the actual
    worker identity; if absent, the trace records ``unknown`` and the target
    stays unverified (never assumed correct).
    """

    def __init__(
        self,
        url: str,
        model: str,
        task_header: str = "X-Task-Id",
        worker_header: str = "x-worker-id",
        destination_header: str = "X-Target-Worker",
        timeout_s: float = 600.0,
    ):
        self.url = url
        self.model = model
        self.task_header = task_header
        self.worker_header = worker_header
        self.destination_header = destination_header
        self.timeout_s = timeout_s
        self._queue: queue.Queue[tuple[RequestKey, StreamChunk]] = queue.Queue()
        self._jobs: list[_HttpJob] = []
        self._receipts = 0

    def submit(self, request: ReplayRequest, destination: str) -> DispatchReceipt:
        body = json.dumps(
            {
                "model": self.model,
                "messages": list(request.turn.messages),
                "max_tokens": request.max_output_tokens,
                "temperature": 0,
                "stream": True,
                "stream_options": {"include_usage": True},
            }
        ).encode()
        headers = {
            "Content-Type": "application/json",
            self.task_header: request.key.label(),
            self.destination_header: destination,
        }
        thread = threading.Thread(
            target=self._run, args=(request.key, body, headers), daemon=True
        )
        self._jobs.append(_HttpJob(request.key, thread))
        thread.start()
        self._receipts += 1
        # Receipt of the actual worker arrives with the stream; until then the
        # target is unverified.
        return DispatchReceipt(
            request.key, destination, "unknown", f"http-{self._receipts}"
        )

    def _run(self, key: RequestKey, body: bytes, headers: dict[str, str]) -> None:
        req = urllib.request.Request(self.url, data=body, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                worker = resp.headers.get(self.worker_header)
                # The finish_reason chunk is held back and emitted as the
                # single DONE only once the stream closes, so a trailing
                # usage-only chunk (OpenAI style) is seen by the runner before
                # response_done is written. The DONE keeps the finish time.
                finish_ns: int | None = None
                finish_worker: str | None = None
                last_usage: dict[str, int] | None = None
                for raw in resp:
                    line = raw.decode("utf-8", "replace").rstrip("\r\n")
                    obj = parse_sse_data(line)
                    if obj is None:
                        continue
                    now = time.monotonic_ns()
                    if obj.get("__done__"):
                        break
                    if obj.get("usage"):
                        last_usage = obj["usage"]
                    kind, text = classify_chunk(obj)
                    if kind == ChunkKind.DONE:
                        finish_ns = now
                        finish_worker = worker or obj.get("worker_id")
                        continue
                    self._queue.put(
                        (
                            key,
                            StreamChunk(
                                kind,
                                now,
                                text=text,
                                usage=obj.get("usage"),
                                worker_id=worker or obj.get("worker_id"),
                            ),
                        )
                    )
                self._queue.put(
                    (
                        key,
                        StreamChunk(
                            ChunkKind.DONE,
                            finish_ns if finish_ns is not None else time.monotonic_ns(),
                            usage=last_usage,
                            worker_id=finish_worker or worker,
                        ),
                    )
                )
        except Exception as exc:  # noqa: BLE001 - reported in the trace
            self._queue.put(
                (
                    key,
                    StreamChunk(ChunkKind.ERROR, time.monotonic_ns(), error=repr(exc)),
                )
            )

    def poll(self) -> list[tuple[RequestKey, StreamChunk]]:
        out: list[tuple[RequestKey, StreamChunk]] = []
        while True:
            try:
                out.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return out


@dataclass
class MonotonicClock(Clock):
    """Wall monotonic clock for real runs; do not subtract across hosts."""

    offset_ns: int = field(default_factory=time.monotonic_ns)

    def now_ns(self) -> int:
        return time.monotonic_ns()
