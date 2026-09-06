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
import json
import os
import socket
import threading
import time
from concurrent.futures import Future

from vamp_cxl import vllm_binding as _b
from vamp_cxl.keys import JobId
from vamp_cxl.network_transport import NetworkJob, PayloadReceiver, TcpPayloadTransport, sha256_hex

_PATH = os.environ.get("VAMP_PROBE_FILE")
_LOCK = threading.Lock()
_PIN_MIN = int(os.environ.get("VAMP_PIN_MIN_BLOCKS", "64"))
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
        if (STATE.candidate is None and mgr is not None and len(block_hashes) >= _PIN_MIN):
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
    }


def _export(host, port):
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
                out = _export(req["host"], req["port"])
            elif cmd == "import_prepare":
                out = _import_prepare(req["hashes"])
            elif cmd == "import_status":
                _advance_import(); out = {"ok": True, "stage": STATE.import_stage, "error": STATE.import_error,
                                           "reservation_blocks": None if STATE.import_reservation is None else len(STATE.import_reservation.block_ids)}
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
