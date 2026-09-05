# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""G-A/G-B probe: file-logging ReadyListener + VampOffloadingSpec with creation logging.

Loaded inside the vLLM EngineCore process via spec_module_path; the listener is
registered at import time, before the factory builds the spec. Writes JSONL to
$VAMP_PROBE_FILE. No vLLM/vamp_cxl source is modified.
"""
import json
import os
import threading
import time

from vamp_cxl import vllm_binding as _b

_PATH = os.environ.get("VAMP_PROBE_FILE")
_LOCK = threading.Lock()


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


class FileListener:
    def __init__(self):
        self.ready_calls = 0
        self.ready_blocks = 0
        self.evict_calls = 0
        self.evict_blocks = 0

    def on_blocks_ready(self, block_hashes):
        self.ready_calls += 1
        self.ready_blocks += len(block_hashes)
        _log("ready", n_blocks=len(block_hashes),
             ready_calls=self.ready_calls, ready_blocks=self.ready_blocks)

    def on_blocks_evicted(self, block_hashes):
        self.evict_calls += 1
        self.evict_blocks += len(block_hashes)
        _log("evicted", n_blocks=len(block_hashes),
             evict_calls=self.evict_calls, evict_blocks=self.evict_blocks)


if _PATH:
    _b.register_listener(FileListener())
    _log("listener_registered", spec_module=__name__)


class VampOffloadingSpec(_b.VampOffloadingSpec):
    def get_manager(self):
        m = super().get_manager()
        _log("manager_created", manager=type(m).__name__,
             num_blocks=int(self.num_blocks),
             offloaded_block_size=int(self.gpu_block_size[0] * self.block_size_factor),
             eviction_policy=self.eviction_policy)
        return m

    def get_handlers(self, kv_caches):
        yield from super().get_handlers(kv_caches)
        br = _b.current_bridge()
        _log("handlers_registered",
             bridge_num_blocks=None if br is None else br.num_blocks,
             bridge_block_bytes=None if br is None else br.block_bytes,
             bridge_tensors=None if br is None else len(br.cpu_tensors))
