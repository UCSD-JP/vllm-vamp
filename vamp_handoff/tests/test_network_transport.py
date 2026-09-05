# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""Loopback tests for the B1 TCP payload transport (correctness prototype).

These move real bytes over 127.0.0.1 with stdlib sockets only. Timings are
not performance measurements.
"""

import os
import sys
import time
import unittest
from pathlib import Path

HANDOFF = Path(__file__).resolve().parents[1]
if str(HANDOFF) not in sys.path:
    sys.path.insert(0, str(HANDOFF))

from vamp_cxl.keys import JobId  # noqa: E402
from vamp_cxl.kv_transfer_adapter import (  # noqa: E402
    ExecutorLimits,
    JobState,
    TransferEventKind,
    TransferExecutor,
)
from vamp_cxl.network_transport import (  # noqa: E402
    NetworkJob,
    PayloadReceiver,
    TcpPayloadTransport,
    sha256_hex,
)


class _Clock:
    def now_ns(self):
        return time.monotonic_ns()


def wait_for(pred, timeout_s=10.0):
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.005)
    return False


class TcpTransportTests(unittest.TestCase):
    def setUp(self):
        self.receiver = PayloadReceiver()
        self.transport = TcpPayloadTransport()
        self.payload = bytearray(os.urandom(1 << 20))  # 1 MiB
        self.chunk = 64 * 1024

    def tearDown(self):
        self.receiver.close()

    def _job(self, jid="j1", chunk=None):
        return NetworkJob(
            JobId(jid),
            memoryview(self.payload),
            self.receiver.address,
            generation=7,
            chunk_bytes=chunk or self.chunk,
        )

    def _events(self, job_id):
        out = []
        self.transport.wait(job_id)
        self.transport.jobs[job_id]  # noqa: B018 - presence check
        out.extend(self.transport.poll())
        return [e for e in out if e.job_id == job_id]

    def test_roundtrip_bytes_and_checksum(self):
        job = self._job()
        self.transport.submit(job)
        self.transport.start(job.job_id)
        events = self._events(job.job_id)
        kinds = [e.kind for e in events]
        self.assertEqual(kinds[0], TransferEventKind.STARTED)
        self.assertEqual(kinds.count(TransferEventKind.COMPLETED), 1)
        self.assertEqual(
            sum(
                k
                in (
                    TransferEventKind.COMPLETED,
                    TransferEventKind.FAILED,
                    TransferEventKind.CANCELLED,
                )
                for k in kinds
            ),
            1,
        )
        proof = events[-1].proof
        self.assertEqual(proof.checksum, sha256_hex(self.payload))
        self.assertEqual(proof.generation, 7)
        self.assertEqual(proof.nbytes, len(self.payload))
        got = self.receiver.received.get(timeout=5)
        self.assertTrue(got.complete)
        self.assertEqual(got.job_id, "j1")
        self.assertEqual(bytes(got.data), bytes(self.payload))
        self.assertEqual(got.checksum, proof.checksum)
        self.assertEqual(self.transport.active_jobs(), 0)

    def test_cancel_quiesces_at_chunk_boundary(self):
        job = self._job("j2", chunk=4096)  # 256 chunks -> room to cancel mid-way
        self.transport.submit(job)
        self.transport.start(job.job_id)
        self.assertTrue(wait_for(lambda: job.chunks_done >= 2))
        self.transport.cancel(job.job_id)
        events = self._events(job.job_id)
        terminal = [
            e
            for e in events
            if e.kind
            in (
                TransferEventKind.COMPLETED,
                TransferEventKind.FAILED,
                TransferEventKind.CANCELLED,
            )
        ]
        self.assertEqual([e.kind for e in terminal], [TransferEventKind.CANCELLED])
        self.assertEqual(terminal[0].bytes_done, job.chunks_done * 4096)
        self.assertEqual(job.state, JobState.CANCELLED)
        got = self.receiver.received.get(timeout=5)
        self.assertFalse(got.complete)
        self.assertIn("cancelled", got.error)
        # destination must not treat partial bytes as a payload
        self.assertNotEqual(sha256_hex(got.data), sha256_hex(self.payload))

    def test_injected_failure_is_single_terminal(self):
        job = self._job("j3")
        self.transport.submit(job)
        self.transport.inject_failure(job.job_id, at_chunk=3)
        self.transport.start(job.job_id)
        events = self._events(job.job_id)
        terminal = [
            e
            for e in events
            if e.kind
            in (
                TransferEventKind.COMPLETED,
                TransferEventKind.FAILED,
                TransferEventKind.CANCELLED,
            )
        ]
        self.assertEqual([e.kind for e in terminal], [TransferEventKind.FAILED])
        got = self.receiver.received.get(timeout=5)
        self.assertFalse(got.complete)

    def test_cancel_before_start_and_duplicate_submit(self):
        job = self._job("j4")
        self.transport.submit(job)
        with self.assertRaises(ValueError):
            self.transport.submit(self._job("j4"))
        self.transport.cancel(job.job_id)
        events = [e for e in self.transport.poll() if e.job_id == job.job_id]
        self.assertEqual([e.kind for e in events], [TransferEventKind.CANCELLED])
        self.transport.cancel(job.job_id)  # idempotent after terminal
        self.assertEqual(self.transport.poll(), [])

    def test_executor_drives_real_transport(self):
        limits = ExecutorLimits(chunk_bytes=self.chunk)
        executor = TransferExecutor(self.transport, limits, _Clock())  # type: ignore[arg-type]
        jobs = [self._job(f"e{i}") for i in range(3)]
        for j in jobs:
            executor.submit(j)
        self.assertTrue(wait_for(lambda: all(j.is_terminal() for j in jobs)))
        events = executor.poll()
        done = [e for e in events if e.kind == TransferEventKind.COMPLETED]
        self.assertEqual(len(done), 3)
        self.assertEqual(executor.staging_in_use, 0)
        self.assertLessEqual(executor.staging_high_watermark, 3 * self.chunk)
        for _ in range(3):
            self.assertTrue(self.receiver.received.get(timeout=5).complete)


if __name__ == "__main__":
    unittest.main()
