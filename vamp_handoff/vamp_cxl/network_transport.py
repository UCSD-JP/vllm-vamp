# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""CPU-staging host-to-host payload transport over TCP (B1 correctness
prototype, spec §4).

This is a *correctness* prototype, not the high-performance network
baseline: chunked framing, per-chunk checksums, cancellation at a chunk
boundary, and exactly one terminal event per job. It moves bytes that a
worker-side bridge exposes from its CPU KV tensors; it never touches a GPU.
Faster supported transports (NIXL, RDMA) are to be evaluated separately and
are not excluded by this file.
"""

from __future__ import annotations

import hashlib
import queue
import socket
import struct
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .keys import JobId
from .kv_transfer_adapter import (
    CompletionProof,
    JobState,
    TransferEvent,
    TransferEventKind,
    TransferKind,
    TransferPriority,
)

MAGIC = b"VAMP1"
# header: magic, job id length, nbytes, chunk_bytes, generation, checksum(32)
_HEADER = struct.Struct("!5sHQIQ32s")
# per chunk: index, length, sha256[:16]
_CHUNK = struct.Struct("!IQ16s")
_STATUS = struct.Struct("!B")
STATUS_OK, STATUS_CANCEL, STATUS_ERROR = 0, 1, 2


def sha256_hex(data: bytes | memoryview) -> str:
    return hashlib.sha256(data).hexdigest()


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        part = sock.recv(n - len(buf))
        if not part:
            raise ConnectionError("peer closed during transfer")
        buf += part
    return bytes(buf)


@dataclass
class NetworkJob:
    job_id: JobId
    payload: memoryview
    destination: tuple[str, int]
    generation: int
    chunk_bytes: int
    source: str = "local"
    kind: TransferKind = TransferKind.NETWORK_COPY
    priority: TransferPriority = TransferPriority.DEMAND
    checksum: str = ""
    submitted_ns: int = 0
    started_ns: int | None = None
    finished_ns: int | None = None
    state: JobState = JobState.QUEUED
    chunks_done: int = 0
    cancel_requested: bool = False
    fail_at_chunk: int | None = None
    terminal_emitted: bool = False
    thread: threading.Thread | None = field(default=None, repr=False)

    @property
    def nbytes(self) -> int:
        return self.payload.nbytes

    @property
    def chunks_total(self) -> int:
        return max(1, -(-self.nbytes // self.chunk_bytes))

    def is_terminal(self) -> bool:
        return self.state in (JobState.DONE, JobState.FAILED, JobState.CANCELLED)


@dataclass
class ReceivedPayload:
    job_id: str
    generation: int
    nbytes: int
    checksum: str
    data: bytearray
    complete: bool
    error: str | None = None


class PayloadReceiver:
    """Destination side. Receives into a caller-provided buffer allocator so
    the real bridge can hand over pinned CPU block memory."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        allocate: Callable[[str, int, int], bytearray | memoryview] | None = None,
    ):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((host, port))
        self._sock.listen(16)
        self._sock.settimeout(0.2)
        self.address: tuple[str, int] = self._sock.getsockname()
        self._allocate = allocate or (lambda job_id, gen, n: bytearray(n))
        self.received: queue.Queue[ReceivedPayload] = queue.Queue()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        self._sock.close()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except TimeoutError:
                continue
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        with conn:
            conn.settimeout(30)
            head = _recv_exact(conn, _HEADER.size)
            magic, id_len, nbytes, chunk_bytes, generation, checksum = _HEADER.unpack(
                head
            )
            if magic != MAGIC:
                conn.sendall(_STATUS.pack(STATUS_ERROR))
                return
            job_id = _recv_exact(conn, id_len).decode()
            buf = self._allocate(job_id, generation, nbytes)
            view = memoryview(buf)
            received = 0
            error: str | None = None
            complete = False
            try:
                while received < nbytes:
                    ch = _recv_exact(conn, _CHUNK.size)
                    idx, length, digest = _CHUNK.unpack(ch)
                    if length == 0:
                        # sender-side cancel/error marker
                        status = _STATUS.unpack(_recv_exact(conn, _STATUS.size))[0]
                        error = (
                            "cancelled by sender"
                            if status == STATUS_CANCEL
                            else "sender error"
                        )
                        break
                    data = _recv_exact(conn, length)
                    if hashlib.sha256(data).digest()[:16] != digest:
                        error = f"chunk {idx} checksum mismatch"
                        break
                    view[received : received + length] = data
                    received += length
                else:
                    complete = sha256_hex(view) == checksum.hex()
                    if not complete:
                        error = "payload checksum mismatch"
                conn.sendall(_STATUS.pack(STATUS_OK if complete else STATUS_ERROR))
            except OSError as exc:
                error = f"socket error: {exc}"
            self.received.put(
                ReceivedPayload(
                    job_id=job_id,
                    generation=generation,
                    nbytes=nbytes,
                    checksum=checksum.hex(),
                    data=buf if isinstance(buf, bytearray) else bytearray(view),
                    complete=complete,
                    error=error,
                )
            )


class TcpPayloadTransport:
    """Source side. Same submit/start/cancel/poll surface as FakeTransport so
    the executor and coordinator do not care which one they drive."""

    def __init__(self, clock_ns: Callable[[], int] = time.monotonic_ns):
        self.clock_ns = clock_ns
        self.jobs: dict[JobId, NetworkJob] = {}
        self._events: queue.Queue[TransferEvent] = queue.Queue()

    def submit(self, job: NetworkJob) -> JobId:
        if job.job_id in self.jobs:
            raise ValueError(f"duplicate job {job.job_id}")
        job.checksum = sha256_hex(job.payload)
        job.submitted_ns = self.clock_ns()
        self.jobs[job.job_id] = job
        return job.job_id

    def start(self, job_id: JobId) -> None:
        job = self.jobs[job_id]
        assert job.state == JobState.QUEUED
        job.state = JobState.RUNNING
        job.started_ns = self.clock_ns()
        self._events.put(
            TransferEvent(TransferEventKind.STARTED, job_id, job.started_ns)
        )
        job.thread = threading.Thread(target=self._send, args=(job,), daemon=True)
        job.thread.start()

    def cancel(self, job_id: JobId) -> None:
        job = self.jobs[job_id]
        if job.is_terminal():
            return
        job.cancel_requested = True
        if job.state == JobState.QUEUED:
            self._terminate(job, JobState.CANCELLED, "cancelled before start")

    def inject_failure(self, job_id: JobId, at_chunk: int) -> None:
        self.jobs[job_id].fail_at_chunk = at_chunk

    def _terminate(self, job: NetworkJob, state: JobState, error: str | None) -> None:
        assert not job.terminal_emitted, "second terminal event"
        job.terminal_emitted = True
        job.state = state
        job.finished_ns = self.clock_ns()
        kind = {
            JobState.DONE: TransferEventKind.COMPLETED,
            JobState.FAILED: TransferEventKind.FAILED,
            JobState.CANCELLED: TransferEventKind.CANCELLED,
        }[state]
        proof = None
        if state == JobState.DONE:
            proof = CompletionProof(
                job_id=job.job_id,
                allocator_id=None,
                generation=job.generation,
                reservation_id=None,
                checksum=job.checksum,
                nbytes=job.nbytes,
            )
        self._events.put(
            TransferEvent(
                kind,
                job.job_id,
                job.finished_ns,
                proof=proof,
                error=error,
                bytes_done=min(job.nbytes, job.chunks_done * job.chunk_bytes),
            )
        )

    def _send(self, job: NetworkJob) -> None:
        try:
            with socket.create_connection(job.destination, timeout=30) as sock:
                sock.sendall(
                    _HEADER.pack(
                        MAGIC,
                        len(job.job_id.value),
                        job.nbytes,
                        job.chunk_bytes,
                        job.generation,
                        bytes.fromhex(job.checksum),
                    )
                    + job.job_id.value.encode()
                )
                for idx in range(job.chunks_total):
                    if job.cancel_requested:
                        sock.sendall(
                            _CHUNK.pack(idx, 0, b"\0" * 16)
                            + _STATUS.pack(STATUS_CANCEL)
                        )
                        self._terminate(
                            job, JobState.CANCELLED, "cancelled at chunk boundary"
                        )
                        return
                    if job.fail_at_chunk is not None and idx + 1 >= job.fail_at_chunk:
                        sock.sendall(
                            _CHUNK.pack(idx, 0, b"\0" * 16) + _STATUS.pack(STATUS_ERROR)
                        )
                        self._terminate(job, JobState.FAILED, "injected sender error")
                        return
                    start = idx * job.chunk_bytes
                    chunk = job.payload[start : start + job.chunk_bytes]
                    sock.sendall(
                        _CHUNK.pack(
                            idx, chunk.nbytes, hashlib.sha256(chunk).digest()[:16]
                        )
                    )
                    sock.sendall(chunk)
                    job.chunks_done = idx + 1
                status = _STATUS.unpack(_recv_exact(sock, _STATUS.size))[0]
                if status == STATUS_OK:
                    self._terminate(job, JobState.DONE, None)
                else:
                    self._terminate(job, JobState.FAILED, "receiver reported error")
        except OSError as exc:
            if not job.terminal_emitted:
                self._terminate(job, JobState.FAILED, f"socket error: {exc}")

    def poll(self) -> list[TransferEvent]:
        out: list[TransferEvent] = []
        while True:
            try:
                out.append(self._events.get_nowait())
            except queue.Empty:
                return out

    def active_jobs(self) -> int:
        return sum(1 for j in self.jobs.values() if not j.is_terminal())

    def wait(self, job_id: JobId, timeout_s: float = 30.0) -> None:
        job = self.jobs[job_id]
        if job.thread is not None:
            job.thread.join(timeout=timeout_s)
