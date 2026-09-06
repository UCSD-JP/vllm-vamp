# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""Heavy-48 checkpoint adapter over the existing, tested staged transports.

Record completed prompt identities without pins during pressure. At a quiescent
checkpoint select exact CPU-READY hashes; transfer only blocks absent at B.
No automatic routing or concurrent migration. Provider code remains external.
"""

import hashlib
import json
import os
import queue
import socketserver
import threading
from array import array
from collections import OrderedDict
from concurrent.futures import Future

import vamp_agent as legacy
from vamp_cxl import vllm_binding as binding

from vllm.distributed.kv_transfer.kv_connector.v1.offloading_connector import (
    OffloadingConnector,
)
from vllm.v1.core.kv_cache_manager import KVCacheManager
from vllm.v1.core.kv_cache_utils import BlockHash, get_block_hash
from vllm.v1.core.sched.scheduler import Scheduler

REGISTRY = OrderedDict()
MAILBOX = queue.Queue()
JOBS = {}
_next_job = 0
_started = False
_owner = None
_schedule = Scheduler.schedule
_free = Scheduler._free_blocks
_computed = KVCacheManager.get_computed_blocks
_external = OffloadingConnector.get_num_new_matched_tokens


def fingerprint(request):
    return hashlib.sha256(array("I", request.prompt_token_ids).tobytes()).hexdigest()


def free_blocks(self, request):
    n = max(0, len(request.prompt_token_ids) // self.block_size - 2)
    groups = self.kv_cache_manager.get_blocks(request.request_id).blocks
    blocks = list(groups[0][:n]) if len(groups) == 1 else []
    if (
        n
        and len(blocks) == n
        and all(b.block_hash is not None and not b.is_null for b in blocks)
    ):
        key = fingerprint(request)
        REGISTRY[key] = dict(
            hashes=[bytes(get_block_hash(b.block_hash)).hex() for b in blocks],
            prompt_tokens=len(request.prompt_token_ids),
            block_tokens=self.block_size,
            request_id=request.request_id,
        )
        REGISTRY.move_to_end(key)
        if len(REGISTRY) > 512:
            REGISTRY.popitem(last=False)
        legacy._log(
            "heavy_completed",
            fingerprint=key,
            prompt_tokens=len(request.prompt_token_ids),
            n_blocks=n,
            request_id=request.request_id,
        )
    return _free(self, request)


def command(cmd):
    mgr = binding.current_manager()
    if cmd["cmd"] == "metadata":
        return dict(ok=True, **REGISTRY[cmd["fingerprint"]])
    hashes = [BlockHash(bytes.fromhex(h)) for h in cmd["hashes"]]
    if cmd["cmd"] == "missing":
        missing = [bytes(h).hex() for h in hashes if mgr.ready_run([h]) == 0]
        return dict(
            ok=True, missing=missing, already_cpu_ready=len(hashes) - len(missing)
        )
    if cmd["cmd"] == "select":
        if legacy.STATE.candidate is not None:
            raise RuntimeError("previous export lease still held")
        if not hashes:
            return dict(ok=True, selected=0)
        known = set(REGISTRY[cmd["fingerprint"]]["hashes"])
        if not all(bytes(h).hex() in known for h in hashes):
            raise ValueError("selection not from the completed source prompt")
        lease = mgr.acquire_export_lease(hashes)
        if lease is None:
            raise RuntimeError(
                "source CPU prefix not fully READY; no hidden forced recompute"
            )
        legacy.STATE.candidate = lease
        legacy.STATE.candidate_hashes = list(hashes)
        legacy._log(
            "heavy_selected", n_blocks=len(hashes), fingerprint=cmd["fingerprint"]
        )
        return dict(
            ok=True,
            selected=len(hashes),
            nbytes=len(hashes) * binding.current_bridge().block_bytes,
        )
    raise ValueError("unknown checkpoint command")


def schedule(self, *args, **kwargs):
    global _owner
    if _owner is None:
        _owner = threading.get_ident()
    if _owner != threading.get_ident():
        raise RuntimeError("heavy scheduler owner changed")
    while not MAILBOX.empty():
        cmd, future = MAILBOX.get_nowait()
        try:
            if self.running:
                raise RuntimeError("checkpoint requires idle workers")
            result = command(cmd)
        except Exception as exc:
            result = dict(ok=False, error=repr(exc))
        future.set_result(result)
    return _schedule(self, *args, **kwargs)


def computed(self, request):
    result = _computed(self, request)
    legacy._log(
        "heavy_gpu_lookup",
        fingerprint=fingerprint(request),
        request_id=request.request_id,
        prompt_tokens=len(request.prompt_token_ids),
        gpu_hit_tokens=result[1],
    )
    return result


def external(self, request, num_computed_tokens):
    result = _external(self, request, num_computed_tokens)
    legacy._log(
        "heavy_cpu_lookup",
        fingerprint=fingerprint(request),
        request_id=request.request_id,
        prompt_tokens=len(request.prompt_token_ids),
        cpu_hit_tokens=result[0],
        gpu_computed_tokens=num_computed_tokens,
    )
    return result


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        global _next_job
        try:
            cmd = json.loads(self.rfile.readline(2 << 20))
            if cmd["cmd"] == "status":
                result = dict(
                    ok=True,
                    completed=len(REGISTRY),
                    queue=MAILBOX.qsize(),
                    owner=_owner,
                    bridge_ready=binding.current_bridge() is not None,
                )
            elif cmd["cmd"] == "job":
                f = JOBS[cmd["id"]]
                result = dict(
                    ok=True, done=f.done(), result=f.result() if f.done() else None
                )
            else:
                _next_job += 1
                f = Future()
                JOBS[_next_job] = f
                MAILBOX.put((cmd, f))
                result = dict(ok=True, id=_next_job)
        except Exception as exc:
            result = dict(ok=False, error=repr(exc))
        self.wfile.write(json.dumps(result).encode() + b"\n")


class Server(socketserver.TCPServer):
    allow_reuse_address = True


Scheduler.schedule = schedule
Scheduler._free_blocks = free_blocks
KVCacheManager.get_computed_blocks = computed
OffloadingConnector.get_num_new_matched_tokens = external


class VampOffloadingSpec(legacy.VampOffloadingSpec):
    def get_handlers(self, kv_caches):
        global _started
        yield from super().get_handlers(kv_caches)
        if not _started:
            if self.block_size_factor != 1 or len(self.gpu_block_size) != 1:
                raise RuntimeError(
                    "Heavy checkpoint requires equal single-group GPU/CPU blocks"
                )
            if os.environ.get("VAMP_AGENT_PIN") != "0":
                raise RuntimeError(
                    "disable automatic pinning during Heavy pressure replay"
                )
            port = 7201 if os.environ["VAMP_AGENT_PORT"] == "7001" else 7202
            server = Server(("0.0.0.0", port), Handler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            _started = True
            legacy._log(
                "heavy_adapter_ready", port=port, block_tokens=self.gpu_block_size[0]
            )
