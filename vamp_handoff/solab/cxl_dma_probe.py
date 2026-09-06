#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""GPU <-> CXL direct-path feasibility probe (D0-D2), independent of the engine.

Runs as ONE process per node (all provider calls on the main thread) and uses the
provider allocator for the CXL region, so nothing outside our slice is touched.

  D0  CUDA registration: a normal host buffer (control) and the CXL payload region,
      with cudaHostRegister flags Default / IoMemory / Mapped. A failure is recorded as
      "failed for this mapping+flag+driver combination", not as "direct DMA impossible".
  D1  writer: GPU -> CXL copy (cudaMemcpy D2H into the CXL pointer), device sync,
      clwb fence; local byte check. reader: refresh, CXL -> GPU copy (H2D), byte check
      against the digest published in the shared record.
  D2  writer on node A, reader on node B = A GPU -> shared CXL -> B GPU with cross-host
      visibility (lock + record + fence/refresh protocol as in G-E/G-F).
Copy time is measured separately from the checksum (sha256 is correctness only).
If registration fails the copy still runs (pageable path) and is labelled
"pageable (driver staging)" - that is NOT a direct DMA result.

usage (writer on s1 = node 1, reader on s2 = node 0, manager already running):
  CXL_SHM_LIBRARY=... python3 cxl_dma_probe.py --role writer --key DMA_T1 --size-mib 256
  CXL_SHM_LIBRARY=... python3 cxl_dma_probe.py --role reader --key DMA_T1
  CXL_SHM_LIBRARY=... python3 cxl_dma_probe.py --role cleanup --key DMA_T1   (writer node)
"""
import argparse
import ctypes
import hashlib
import json
import os
import struct
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

P = argparse.ArgumentParser()
P.add_argument("--role", required=True, choices=["writer", "reader", "cleanup", "register-only"])
P.add_argument("--key", default="DMA_T1")
P.add_argument("--size-mib", type=int, default=256)
P.add_argument("--flags", default="0,4,2", help="cudaHostRegister flags to try in order: 0=Default 1=Portable 2=Mapped 4=IoMemory 8=ReadOnly")
P.add_argument("--seed", type=int, default=7)
args = P.parse_args()

REC = struct.Struct("<QQ32sQ")     # size, generation, sha256, flags_used_by_writer
T0 = time.monotonic()


def out(event, **kw):
    kw["event"] = event; kw["t"] = round(time.monotonic() - T0, 3); print(json.dumps(kw), flush=True)


# --- CUDA runtime via the libcudart torch already loaded ---------------------------
import torch  # noqa: E402
torch.cuda.init()
_cudart_path = None
with open("/proc/self/maps") as fh:
    for line in fh:
        if "libcudart" in line:
            _cudart_path = line.split()[-1]; break
if _cudart_path is None:
    out("abort", reason="libcudart not found in process maps"); sys.exit(2)
cudart = ctypes.CDLL(_cudart_path)
cudart.cudaHostRegister.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint]
cudart.cudaHostUnregister.argtypes = [ctypes.c_void_p]
cudart.cudaMemcpy.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
cudart.cudaDeviceSynchronize.argtypes = []
cudart.cudaGetErrorString.restype = ctypes.c_char_p
cudart.cudaGetLastError.argtypes = []
H2D, D2H = 1, 2


def cuda_err(rc):
    return cudart.cudaGetErrorString(rc).decode()


def try_register(ptr, nbytes, label):
    """D0: try each flag; return the flag that registered or None."""
    for f in [int(x) for x in args.flags.split(",")]:
        t = time.monotonic(); rc = cudart.cudaHostRegister(ctypes.c_void_p(ptr), nbytes, f)
        cudart.cudaGetLastError()  # clear sticky error state for the next attempt
        out("D0_register", target=label, flag=f, rc=rc, err=cuda_err(rc) if rc else "ok", seconds=round(time.monotonic() - t, 4),
            driver=torch.version.cuda, gpu=torch.cuda.get_device_name(0))
        if rc == 0:
            return f
    return None


def timed_memcpy(dst, src, nbytes, kind):
    t = time.monotonic(); rc = cudart.cudaMemcpy(ctypes.c_void_p(dst), ctypes.c_void_p(src), nbytes, kind)
    rc2 = cudart.cudaDeviceSynchronize(); s = time.monotonic() - t
    return rc or rc2, round(s, 4), round(nbytes / s / 1e9, 2)


def sha256_ptr(ptr, nbytes):
    t = time.monotonic(); d = hashlib.sha256(memoryview((ctypes.c_char * nbytes).from_address(ptr)).cast("B")).hexdigest()
    return d, round(time.monotonic() - t, 3)


# --- provider (single thread) --------------------------------------------------------
from vamp_cxl.cxl_shm_binding import CtypesProviderApi  # noqa: E402
from cxl_abi_solab import solab_confirmation  # noqa: E402
lib = os.environ["CXL_SHM_LIBRARY"]
api = CtypesProviderApi(solab_confirmation(lib), library_path=lib)
api.connect(); out("provider_connected", lib=lib)
nbytes = args.size_mib << 20

# D0 control: an ordinary page-aligned host buffer must register
ctl = torch.empty(64 << 20, dtype=torch.uint8)          # 64 MiB pageable host tensor
ctl_flag = try_register(ctl.data_ptr(), ctl.numel(), "host_control_64MiB")
if ctl_flag is not None:
    cudart.cudaHostUnregister(ctypes.c_void_p(ctl.data_ptr()))

if args.role == "register-only":
    p = api.payload_alloc(nbytes)
    f = try_register(p, nbytes, f"cxl_payload_{args.size_mib}MiB")
    if f is not None:
        cudart.cudaHostUnregister(ctypes.c_void_p(p))
    api.payload_free(p, nbytes)
    out("done", registered=f is not None, flag=f); sys.exit(0)

if args.role == "writer":
    p = api.payload_alloc(nbytes); poff = api.get_offset(p)
    out("payload_alloc", offset=poff, nbytes=nbytes)
    flag = try_register(p, nbytes, f"cxl_payload_{args.size_mib}MiB")
    path = "registered_pinned" if flag is not None else "pageable_driver_staging"
    g = torch.Generator(device="cuda").manual_seed(args.seed)
    gpu = torch.randint(0, 256, (nbytes,), dtype=torch.uint8, device="cuda", generator=g)
    torch.cuda.synchronize()
    expected = hashlib.sha256(gpu.cpu().numpy().tobytes()).hexdigest()   # correctness reference, not timed
    # D1: GPU -> CXL
    rc, s, gbps = timed_memcpy(p, gpu.data_ptr(), nbytes, D2H)
    out("D1_gpu_to_cxl", path=path, rc=rc, err=cuda_err(rc) if rc else "ok", seconds=s, GBps=gbps)
    t = time.monotonic(); api.fence(p, nbytes); out("fence", seconds=round(time.monotonic() - t, 4))
    local, ds = sha256_ptr(p, nbytes); out("local_check", match=local == expected, sha256_s=ds)
    # publish the record under a lock (same protocol as the KV record)
    r = api.shmalloc(REC.size); lock = api.lock_alloc(); api.lock_acquire(lock)
    api.write(r, REC.pack(nbytes, 1, bytes.fromhex(expected), 0xFFFFFFFF if flag is None else flag)); api.fence(r, REC.size)
    api.lock_release(lock); api.put(args.key, r)
    o = api.shmalloc(8); api.write(o, struct.pack("<Q", poff)); api.fence(o, 8); api.put(args.key + "_OFF", o)
    out("published", key=args.key, payload_off=poff, record_off=api.get_offset(r), lock=lock, sha256=expected)
    if flag is not None:
        cudart.cudaHostUnregister(ctypes.c_void_p(p))
    out("done", role="writer", path=path, note="payload/record kept for the reader; run --role cleanup afterwards")
    sys.exit(0)

if args.role == "reader":
    r = api.get(args.key)
    if r is None:
        out("abort", reason=f"key {args.key} not found"); sys.exit(3)
    api.refresh(r, REC.size); size, gen, digest, wflag = REC.unpack(api.read(r, REC.size))
    out("record", size=size, generation=gen, writer_flag=wflag, sha256=digest.hex())
    # the payload offset is published under a second key (<key>_OFF)
    poff_rec = api.get(args.key + "_OFF")
    if poff_rec is None:
        out("abort", reason="offset record missing"); sys.exit(3)
    api.refresh(poff_rec, 8); (poff,) = struct.unpack("<Q", api.read(poff_rec, 8))
    p = api.get_ptr(poff)
    t = time.monotonic(); api.refresh(p, size); out("refresh", seconds=round(time.monotonic() - t, 4))
    flag = try_register(p, size, f"cxl_payload_{size >> 20}MiB")
    path = "registered_pinned" if flag is not None else "pageable_driver_staging"
    gpu = torch.empty(size, dtype=torch.uint8, device="cuda")
    rc, s, gbps = timed_memcpy(gpu.data_ptr(), p, size, H2D)
    out("D1_cxl_to_gpu", path=path, rc=rc, err=cuda_err(rc) if rc else "ok", seconds=s, GBps=gbps)
    got = hashlib.sha256(gpu.cpu().numpy().tobytes()).hexdigest()
    cpu_view, ds = sha256_ptr(p, size)
    out("D2_check", gpu_bytes_match=got == digest.hex(), cpu_view_match=cpu_view == digest.hex(), sha256_s=ds)
    if flag is not None:
        cudart.cudaHostUnregister(ctypes.c_void_p(p))
    out("done", role="reader", path=path)
    sys.exit(0 if got == digest.hex() else 4)

if args.role == "cleanup":
    poff_rec = api.get(args.key + "_OFF"); r = api.get(args.key)
    if poff_rec is not None:
        api.refresh(poff_rec, 8); (poff,) = struct.unpack("<Q", api.read(poff_rec, 8))
        api.destroy(args.key + "_OFF")
    if r is not None:
        api.refresh(r, REC.size); size, _, _, _ = REC.unpack(api.read(r, REC.size)); api.destroy(args.key)
        if poff_rec is not None:
            api.payload_free(api.get_ptr(poff), size)
    out("done", role="cleanup", freed=poff_rec is not None and r is not None)
