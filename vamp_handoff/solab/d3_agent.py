# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""D3 fixed-topology feasibility adapter, loaded only via spec_module_path.

One uniproc TP=1 worker, one full-attention KV group, one transfer. Scheduler
hooks serialize all GPU pool/provider operations; RPC threads only enqueue.
Imported GPU hashes are local-only: no Dynamo KV event/discovery claim.
Existing CPU offload remains enabled but is never used for the D3 payload.
"""

import json
import os
import queue
import socketserver
import struct
import threading
import time
import traceback
from concurrent.futures import Future

from d3_blocks import GpuReservation
from vamp_cxl.vllm_binding import VampOffloadingSpec as BaseSpec
from vllm.distributed.kv_transfer.kv_connector.v1.offloading_connector import (
    OffloadingConnector,
)
from vllm.v1.core.kv_cache_manager import KVCacheManager
from vllm.v1.core.kv_cache_utils import (
    BlockHash,
    get_block_hash,
    make_block_hash_with_group_id,
)
from vllm.v1.core.sched.scheduler import Scheduler


class State:
    def __init__(self):
        self.owner = None
        self.scheduler = None
        self.bridge = None
        self.block_tokens = None
        self.candidate = []
        self.hashes = []
        self.imported = None
        self.exported = None
        self.api = None
        self.jobs = {}
        self.mailbox = queue.Queue()
        self.records = []
        self.next_job = 0

    def log(self, event, **fields):
        row = dict(event=event, ts=time.time(), mono=time.monotonic(), **fields)
        self.records.append(row)
        path = os.environ.get("VAMP_PROBE_FILE")
        if path:
            with open(path, "a") as f:
                f.write(json.dumps(row) + "\n")

    def bind(self, scheduler):
        if self.owner is None:
            self.owner = threading.get_ident()
            self.scheduler = scheduler
            if len(scheduler.kv_cache_manager.kv_cache_config.kv_cache_groups) != 1:
                raise RuntimeError("D3 only supports one KV group")
            if self.pool.hash_block_size != self.block_tokens:
                raise RuntimeError("D3 GPU and hash block sizes must match")
        if self.owner != threading.get_ident() or self.scheduler is not scheduler:
            raise RuntimeError("D3 scheduler ownership mismatch")

    @property
    def pool(self):
        return self.scheduler.kv_cache_manager.block_pool

    def provider(self):
        if self.owner != threading.get_ident():
            raise RuntimeError("provider must run on scheduler owner thread")
        if self.api is None:
            from cxl_abi_solab import solab_confirmation
            from vamp_cxl.cxl_shm_binding import CtypesProviderApi

            lib = os.environ["CXL_SHM_LIBRARY"]
            self.api = CtypesProviderApi(solab_confirmation(lib), library_path=lib)
            self.api.connect()
        return self.api

    def snapshot(self):
        return dict(
            ok=True,
            candidate_blocks=len(self.candidate),
            imported_blocks=0
            if self.imported is None or self.imported.released
            else len(self.imported.blocks),
            exported_key=None if self.exported is None else self.exported["key"],
            bridge_ready=self.bridge is not None,
            block_tokens=self.block_tokens,
            block_bytes=None if self.bridge is None else self.bridge.block_bytes,
            gpu_pages=None if self.bridge is None else self.bridge.pages,
            staging_bytes=None if self.bridge is None else self.bridge.capacity,
            pool_blocks=None if self.scheduler is None else len(self.pool.blocks),
            queue_size=self.mailbox.qsize(),
            records=list(self.records[-80:]),
        )

    def capture(self, scheduler, request):
        self.bind(scheduler)
        if os.environ.get("VAMP_AGENT_PIN", "0") != "1" or self.candidate:
            return
        n = len(request.prompt_token_ids) // self.block_tokens - 2
        if n < 64:
            return
        groups = scheduler.kv_cache_manager.get_blocks(request.request_id).blocks
        if len(groups) != 1:
            raise RuntimeError("D3 requires one cache group")
        blocks = list(groups[0][:n])
        if len(blocks) != n or any(b.is_null or b.block_hash is None for b in blocks):
            raise RuntimeError("D3 source prefix incomplete/not cached")
        self.pool.touch(blocks)
        self.candidate = blocks
        self.hashes = [bytes(get_block_hash(b.block_hash)) for b in blocks]
        self.log(
            "gpu_source_pinned",
            n_blocks=n,
            prompt_tokens=len(request.prompt_token_ids),
            prefix_tokens=n * self.block_tokens,
            request_id=request.request_id,
        )

    def export(self, cmd):
        if not self.candidate or self.exported:
            raise RuntimeError("missing GPU source or previous publication still held")
        api = self.provider()
        key = cmd["key"]
        if not key.startswith("D3_") or api.get(key) is not None:
            raise ValueError("D3 key must be new and start with D3_")
        size = len(self.candidate) * self.bridge.block_bytes
        p = api.payload_alloc(size)
        off = api.get_offset(p)
        if off < 0 or off + size > 64 << 30:
            api.payload_free(p, size)
            raise ValueError("payload outside UCSD-relative [0,64 GiB)")
        lock = api.lock_alloc()
        self.exported = dict(
            key=key, ptr=p, size=size, lock=lock, record=None, published=False
        )
        api.lock_acquire(lock)
        try:
            result = self.bridge.transfer(
                [b.block_id for b in self.candidate], p, True, api
            )
            rec = dict(
                magic="VAMP_D3_1",
                state="READY",
                generation=1,
                payload_off=off,
                nbytes=size,
                lock=lock,
                pages=self.bridge.pages,
                block_tokens=self.block_tokens,
                chunk_bytes=self.bridge.capacity,
                hashes=[h.hex() for h in self.hashes],
                sha256=result["sha256"],
            )
            body = json.dumps(rec).encode()
            raw = struct.pack("<Q", len(body)) + body
            rp = api.shmalloc(len(raw))
            self.exported["record"] = rp
            api.write(rp, raw)
            api.fence(rp, len(raw))
        finally:
            api.lock_release(lock)
        api.put(key, rp)
        self.exported["published"] = True
        self.log("gpu_export_ready", key=key, **result)
        return dict(ok=True, key=key, n_blocks=len(self.candidate), **result)

    def import_kv(self, cmd):
        if self.imported is not None:
            raise RuntimeError("previous GPU reservation still held")
        api = self.provider()
        rp = api.get(cmd["key"])
        if not rp:
            raise KeyError(cmd["key"])
        api.refresh(rp, 8)
        (length,) = struct.unpack("<Q", api.read(rp, 8))
        if not 0 < length < 2 << 20:
            raise ValueError("invalid D3 record size")
        api.refresh(rp + 8, length)
        rec = json.loads(api.read(rp + 8, length))
        api.lock_acquire(rec["lock"])
        try:
            if (
                rec["magic"] != "VAMP_D3_1"
                or rec["state"] != "READY"
                or rec["pages"] != self.bridge.pages
                or rec["block_tokens"] != self.block_tokens
                or rec["chunk_bytes"] != self.bridge.capacity
            ):
                raise ValueError("D3 layout/state mismatch")
            hashes = [BlockHash(bytes.fromhex(h)) for h in rec["hashes"]]
            size = len(hashes) * self.bridge.block_bytes
            if (
                size != rec["nbytes"]
                or not 0 <= rec["payload_off"] <= (64 << 30) - size
            ):
                raise ValueError("D3 payload bounds/size mismatch")
            res = GpuReservation(self.pool, hashes, make_block_hash_with_group_id)
            self.imported = res
            try:
                result = self.bridge.transfer(
                    [b.block_id for b in res.blocks],
                    api.get_ptr(rec["payload_off"]),
                    False,
                    api,
                )
                expected = (
                    "0" * 64 if cmd.get("inject") == "checksum" else rec["sha256"]
                )
                match = result["sha256"] == expected
                res.commit(copy_complete=True, digest_match=match)
            except Exception:
                res.abort()
                self.imported = None
                self.log(
                    "gpu_import_aborted",
                    n_blocks=len(hashes),
                    cached_after=sum(
                        bool(self.pool.get_cached_block(h, [0])) for h in hashes
                    ),
                )
                raise
        finally:
            api.lock_release(rec["lock"])
        self.log(
            "gpu_import_committed",
            n_blocks=len(hashes),
            prefix_tokens=len(hashes) * self.block_tokens,
            gpu_cached_after=sum(
                bool(self.pool.get_cached_block(h, [0])) for h in hashes
            ),
            **result,
        )
        return dict(ok=True, n_blocks=len(hashes), digest_match=match, **result)

    def cleanup(self, cmd):
        if self.imported is not None:
            self.imported.release()
            self.imported = None
        if self.exported is not None:
            if not cmd.get("destination_done"):
                raise RuntimeError(
                    "A cleanup requires destination_done acknowledgement"
                )
            api = self.provider()
            ex = self.exported
            # Do not release payload or GPU pins if publication removal fails.
            if ex["published"]:
                api.destroy(ex["key"])
                ex["published"] = False
                ex["record"] = None
            if ex["record"] is not None:
                api.shfree(ex["record"])
                ex["record"] = None
            if ex["lock"] is not None:
                api.lock_free(ex["lock"])
                ex["lock"] = None
            api.payload_free(ex["ptr"], ex["size"])
            self.exported = None
        if self.candidate:
            self.pool.free_blocks(self.candidate)
            self.candidate = []
            self.hashes = []
        self.log("gpu_cleanup", ok=True)
        return self.snapshot()

    def drain(self, scheduler):
        self.bind(scheduler)
        while not self.mailbox.empty():
            cmd, future = self.mailbox.get_nowait()
            t0 = time.monotonic()
            try:
                # The nudge is waiting; no real request may be running during D3.
                if scheduler.running:
                    raise RuntimeError("D3 operations require idle worker")
                result = {
                    "export": self.export,
                    "import": self.import_kv,
                    "cleanup": self.cleanup,
                }[cmd["cmd"]](cmd)
                result["operation_s"] = time.monotonic() - t0
                future.set_result(result)
            except Exception as exc:
                self.log(
                    "operation_failed",
                    command=cmd["cmd"],
                    error=repr(exc),
                    detail=traceback.format_exc(),
                )
                future.set_result(
                    dict(ok=False, error=repr(exc), operation_s=time.monotonic() - t0)
                )


S = State()
_schedule = Scheduler.schedule
_free = Scheduler._free_blocks
_computed = KVCacheManager.get_computed_blocks
_external = OffloadingConnector.get_num_new_matched_tokens


def schedule(self, *args, **kwargs):
    S.drain(self)
    return _schedule(self, *args, **kwargs)


def free_blocks(self, request):
    S.capture(self, request)
    return _free(self, request)


def computed(self, request):
    result = _computed(self, request)
    S.log(
        "gpu_prefix_lookup",
        request_id=request.request_id,
        prompt_tokens=len(request.prompt_token_ids),
        gpu_hit_tokens=result[1],
    )
    return result


def external(self, request, num_computed_tokens):
    result = _external(self, request, num_computed_tokens)
    S.log(
        "cpu_prefix_lookup",
        request_id=request.request_id,
        prompt_tokens=len(request.prompt_token_ids),
        gpu_computed_tokens=num_computed_tokens,
        cpu_external_tokens=result[0],
    )
    return result


Scheduler.schedule = schedule
Scheduler._free_blocks = free_blocks
KVCacheManager.get_computed_blocks = computed
OffloadingConnector.get_num_new_matched_tokens = external


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        try:
            cmd = json.loads(self.rfile.readline(1 << 20))
            if cmd["cmd"] == "status":
                result = S.snapshot()
            elif cmd["cmd"] == "job":
                f = S.jobs[cmd["id"]]
                result = dict(
                    ok=True, done=f.done(), result=f.result() if f.done() else None
                )
            elif cmd["cmd"] in ("export", "import", "cleanup"):
                S.next_job += 1
                f = Future()
                S.jobs[S.next_job] = f
                S.mailbox.put((cmd, f))
                result = dict(ok=True, id=S.next_job)
            else:
                raise ValueError("unknown D3 command")
        except Exception as exc:
            result = dict(ok=False, error=repr(exc))
        self.wfile.write(json.dumps(result).encode() + b"\n")


class Server(socketserver.TCPServer):
    allow_reuse_address = True


class VampOffloadingSpec(BaseSpec):
    def get_handlers(self, kv_caches):
        yield from super().get_handlers(kv_caches)
        if S.bridge is None:
            from d3_gpu import GpuBridge

            S.block_tokens = self.gpu_block_size[0]
            S.bridge = GpuBridge(self._handlers.gpu_to_cpu_handler.src_tensors)
            server = Server(("0.0.0.0", int(os.environ["VAMP_AGENT_PORT"])), Handler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            S.log(
                "d3_ready",
                pages=S.bridge.pages,
                block_tokens=S.block_tokens,
                staging_bytes=S.bridge.capacity,
                scope="single-group/TP1/uniproc/idle-only",
            )
