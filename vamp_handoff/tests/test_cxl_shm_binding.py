# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""Tests of the fixed-API shared store using the in-process EMULATED provider.

An emulated pass is not a CXL gate (spec §9). The tests check our metadata
record, single-writer/reader-lease rules under the provider lock surface, the
destroy/free ordering from the integration note, and that the ctypes binding
refuses to load or call anything that is not confirmed.
"""

import os
import sys
import time
import unittest
from pathlib import Path

HANDOFF = Path(__file__).resolve().parents[1]
if str(HANDOFF) not in sys.path:
    sys.path.insert(0, str(HANDOFF))

from vamp_cxl.cxl_shm_binding import (  # noqa: E402
    PROPOSED_SIGNATURES,
    AbiConfirmation,
    AbiNotConfirmed,
    CtypesProviderApi,
    CxlCopyJob,
    CxlCopyTransport,
    CxlSharedKVStore,
    EmulatedProviderApi,
    EntryRecord,
    EntryState,
)
from vamp_cxl.keys import (  # noqa: E402
    JobId,
    LayoutDescriptor,
    ModelKey,
    PrefixKey,
    block_hash_chain,
)
from vamp_cxl.kv_transfer_adapter import (  # noqa: E402
    CompletionProof,
    Miss,
    OffsetMapper,
    ReserveStatus,
    StoreCapacity,
    TransferEvent,
    TransferEventKind,
    TransferKind,
    TransferPriority,
)
from vamp_cxl.offload_policy import CxlState  # noqa: E402

MODEL = ModelKey("m", "t", "bf16", "v0")
BLK = 4096


def prefix(tokens=64, salt="p"):
    ids = [(i * 31 + len(salt)) % 997 for i in range(tokens)]
    return PrefixKey(MODEL, salt, block_hash_chain(ids, 32, salt), tokens)


def make_store(api=None, writer="w0", capacity_blocks=8):
    api = api or EmulatedProviderApi(64 * BLK)
    cap = StoreCapacity(
        nominal_bytes=capacity_blocks * BLK, metadata_reserve_bytes=0, block_bytes=BLK
    )
    mapper = OffsetMapper(slice_len_bytes=64 * BLK, provider_maps_slice_relative=True)
    return CxlSharedKVStore(
        api, "run1/cell/0", cap, mapper, LayoutDescriptor(32, 64, "v0"), writer
    ), api


class EmulatedStore(unittest.TestCase):
    def test_lifecycle_reserve_write_commit_read_evict(self):
        store, api = make_store()
        self.assertTrue(api.is_emulated)
        p = prefix()
        r = store.reserve(p, 3000, "w0")
        self.assertEqual(r.status, ReserveStatus.RESERVED)
        res = r.reservation
        self.assertEqual(res.rounded_bytes, BLK)
        self.assertEqual(store.state_of(p), (CxlState.RESERVED, res.generation))
        self.assertEqual(
            store.reserve(p, 3000, "w1").status, ReserveStatus.ALREADY_WRITING
        )
        store.mark_writing(res)
        self.assertEqual(store.state_of(p)[0], CxlState.WRITING)
        self.assertIsInstance(store.acquire_ready(p, "r"), Miss)
        payload = os.urandom(3000)
        api.write(store.payload_ptr(res), payload)
        chk = "abc123"
        bad = CompletionProof(
            JobId("j"),
            store.allocator_id,
            res.generation + 1,
            res.reservation_id,
            chk,
            3000,
        )
        self.assertFalse(store.commit_ready(res, bad).accepted)
        good = CompletionProof(
            JobId("j"),
            store.allocator_id,
            res.generation,
            res.reservation_id,
            chk,
            3000,
        )
        self.assertTrue(store.commit_ready(res, good).accepted)
        self.assertEqual(store.state_of(p)[0], CxlState.READY)
        rec = EntryRecord.unpack(
            api.read(api.get(store._key(p)), 8 + 4 + 4 + 8 * 5 + 64 * 3 + 32 + 8)  # + lockptr
        )
        self.assertEqual(rec.state, EntryState.READY)
        self.assertEqual(rec.checksum, chk)
        grant = store.acquire_ready(p, "reader-1", expected_generation=res.generation)
        self.assertFalse(isinstance(grant, Miss))
        self.assertEqual(
            api.read(api.get_ptr(grant.payload_ref.relative_offset), 3000), payload
        )
        self.assertEqual(store.request_evict(p), "DEFERRED_READERS_ACTIVE")
        self.assertEqual(store.state_of(p)[0], CxlState.EVICTING)
        self.assertIsInstance(store.acquire_ready(p, "reader-2"), Miss)
        calls_before = len(api.calls)
        store.release(grant.lease_id)
        self.assertEqual(store.state_of(p)[0], CxlState.ABSENT)
        # provider ordering: destroy the keyed object (never shfree it first),
        # then free our separately allocated payload buffer
        self.assertEqual(api.calls[calls_before:], ["destroy", "shfree"])
        self.assertEqual(store.occupied_bytes(), 0)
        self.assertIsNone(api.get(store._key(p)))

    def test_generation_and_mismatch_checks(self):
        store, api = make_store()
        p = prefix()
        r1 = store.reserve(p, 100, "w0").reservation
        store.abort(r1)
        self.assertEqual(store.state_of(p)[0], CxlState.ABORTING)
        self.assertEqual(store.reserve(p, 100, "w1").status, ReserveStatus.BUSY)
        self.assertEqual(store.request_evict(p), "REFUSED_WRITER_PROTECTED")
        store.settle(
            r1, TransferEvent(TransferEventKind.FAILED, JobId("x"), 0, error="e")
        )
        self.assertEqual(store.state_of(p)[0], CxlState.ABSENT)
        r2 = store.reserve(p, 100, "w1").reservation
        self.assertGreater(r2.generation, r1.generation)
        store.mark_writing(r1)  # stale: ignored
        self.assertEqual(store.state_of(p)[0], CxlState.RESERVED)
        store.mark_writing(r2)
        stale = CompletionProof(
            JobId("j"), store.allocator_id, r1.generation, r1.reservation_id, "c", 100
        )
        self.assertFalse(store.commit_ready(r2, stale).accepted)
        self.assertFalse(store.commit_ready(r1, stale).accepted)
        ok = CompletionProof(
            JobId("j"), store.allocator_id, r2.generation, r2.reservation_id, "c", 100
        )
        self.assertTrue(store.commit_ready(r2, ok).accepted)
        self.assertIsInstance(
            store.acquire_ready(p, "r", expected_generation=r1.generation), Miss
        )
        # a different model with the same token chain is a different key
        other = PrefixKey(
            ModelKey("m2", "t", "bf16", "v0"), p.salt, p.hash_chain, p.complete_tokens
        )
        self.assertEqual(store.state_of(other)[0], CxlState.ABSENT)

    def test_capacity_bounds_and_second_process_view(self):
        store, api = make_store(capacity_blocks=2)
        a = store.reserve(prefix(salt="a"), BLK, "w0")
        b = store.reserve(prefix(salt="b"), BLK, "w0")
        self.assertEqual(
            (a.status, b.status), (ReserveStatus.RESERVED, ReserveStatus.RESERVED)
        )
        self.assertEqual(
            store.reserve(prefix(salt="c"), 1, "w0").status,
            ReserveStatus.REJECTED_CAPACITY,
        )
        # a second store instance on the same provider pool sees the entries
        other, _ = make_store(api=api, writer="w1", capacity_blocks=2)
        self.assertEqual(other.state_of(prefix(salt="a"))[0], CxlState.RESERVED)
        self.assertEqual(
            other.reserve(prefix(salt="a"), 1, "w1").status,
            ReserveStatus.ALREADY_WRITING,
        )
        # bounds: a slice narrower than the arena rejects offsets past its end
        tiny_mapper = OffsetMapper(
            slice_len_bytes=BLK, provider_maps_slice_relative=True
        )
        cap = StoreCapacity(
            nominal_bytes=8 * BLK, metadata_reserve_bytes=0, block_bytes=BLK
        )
        narrow = CxlSharedKVStore(
            api, "run2", cap, tiny_mapper, LayoutDescriptor(32, 64, "v0"), "w2"
        )
        first = narrow.reserve(prefix(salt="n1"), BLK, "w2")
        # the arena already holds earlier allocations, so this lands past 1 block
        self.assertEqual(first.status, ReserveStatus.REJECTED_BOUNDS)

    def test_copy_transport_write_then_read(self):
        store, api = make_store()
        p = prefix()
        res = store.reserve(p, 10_000, "w0").reservation
        store.mark_writing(res)
        data = bytearray(os.urandom(10_000))
        fenced = []
        transport = CxlCopyTransport(api, fence=lambda ptr, n: fenced.append((ptr, n)))
        job = CxlCopyJob(
            JobId("w"),
            memoryview(data),
            store.payload_ptr(res),
            None,
            None,
            10_000,
            1024,
            res.generation,
            res.reservation_id,
            store.allocator_id,
            "chk",
            TransferKind.CXL_WRITE,
            TransferPriority.BACKGROUND,
        )
        transport.submit(job)
        transport.start(job.job_id)
        transport.wait(job.job_id)
        kinds = [e.kind for e in transport.poll()]
        self.assertEqual(
            kinds,
            [
                TransferEventKind.STARTED,
                TransferEventKind.COMPLETED,
                TransferEventKind.VISIBLE,
            ],
        )
        self.assertEqual(fenced, [(store.payload_ptr(res), 10_000)])
        self.assertEqual(api.read(store.payload_ptr(res), 10_000), bytes(data))
        sink = bytearray(10_000)
        rjob = CxlCopyJob(
            JobId("r"),
            None,
            None,
            store.payload_ptr(res),
            sink,
            10_000,
            4096,
            res.generation,
            None,
            store.allocator_id,
            "chk",
            TransferKind.CXL_READ,
            TransferPriority.DEMAND,
        )
        transport.submit(rjob)
        transport.inject_failure(rjob.job_id, at_chunk=2)
        transport.start(rjob.job_id)
        transport.wait(rjob.job_id)
        kinds = [e.kind for e in transport.poll()]
        self.assertEqual(kinds, [TransferEventKind.STARTED, TransferEventKind.FAILED])
        self.assertNotEqual(bytes(sink), bytes(data))
        # without a fence there is no VISIBLE: READY cannot be reached
        nofence = CxlCopyTransport(api)
        job2 = CxlCopyJob(
            JobId("w2"),
            memoryview(data),
            store.payload_ptr(res),
            None,
            None,
            10_000,
            4096,
            res.generation,
            res.reservation_id,
            store.allocator_id,
            "chk",
            TransferKind.CXL_WRITE,
            TransferPriority.BACKGROUND,
        )
        nofence.submit(job2)
        nofence.start(job2.job_id)
        nofence.wait(job2.job_id)
        self.assertNotIn(TransferEventKind.VISIBLE, [e.kind for e in nofence.poll()])

    def _write_job(self, store, res, data, job_id="w"):
        return CxlCopyJob(
            JobId(job_id),
            memoryview(data),
            store.payload_ptr(res),
            None,
            None,
            len(data),
            1024,
            res.generation,
            res.reservation_id,
            store.allocator_id,
            "chk",
            TransferKind.CXL_WRITE,
            TransferPriority.BACKGROUND,
        )

    def _read_job(self, store, res, sink, job_id="r"):
        return CxlCopyJob(
            JobId(job_id),
            None,
            None,
            store.payload_ptr(res),
            sink,
            len(sink),
            512,
            res.generation,
            None,
            store.allocator_id,
            "chk",
            TransferKind.CXL_READ,
            TransferPriority.DEMAND,
        )

    def test_fence_failure_is_failed_not_done(self):
        # A write whose fence fails is not complete: it must be FAILED with no
        # completion proof (so commit_ready is impossible), never DONE/VISIBLE.
        from vamp_cxl.cxl_shm_binding import JobState

        store, api = make_store()
        res = store.reserve(prefix(salt="fence"), 2048, "w0").reservation
        store.mark_writing(res)

        def bad_fence(ptr, n):
            raise OSError("fence unavailable")

        transport = CxlCopyTransport(api, fence=bad_fence)
        job = self._write_job(store, res, bytearray(os.urandom(2048)), "wf")
        transport.submit(job)
        transport.start(job.job_id)
        transport.wait(job.job_id)
        events = transport.poll()
        self.assertEqual(
            [e.kind for e in events],
            [TransferEventKind.STARTED, TransferEventKind.FAILED],
        )
        self.assertEqual(job.state, JobState.FAILED)
        self.assertIn("fence failed", events[-1].error)
        self.assertIsNone(events[-1].proof)
        self.assertFalse(job.visible_emitted)

    def test_read_without_refresh_fails_closed_on_non_emulated_provider(self):
        class _RealLike(EmulatedProviderApi):
            is_emulated = False  # coherence is not implied, as with the ctypes provider

        store, api = make_store(api=_RealLike(64 * BLK))
        res = store.reserve(prefix(salt="rd"), 1024, "w0").reservation
        store.mark_writing(res)
        data = bytearray(os.urandom(1024))
        api.write(store.payload_ptr(res), bytes(data))
        sink = bytearray(1024)
        norefresh = CxlCopyTransport(api)
        rjob = self._read_job(store, res, sink, "r-norefresh")
        norefresh.submit(rjob)
        norefresh.start(rjob.job_id)
        norefresh.wait(rjob.job_id)
        ev = norefresh.poll()
        self.assertEqual(ev[-1].kind, TransferEventKind.FAILED)
        self.assertIn("refresh", ev[-1].error)
        self.assertNotEqual(bytes(sink), bytes(data))
        refreshed = []
        withrefresh = CxlCopyTransport(
            api, refresh=lambda ptr, n: refreshed.append((ptr, n))
        )
        rjob2 = self._read_job(store, res, sink, "r-refresh")
        withrefresh.submit(rjob2)
        withrefresh.start(rjob2.job_id)
        withrefresh.wait(rjob2.job_id)
        self.assertEqual(withrefresh.poll()[-1].kind, TransferEventKind.COMPLETED)
        self.assertEqual(refreshed, [(store.payload_ptr(res), 1024)])
        self.assertEqual(bytes(sink), bytes(data))

    def test_second_instance_is_read_only_no_double_accounting(self):
        # Single-manager mode: capacity is tracked per instance, so a second
        # writer in the same namespace would over-commit the shared budget.
        store, api = make_store(capacity_blocks=2)
        self.assertFalse(store.read_only)
        other, _ = make_store(api=api, writer="w1", capacity_blocks=2)
        self.assertTrue(other.read_only)
        a = store.reserve(prefix(salt="a"), BLK, "w0")
        b = store.reserve(prefix(salt="b"), BLK, "w0")
        self.assertEqual(
            (a.status, b.status), (ReserveStatus.RESERVED, ReserveStatus.RESERVED)
        )
        self.assertEqual(
            other.reserve(prefix(salt="c"), BLK, "w1").status,
            ReserveStatus.REJECTED_READ_ONLY,
        )
        # existing entries are still reported to the read-only instance
        self.assertEqual(
            other.reserve(prefix(salt="a"), 1, "w1").status,
            ReserveStatus.ALREADY_WRITING,
        )
        self.assertEqual(store._occupied + other._occupied, 2 * BLK)

    def test_foreign_ready_entry_is_readable_via_record_lockptr(self):
        # Single-manager mode: the manager (w0) reserves/writes/commits; a
        # read-only instance on another host (w1) finds the entry, rebuilds the
        # entry lock from the record's lockptr and takes a reader lease.
        store, api = make_store()
        p = prefix(salt="shared")
        res = store.reserve(p, 4096, "w0").reservation
        store.mark_writing(res)
        data = bytes(os.urandom(4096))
        api.write(store.payload_ptr(res), data)
        import hashlib

        from vamp_cxl.cxl_shm_binding import CompletionProof

        digest = hashlib.sha256(data).hexdigest()
        proof = CompletionProof(
            "j", store.allocator_id, res.generation, res.reservation_id, digest, 4096
        )
        self.assertTrue(store.commit_ready(res, proof).accepted)
        other, _ = make_store(api=api, writer="w1")
        self.assertTrue(other.read_only)
        grant = other.acquire_ready(p, "readerB")
        self.assertNotIsInstance(grant, Miss, grant)
        self.assertEqual(grant.checksum, digest)
        self.assertEqual(api.read(api.get_ptr(grant.payload_ref.relative_offset), 4096), data)
        other.release(grant.lease_id)


class CtypesBindingRefusesUnconfirmed(unittest.TestCase):
    def test_no_library_offline(self):
        os.environ.pop("CXL_SHM_LIBRARY", None)
        with self.assertRaises(AbiNotConfirmed):
            CtypesProviderApi(AbiConfirmation({}))

    def test_unconfirmed_signature_is_never_bound(self):
        # libc stands in for "some library exists"; nothing about it is confirmed
        import ctypes.util

        libc = ctypes.util.find_library("c")
        if not libc:
            self.skipTest("no libc found")
        api = CtypesProviderApi(AbiConfirmation({}), library_path=libc)
        with self.assertRaises(AbiNotConfirmed):
            api.is_initialized()
        with self.assertRaises(AbiNotConfirmed):
            api.lock_alloc()
        sig = PROPOSED_SIGNATURES["shmalloc"]
        wrong = AbiConfirmation({"shmalloc": "0000000000000000"})
        self.assertFalse(wrong.covers(sig))
        right = AbiConfirmation({"shmalloc": sig.digest()})
        self.assertTrue(right.covers(sig))
        with self.assertRaises(AbiNotConfirmed):
            CtypesProviderApi(
                AbiConfirmation({}, library_sha256="00" * 32), library_path=libc
            )
        self.assertEqual(len(PROPOSED_SIGNATURES), 19)
        self.assertEqual(sig.digest(), sig.digest())
        time.sleep(0)


class SolabKvRecord(unittest.TestCase):
    def test_record_roundtrip_and_consistency(self):
        import sys as _sys
        solab = str(HANDOFF / "solab")
        if solab not in _sys.path:
            _sys.path.insert(0, solab)
        import cxl_kv_record as R

        sha = bytes(range(32))
        rec = R.KvRecord(3, 801 * 2621440, 801, 2621440, 0x7000008403000, 1 << 30, 140509248, 801 * 32, R.STATE_READY, sha)
        raw = rec.pack()
        self.assertEqual(len(raw), R.SIZE)
        self.assertEqual(R.KvRecord.unpack(raw), rec)
        self.assertTrue(rec.consistent())
        bad = R.KvRecord(3, 1, 801, 2621440, 1, 0, 0, 801 * 32, R.STATE_READY, sha)
        self.assertFalse(bad.consistent())
        with self.assertRaises(ValueError):
            R.KvRecord.unpack(b"\0" * R.SIZE)
        hashes = [bytes([i]) * 32 for i in range(5)]
        self.assertEqual(R.unpack_hashes(R.pack_hashes(hashes), 5), hashes)
        with self.assertRaises(ValueError):
            R.pack_hashes([b"short"])


if __name__ == "__main__":
    unittest.main()
