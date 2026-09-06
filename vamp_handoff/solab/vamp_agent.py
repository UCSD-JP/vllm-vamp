# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""In-engine agent for G-D (A->B network KV import), loaded via spec_module_path.

Runs inside the vLLM EngineCore process (uniproc executor: scheduler and worker
share the process, so both ``current_manager()`` and ``current_bridge()`` are
valid here). It extends the probe (file log) with:

* source side: on the first READY run of at least ``VAMP_PIN_MIN_BLOCKS``
  blocks the listener takes an export lease *synchronously on the scheduler
  thread* (the engine may run no further step during the gap, so this is the
  only reliable moment to pin); the pinned run is the export candidate.
* a JSON-lines control server (``VAMP_AGENT_PORT``): status / export /
  import_prepare / import_status / release.
* destination side: a ``PayloadReceiver`` (``VAMP_PAYLOAD_PORT``); the import
  reservation and commit are posted to the manager mailbox and become
  effective when the scheduler next runs a step (the runner sends a tiny
  "nudge" request to force one while the engine is otherwise idle).

Scope: one candidate, one transfer at a time, no cancellation; on any error
the stage is recorded and the caller aborts the cell.
"""
import ctypes
import hashlib
import json
import os
import socket
import sys
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor

from vamp_cxl import vllm_binding as _b
from vamp_cxl.keys import JobId
from vamp_cxl.network_transport import NetworkJob, PayloadReceiver, TcpPayloadTransport, sha256_hex

# CXL path (G-F): provider library via our ctypes binding; loaded lazily on first use.
_CXL_LIB = os.environ.get("CXL_SHM_LIBRARY")
_CXL = {"api": None}
# The provider keeps its rank identity (my_id) in thread-local storage: a lock or
# allocator call from any thread other than the one that ran cxl_shm_init sees id
# -1 and aborts the whole EngineCore after the lock timeout (observed 2026-09-06,
# cleanup on a second RPC thread). Every provider call therefore runs on this one
# long-lived thread; the control server hands CXL commands to it and waits.
_CXL_THREAD_PREFIX = "vamp-cxl"
_CXL_EXEC = ThreadPoolExecutor(max_workers=1, thread_name_prefix=_CXL_THREAD_PREFIX)


def _cxl_run(fn, *args, **kwargs):
    if threading.current_thread().name.startswith(_CXL_THREAD_PREFIX):
        return fn(*args, **kwargs)
    return _CXL_EXEC.submit(fn, *args, **kwargs).result()


def _cxl_api():
    if not threading.current_thread().name.startswith(_CXL_THREAD_PREFIX):
        raise RuntimeError("provider calls must run on the vamp-cxl thread (my_id is thread-local); use _cxl_run")
    if _CXL["api"] is None:
        if not _CXL_LIB:
            raise RuntimeError("CXL_SHM_LIBRARY not set")
        here = os.path.dirname(os.path.abspath(__file__))
        if here not in sys.path:
            sys.path.insert(0, here)
        from vamp_cxl.cxl_shm_binding import CtypesProviderApi
        from cxl_abi_solab import solab_confirmation
        api = CtypesProviderApi(solab_confirmation(_CXL_LIB), library_path=_CXL_LIB)
        api.connect()  # this EngineCore process becomes a rank on its node
        _CXL["api"] = api
        _log("cxl_connected", lib=_CXL_LIB)
    return _CXL["api"]

_PATH = os.environ.get("VAMP_PROBE_FILE")
_LOCK = threading.Lock()
_PIN_MIN = int(os.environ.get("VAMP_PIN_MIN_BLOCKS", "64"))
# Only an export source pins; a destination worker keeps its ordinary CPU cache and
# must not hold export pins (review 2026-09-06: no side-effect pin at B).
_PIN_ENABLED = os.environ.get("VAMP_AGENT_PIN", "1") == "1"
_AGENT_PORT = int(os.environ.get("VAMP_AGENT_PORT", "0"))
_PAYLOAD_PORT = int(os.environ.get("VAMP_PAYLOAD_PORT", "0"))
_CHUNK = 8 * 1024 * 1024


def _log(event, **fields):
    if not _PATH:
        return
    mgr = _b.current_manager()
    rec = {"ts": time.time(), "pid": os.getpid(), "tid": threading.get_ident(), "event": event}
    rec.update(fields)
    if mgr is not None:
        rec["counters"] = dict(mgr.counters)
        rec["active_leases"] = mgr.active_leases()
    with _LOCK, open(_PATH, "a") as f:
        f.write(json.dumps(rec) + "\n")


class AgentState:
    def __init__(self):
        self.candidate = None  # ExportLease of the pinned prefix run
        self.candidate_hashes = None
        self.import_hashes = None
        self.import_future: Future | None = None
        self.import_reservation = None
        self.import_stage = "idle"
        self.import_error = None
        self.payload = None  # ReceivedPayload
        self.commit_future: Future | None = None
        self.export_result = None
        self.cxl_export = None  # ptrs kept for cleanup after the destination is done


STATE = AgentState()


class AgentListener:
    def __init__(self):
        self.ready_calls = 0
        self.ready_blocks = 0

    def on_blocks_ready(self, block_hashes):
        self.ready_calls += 1
        self.ready_blocks += len(block_hashes)
        _log("ready", n_blocks=len(block_hashes), ready_calls=self.ready_calls,
             ready_blocks=self.ready_blocks, hashes_head=[bytes(h).hex()[:16] for h in block_hashes[:3]])
        mgr = _b.current_manager()
        if (_PIN_ENABLED and STATE.candidate is None and mgr is not None and len(block_hashes) >= _PIN_MIN):
            lease = mgr.acquire_export_lease(list(block_hashes))  # owner thread: allowed
            if lease is not None:
                STATE.candidate = lease
                STATE.candidate_hashes = [bytes(h) for h in block_hashes]
                _log("candidate_pinned", n_blocks=len(lease.block_ids), lease_id=lease.lease_id)
            else:
                _log("candidate_pin_miss", n_blocks=len(block_hashes))

    def on_blocks_evicted(self, block_hashes):
        _log("evicted", n_blocks=len(block_hashes))


def _status():
    mgr = _b.current_manager()
    br = _b.current_bridge()
    return {
        "ok": True,
        "candidate_blocks": None if STATE.candidate is None else len(STATE.candidate.block_ids),
        "candidate_hashes_head": None if STATE.candidate_hashes is None else [h.hex()[:16] for h in STATE.candidate_hashes[:3]],
        "active_leases": None if mgr is None else mgr.active_leases(),
        "counters": None if mgr is None else dict(mgr.counters),
        "bridge_block_bytes": None if br is None else br.block_bytes,
        "import_stage": STATE.import_stage,
        "import_error": STATE.import_error,
        "import_reservation_blocks": None if STATE.import_reservation is None else len(STATE.import_reservation.block_ids),
        "import_evicted": None if STATE.import_reservation is None else len(STATE.import_reservation.evicted),
        "payload_received": None if STATE.payload is None else {"nbytes": STATE.payload.nbytes, "complete": STATE.payload.complete, "error": STATE.payload.error},
        "export_result": STATE.export_result,
        "payload_port": None if _RECEIVER is None else _RECEIVER.address[1],
        # cleanup-gate fields (review 2026-09-06): everything a finished cell must have released
        "cxl_import_stage": CXL_IMPORT.stage,
        "cxl_export_held": None if STATE.cxl_export is None else {
            "key": STATE.cxl_export["key"], "nbytes": STATE.cxl_export["nbytes"], "lock_freed": STATE.cxl_export.get("lock_freed", False)},
        "received_queue": None if _RECEIVER is None else _RECEIVER.received.qsize(),
    }


def _export(host, port, inject_fail_chunk=None):
    br = _b.current_bridge()
    lease = STATE.candidate
    if lease is None or br is None:
        return {"ok": False, "error": "no candidate or bridge"}
    t0 = time.time()
    payload = br.gather(lease.block_ids)
    t_gather = time.time() - t0
    checksum = sha256_hex(payload)
    tr = TcpPayloadTransport()
    job = NetworkJob(JobId("gd-export"), memoryview(payload), (host, int(port)), 1, _CHUNK)
    tr.submit(job)
    if inject_fail_chunk is not None:
        # failure injection after the destination reserved: the sender stops with an error
        # marker at this chunk, so B receives a partial payload and must abort + discard it
        tr.inject_failure(job.job_id, int(inject_fail_chunk))
    t1 = time.time()
    tr.start(job.job_id)
    tr.wait(job.job_id, timeout_s=600)
    events = [e.kind.name for e in tr.poll()]
    res = {"ok": job.state.name == "DONE", "state": job.state.name, "events": events,
           "nbytes": len(payload), "n_blocks": len(lease.block_ids), "checksum": checksum,
           "gather_s": round(t_gather, 3), "send_s": round(time.time() - t1, 3)}
    STATE.export_result = res
    _log("export_done", **res)
    return res


def _import_prepare(hashes_hex):
    mgr = _b.current_manager()
    if mgr is None:
        return {"ok": False, "error": "no manager"}
    from vllm.v1.core.kv_cache_utils import BlockHash
    STATE.import_hashes = [BlockHash(bytes.fromhex(h)) for h in hashes_hex]
    STATE.import_stage = "reserve_posted"
    STATE.import_error = None
    STATE.import_future = mgr.reserve_import_async(list(STATE.import_hashes))
    _log("import_reserve_posted", n_blocks=len(STATE.import_hashes))
    return {"ok": True, "n_blocks": len(STATE.import_hashes), "stage": STATE.import_stage}


def _advance_import():
    """Progress the destination state machine; called from import_status."""
    br = _b.current_bridge()
    mgr = _b.current_manager()
    if STATE.import_stage == "reserve_posted" and STATE.import_future is not None and STATE.import_future.done():
        try:
            res = STATE.import_future.result()
        except Exception as exc:  # noqa: BLE001
            STATE.import_stage, STATE.import_error = "failed", f"reserve raised: {exc!r}"
            _log("import_failed", error=STATE.import_error); return
        if res is None:
            STATE.import_stage, STATE.import_error = "failed", "reserve_import returned None (no room)"
            _log("import_failed", error=STATE.import_error); return
        STATE.import_reservation = res
        STATE.import_stage = "reserved"
        _log("import_reserved", n_blocks=len(res.block_ids), evicted=len(res.evicted), to_store=len(res.block_hashes))
    if STATE.import_stage == "reserved" and _RECEIVER is not None:
        try:
            STATE.payload = _RECEIVER.received.get_nowait()
        except Exception:  # noqa: BLE001 - queue.Empty
            pass
        else:
            STATE.import_stage = "payload_received"
            _log("import_payload_received", nbytes=STATE.payload.nbytes, complete=STATE.payload.complete, error=STATE.payload.error)
    if STATE.import_stage == "payload_received":
        res = STATE.import_reservation; p = STATE.payload
        if not p.complete:
            STATE.import_stage, STATE.import_error = "failed", f"payload incomplete: {p.error}"
            STATE.commit_future = mgr.commit_import_async(res, False)
            _log("import_failed", error=STATE.import_error); return
        expected = len(res.block_ids) * br.block_bytes
        if p.nbytes != expected or len(res.block_hashes) != len(STATE.import_hashes):
            STATE.import_stage = "failed"
            STATE.import_error = f"layout mismatch: payload {p.nbytes} B vs {len(res.block_ids)} x {br.block_bytes}; to_store {len(res.block_hashes)} of {len(STATE.import_hashes)}"
            STATE.commit_future = mgr.commit_import_async(res, False)
            _log("import_failed", error=STATE.import_error); return
        t0 = time.time()
        br.import_payload(res.block_ids, memoryview(p.data))
        STATE.import_stage = "written"
        _log("import_written", nbytes=p.nbytes, write_s=round(time.time() - t0, 3))
        STATE.commit_future = mgr.commit_import_async(res, True)
        STATE.import_stage = "commit_posted"
        _log("import_commit_posted")
    if STATE.import_stage == "commit_posted" and STATE.commit_future is not None and STATE.commit_future.done():
        try:
            STATE.commit_future.result()
            STATE.import_stage = "committed"
            _log("import_committed", n_blocks=len(STATE.import_reservation.block_ids))
        except Exception as exc:  # noqa: BLE001
            STATE.import_stage, STATE.import_error = "failed", f"commit raised: {exc!r}"
            _log("import_failed", error=STATE.import_error)


class CxlImportState:
    def __init__(self):
        self.key = None
        self.rec = None
        self.hashes = None
        self.future = None
        self.reservation = None
        self.commit_future = None
        self.stage = "idle"
        self.error = None
        self.timings = {}
        self.verify = "sha256"


CXL_IMPORT = CxlImportState()


def _cxl_export(key, verify="sha256"):
    """A side: copy the pinned prefix run into the CXL payload arena and publish it."""
    import cxl_kv_record as R
    br = _b.current_bridge(); lease = STATE.candidate
    if lease is None or br is None:
        return {"ok": False, "error": "no candidate or bridge"}
    api = _cxl_api()
    t = {}
    t0 = time.time(); payload = br.gather(lease.block_ids); t["gather_s"] = round(time.time() - t0, 3)
    n = len(payload)
    t0 = time.time(); pptr = api.payload_alloc(n); t["payload_alloc_s"] = round(time.time() - t0, 3)
    poff = api.get_offset(pptr)
    if poff + n > 64 * 1024**3:  # our slice is 64 GiB; the provider arena spans 128 GiB
        api.payload_free(pptr, n)
        return {"ok": False, "error": f"payload would cross the 64 GiB slice (off {poff}, n {n})"}
    t0 = time.time()
    src = (ctypes.c_char * n).from_buffer(payload)
    ctypes.memmove(pptr, src, n)
    t["cxl_write_s"] = round(time.time() - t0, 3)
    t0 = time.time(); api.fence(pptr, n); t["fence_s"] = round(time.time() - t0, 3)
    if verify == "none":
        # verification skipped (ablation): the record carries a zero digest and the
        # importer must have been told the same mode; reported as skipped, not verified
        digest = bytes(32); t["sha256_s"] = 0.0
    else:
        t0 = time.time(); digest = hashlib.sha256(payload).digest(); t["sha256_s"] = round(time.time() - t0, 3)
    t["verify"] = verify
    blob = R.pack_hashes(STATE.candidate_hashes)
    hptr = api.shmalloc(len(blob)); api.write(hptr, blob); api.fence(hptr, len(blob))
    rptr = api.shmalloc(R.SIZE); lock = api.lock_alloc()
    rec = R.KvRecord(1, n, len(lease.block_ids), br.block_bytes, lock, poff, api.get_offset(hptr), len(blob), R.STATE_READY, digest)
    api.lock_acquire(lock)
    api.write(rptr, rec.pack()); api.fence(rptr, R.SIZE)
    api.lock_release(lock)
    api.put(key, rptr)
    res = {"ok": True, "key": key, "nbytes": n, "n_blocks": len(lease.block_ids), "payload_off": poff,
           "hashes_off": rec.hashes_off, "lockptr": lock, "sha256": digest.hex(), **t}
    # keep everything cleanup needs; freed only after the controller confirms B is done
    STATE.cxl_export = {"key": key, "rptr": rptr, "hptr": hptr, "hashes_len": len(blob),
                        "pptr": pptr, "nbytes": n, "lock": lock}
    STATE.export_result = res
    _log("cxl_export_done", **res)
    return res


def _cleanup(scope, confirmed):
    """Controller-serialized release. Export-side resources are freed only after the
    controller confirms the destination import is committed or aborted and nothing
    is still reading (provider contract: free locks -> destroy key -> free payload).
    The export lease release is posted to the manager mailbox and takes effect at
    the next scheduler step (the controller nudges)."""
    done = {}
    mgr = _b.current_manager()
    if scope in ("export", "all"):
        ex = getattr(STATE, "cxl_export", None)
        if ex is not None:
            if not confirmed:
                return {"ok": False, "error": "export cleanup requires confirmed=true (destination done)"}
            api = _cxl_api()
            if not ex.get("lock_freed"):
                api.lock_free(ex["lock"]); ex["lock_freed"] = True
            try:
                api.destroy(ex["key"])           # owns the record memory
            except Exception as exc:  # noqa: BLE001
                # review 2026-09-06: a failed key removal must not be papered over by freeing
                # the payload; keep every pointer for the operator and fail the cell
                residual = {"key": ex["key"], "payload_bytes": ex["nbytes"], "payload_ptr": ex["pptr"],
                            "hashes_ptr": ex["hptr"], "hashes_bytes": ex["hashes_len"], "lock_freed": True}
                _log("cleanup_failed", scope=scope, error=repr(exc), residual=residual)
                return {"ok": False, "error": f"destroy({ex['key']}) failed: {exc!r}", "residual": residual}
            api.payload_free(ex["pptr"], ex["nbytes"])
            api.shfree(ex["hptr"])
            done["cxl_freed"] = {"key": ex["key"], "payload_bytes": ex["nbytes"], "hashes_bytes": ex["hashes_len"]}
            STATE.cxl_export = None
        if STATE.candidate is not None and mgr is not None:
            fut = mgr.release_export_lease_async(STATE.candidate)
            done["lease_release_posted"] = STATE.candidate.lease_id
            STATE.candidate = None; STATE.candidate_hashes = None
    if scope in ("import", "all"):
        S = CXL_IMPORT
        if S.stage not in ("idle", "committed", "failed"):
            return {"ok": False, "error": f"cxl import still in progress (stage {S.stage})"}
        if S.commit_future is not None and not S.commit_future.done():
            return {"ok": False, "error": f"cxl import {S.stage}: commit/abort not yet drained (nudge and retry)"}
        # network import state (review 2026-09-06: TCP reservation / received payload were not covered)
        if STATE.import_stage not in ("idle", "committed", "failed"):
            return {"ok": False, "error": f"network import still in progress (stage {STATE.import_stage}); use import_abort"}
        if STATE.commit_future is not None and not STATE.commit_future.done():
            return {"ok": False, "error": f"network import {STATE.import_stage}: commit/abort not yet drained (nudge and retry)"}
        done["cxl_import_stage_reset_from"] = S.stage
        S.__init__()
        done["net_import_stage_reset_from"] = STATE.import_stage
        done["net_payload_discarded_bytes"] = None if STATE.payload is None else STATE.payload.nbytes
        STATE.import_hashes = None; STATE.import_future = None; STATE.import_reservation = None
        STATE.import_stage = "idle"; STATE.import_error = None; STATE.payload = None; STATE.commit_future = None
        n = 0
        if _RECEIVER is not None:
            while True:
                try:
                    _RECEIVER.received.get_nowait(); n += 1
                except Exception:  # noqa: BLE001 - queue.Empty
                    break
        done["received_queue_discarded"] = n
        if STATE.candidate is not None and mgr is not None:   # legacy auto-pin at a destination
            mgr.release_export_lease_async(STATE.candidate)
            done["dest_pin_release_posted"] = STATE.candidate.lease_id
            STATE.candidate = None; STATE.candidate_hashes = None
    _log("cleanup", scope=scope, **{k: v for k, v in done.items() if k != "cxl_freed"}, cxl_freed=done.get("cxl_freed"))
    return {"ok": True, **done}


def _cxl_import_prepare(key, inject=None, verify="sha256"):
    """B side: read the record + hashes under the entry lock, post the reservation.
    inject="checksum" corrupts the expected digest so the post-reservation verify fails
    (exercises the abort/cleanup path without touching the shared data)."""
    import cxl_kv_record as R
    mgr = _b.current_manager()
    if mgr is None:
        return {"ok": False, "error": "no manager"}
    api = _cxl_api()
    rptr = api.get(key)
    if not rptr:
        return {"ok": False, "error": f"key {key} not found"}
    api.refresh(rptr, R.SIZE)
    rec = R.KvRecord.unpack(api.read(rptr, R.SIZE))
    api.lock_acquire(rec.lockptr)
    try:
        api.refresh(rptr, R.SIZE)
        rec = R.KvRecord.unpack(api.read(rptr, R.SIZE))
        if rec.state != R.STATE_READY or not rec.consistent():
            return {"ok": False, "error": f"record not READY/consistent: {rec}"}
        hptr = api.get_ptr(rec.hashes_off)
        api.refresh(hptr, rec.hashes_len)
        blob = api.read(hptr, rec.hashes_len)
    finally:
        api.lock_release(rec.lockptr)
    from vllm.v1.core.kv_cache_utils import BlockHash
    CXL_IMPORT.__init__()
    CXL_IMPORT.key, CXL_IMPORT.rec = key, rec
    CXL_IMPORT.verify = verify
    if inject == "checksum":
        if verify == "none":
            return {"ok": False, "error": "checksum injection needs a verification mode"}
        rec = R.KvRecord(rec.generation, rec.nbytes, rec.n_blocks, rec.block_bytes, rec.lockptr,
                         rec.payload_off, rec.hashes_off, rec.hashes_len, rec.state, bytes(32))
        CXL_IMPORT.rec = rec
        _log("cxl_import_inject", inject=inject)
    CXL_IMPORT.hashes = [BlockHash(h) for h in R.unpack_hashes(blob, rec.n_blocks)]
    CXL_IMPORT.future = mgr.reserve_import_async(list(CXL_IMPORT.hashes))
    CXL_IMPORT.stage = "reserve_posted"
    _log("cxl_import_reserve_posted", key=key, n_blocks=rec.n_blocks, nbytes=rec.nbytes)
    return {"ok": True, "stage": CXL_IMPORT.stage, "n_blocks": rec.n_blocks, "nbytes": rec.nbytes}


def _cxl_import_advance():
    import cxl_kv_record as R
    S = CXL_IMPORT; br = _b.current_bridge(); mgr = _b.current_manager(); api = _cxl_api()
    if S.stage == "reserve_posted" and S.future is not None and S.future.done():
        try:
            res = S.future.result()
        except Exception as exc:  # noqa: BLE001
            S.stage, S.error = "failed", f"reserve raised: {exc!r}"; _log("cxl_import_failed", error=S.error); return
        if res is None:
            S.stage, S.error = "failed", "reserve_import returned None"; _log("cxl_import_failed", error=S.error); return
        S.reservation = res; S.stage = "reserved"
        _log("cxl_import_reserved", n_blocks=len(res.block_ids), evicted=len(res.evicted), to_store=len(res.block_hashes))
    if S.stage == "reserved":
        rec = S.rec; res = S.reservation
        if len(res.block_hashes) != rec.n_blocks or rec.block_bytes != br.block_bytes:
            S.stage, S.error = "failed", f"layout mismatch: to_store {len(res.block_hashes)} vs {rec.n_blocks}, block_bytes {rec.block_bytes} vs {br.block_bytes}"
            S.commit_future = mgr.commit_import_async(res, False); _log("cxl_import_failed", error=S.error); return
        pptr = api.get_ptr(rec.payload_off)
        t0 = time.time(); api.refresh(pptr, rec.nbytes); S.timings["refresh_s"] = round(time.time() - t0, 3)
        view = memoryview((ctypes.c_char * rec.nbytes).from_address(pptr)).cast("B")  # view of the CXL mapping; import_payload copies it into the CPU tier
        if getattr(S, "verify", "sha256") == "none":
            S.timings["sha256_s"] = 0.0; S.timings["verify"] = "skipped"
        else:
            t0 = time.time(); digest = hashlib.sha256(view).digest(); S.timings["sha256_s"] = round(time.time() - t0, 3)
            S.timings["verify"] = "sha256"
            if digest != rec.sha256:
                S.stage, S.error = "failed", f"checksum mismatch after refresh: {digest.hex()} != {rec.sha256.hex()}"
                S.commit_future = mgr.commit_import_async(res, False); _log("cxl_import_failed", error=S.error); return
        t0 = time.time(); br.import_payload(res.block_ids, view); S.timings["cxl_to_cpu_s"] = round(time.time() - t0, 3)
        S.commit_future = mgr.commit_import_async(res, True); S.stage = "commit_posted"
        _log("cxl_import_written", nbytes=rec.nbytes, **S.timings)
    if S.stage == "commit_posted" and S.commit_future is not None and S.commit_future.done():
        try:
            S.commit_future.result(); S.stage = "committed"; _log("cxl_import_committed", n_blocks=len(S.reservation.block_ids), **S.timings)
        except Exception as exc:  # noqa: BLE001
            S.stage, S.error = "failed", f"commit raised: {exc!r}"; _log("cxl_import_failed", error=S.error)


def _import_abort():
    """Controller-requested abort of a network reservation whose payload will not arrive
    (the source transfer failed before sending anything). Posts commit(False); it takes
    effect at the next scheduler step, so the controller nudges before cleanup."""
    mgr = _b.current_manager()
    _advance_import()
    if STATE.import_stage == "reserved":
        STATE.commit_future = mgr.commit_import_async(STATE.import_reservation, False)
        STATE.import_stage, STATE.import_error = "failed", "aborted by controller"
        _log("import_aborted_by_controller", n_blocks=len(STATE.import_reservation.block_ids))
        return {"ok": True, "stage": STATE.import_stage}
    if STATE.import_stage in ("idle", "committed", "failed"):
        return {"ok": True, "stage": STATE.import_stage, "note": "nothing to abort"}
    return {"ok": False, "stage": STATE.import_stage, "error": "reservation not resolved yet; nudge and retry"}


def _release():
    mgr = _b.current_manager()
    if mgr is None or STATE.candidate is None:
        return {"ok": False, "error": "nothing to release"}
    fut = mgr.release_export_lease_async(STATE.candidate)
    STATE.candidate = None
    STATE.candidate_hashes = None
    _log("release_posted")
    return {"ok": True, "posted": True, "note": "effective at the next scheduler step"}


def _handle(conn):
    with conn:
        f = conn.makefile("rwb")
        line = f.readline()
        if not line:
            return
        try:
            req = json.loads(line)
            cmd = req.get("cmd")
            if cmd == "status":
                _advance_import(); out = _status()
            elif cmd == "export":
                out = _export(req["host"], req["port"], req.get("inject_fail_chunk"))
            elif cmd == "import_prepare":
                out = _import_prepare(req["hashes"])
            elif cmd == "import_abort":
                out = _import_abort()
            elif cmd == "import_status":
                _advance_import(); out = {"ok": True, "stage": STATE.import_stage, "error": STATE.import_error,
                                           "reservation_blocks": None if STATE.import_reservation is None else len(STATE.import_reservation.block_ids)}
            elif cmd == "cxl_export":
                out = _cxl_run(_cxl_export, req["key"], req.get("verify", "sha256"))
            elif cmd == "cxl_import_prepare":
                out = _cxl_run(_cxl_import_prepare, req["key"], req.get("inject"), req.get("verify", "sha256"))
            elif cmd == "cleanup":
                out = _cxl_run(_cleanup, req.get("scope", "all"), bool(req.get("confirmed", False)))
            elif cmd == "cxl_import_status":
                _cxl_run(_cxl_import_advance)
                out = {"ok": True, "stage": CXL_IMPORT.stage, "error": CXL_IMPORT.error, "timings": CXL_IMPORT.timings,
                       "reservation_blocks": None if CXL_IMPORT.reservation is None else len(CXL_IMPORT.reservation.block_ids)}
            elif cmd == "hashes":
                out = ({"ok": True, "hashes": [h.hex() for h in STATE.candidate_hashes]}
                       if STATE.candidate_hashes else {"ok": False, "error": "no candidate"})
            elif cmd == "release":
                out = _release()
            else:
                out = {"ok": False, "error": f"unknown cmd {cmd}"}
        except Exception as exc:  # noqa: BLE001 - reported to the caller
            out = {"ok": False, "error": repr(exc)}
            _log("agent_error", error=repr(exc))
        f.write((json.dumps(out) + "\n").encode()); f.flush()


def _serve(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", port)); s.listen(8)
    _log("agent_listening", port=s.getsockname()[1])
    while True:
        conn, _ = s.accept()
        threading.Thread(target=_handle, args=(conn,), daemon=True).start()


_RECEIVER = None
if _PATH:
    _b.register_listener(AgentListener())
    _log("listener_registered", spec_module=__name__, pin_min_blocks=_PIN_MIN)
if _AGENT_PORT:
    threading.Thread(target=_serve, args=(_AGENT_PORT,), daemon=True).start()
if _PAYLOAD_PORT:
    _RECEIVER = PayloadReceiver(host="0.0.0.0", port=_PAYLOAD_PORT)
    _log("payload_receiver_listening", port=_RECEIVER.address[1])


class VampOffloadingSpec(_b.VampOffloadingSpec):
    def get_manager(self):
        m = super().get_manager()
        _log("manager_created", manager=type(m).__name__, num_blocks=int(self.num_blocks),
             offloaded_block_size=int(self.gpu_block_size[0] * self.block_size_factor), eviction_policy=self.eviction_policy)
        return m

    def get_handlers(self, kv_caches):
        yield from super().get_handlers(kv_caches)
        br = _b.current_bridge()
        _log("handlers_registered", bridge_num_blocks=None if br is None else br.num_blocks,
             bridge_block_bytes=None if br is None else br.block_bytes)
