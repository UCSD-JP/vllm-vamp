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
            api.read(api.get(store._key(p)), 8 + 4 + 4 + 8 * 5 + 64 * 3 + 32)
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
        self.assertEqual(len(PROPOSED_SIGNATURES), 10)
        self.assertEqual(sig.digest(), sig.digest())
        time.sleep(0)


if __name__ == "__main__":
    unittest.main()
