#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""Python (ctypes) twin of solab/cxl_ping.c: validates our CtypesProviderApi binding
against the live provider library and manager.

  writer (s2, nid 0): cxl_pyping.py write <nbytes> <seed> [--key K]
  reader (s1, nid 1): cxl_pyping.py read  <nbytes> <seed> [--key K] [--no-refresh]

Record layout matches cxl_ping.c so the C writer / Python reader (and vice versa)
interoperate. --no-refresh is the negative test: read without invalidating local
cache lines (expected to be able to observe stale data on a non-coherent path).
"""
import argparse
import hashlib
import os
import struct
import sys
import time
from pathlib import Path

HANDOFF = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HANDOFF)); sys.path.insert(0, str(HANDOFF / "solab"))
from vamp_cxl.cxl_shm_binding import CtypesProviderApi  # noqa: E402
from cxl_abi_solab import solab_confirmation  # noqa: E402

REC = struct.Struct("<8Q")  # magic, generation, nbytes, seed, checksum, lockptr, payload_off, state
MAGIC = 0x56414D5031


def fnv1a(b: bytes) -> int:
    h = 1469598103934665603
    for x in b:
        h = ((h ^ x) * 1099511628211) & 0xFFFFFFFFFFFFFFFF
    return h


def pattern(n: int, seed: int) -> bytes:
    out = bytearray(n); x = seed
    for i in range(n):
        x = (x * 6364136223846793005 + 1442695040888963407) & 0xFFFFFFFFFFFFFFFF
        out[i] = x >> 56
    return bytes(out)


def main() -> int:
    P = argparse.ArgumentParser()
    P.add_argument("mode", choices=["write", "read", "rewrite"])
    P.add_argument("nbytes", type=int)
    P.add_argument("seed", type=int)
    P.add_argument("--key", default="VAMP_PYPING")
    P.add_argument("--no-refresh", action="store_true")
    P.add_argument("--library", default=os.environ.get("CXL_SHM_LIBRARY"))
    a = P.parse_args()
    t0 = time.time()
    api = CtypesProviderApi(solab_confirmation(a.library), library_path=a.library)
    api.connect()
    print(f"connect_s={time.time()-t0:.3f} lib={a.library}")
    if a.mode == "write":
        data = pattern(a.nbytes, a.seed); ck = fnv1a(data)
        rec_ptr = api.shmalloc(REC.size)
        payload = api.payload_alloc(a.nbytes)
        lock = api.lock_alloc()
        t1 = time.time(); api.write(payload, data); t_w = time.time() - t1
        t1 = time.time(); api.fence(payload, a.nbytes); t_f = time.time() - t1
        api.lock_acquire(lock)
        api.write(rec_ptr, REC.pack(MAGIC, 1, a.nbytes, a.seed, ck, lock, api.get_offset(payload), 1))
        api.refresh(rec_ptr, REC.size)  # clflush+mfence pushes the record out too
        api.lock_release(lock)
        api.put(a.key, rec_ptr)
        print(f"WRITE_OK nbytes={a.nbytes} checksum={ck:016x} rec_off={api.get_offset(rec_ptr)} "
              f"payload_off={api.get_offset(payload)} lockptr={lock} write_s={t_w:.3f} fence_s={t_f:.4f}")
        return 0
    rec_ptr = None
    for _ in range(600):
        rec_ptr = api.get(a.key)
        if rec_ptr: break
        time.sleep(0.1)
    if not rec_ptr:
        print("key not found"); return 1
    if a.mode == "rewrite":
        # overwrite the SAME payload region with a new pattern and bump the record
        # generation (negative test: a reader that skips refresh may see the old bytes)
        api.refresh(rec_ptr, REC.size)
        magic, gen, nb, seed, ck, lockptr, poff, state = REC.unpack(api.read(rec_ptr, REC.size))
        if magic != MAGIC or nb != a.nbytes:
            print(f"REWRITE_BAD magic={magic:x} nbytes={nb}"); return 1
        data = pattern(a.nbytes, a.seed); newck = fnv1a(data)
        payload = api.get_ptr(poff)
        api.lock_acquire(lockptr)
        api.write(payload, data); api.fence(payload, a.nbytes)
        api.write(rec_ptr, REC.pack(MAGIC, gen + 1, nb, a.seed, newck, lockptr, poff, 1))
        api.refresh(rec_ptr, REC.size)
        api.lock_release(lockptr)
        print(f"REWRITE_OK gen={gen+1} checksum={newck:016x} payload_off={poff}")
        return 0
    api.refresh(rec_ptr, REC.size)
    magic, gen, nb, seed, ck, lockptr, poff, state = REC.unpack(api.read(rec_ptr, REC.size))
    t1 = time.time(); api.lock_acquire(lockptr); t_lock = time.time() - t1
    api.refresh(rec_ptr, REC.size)
    magic, gen, nb, seed, ck, lockptr, poff, state = REC.unpack(api.read(rec_ptr, REC.size))
    api.lock_release(lockptr)
    if magic != MAGIC or state != 1 or nb != a.nbytes:
        print(f"READ_BAD magic={magic:x} state={state} nbytes={nb}"); return 1
    payload = api.get_ptr(poff)
    t1 = time.time()
    if not a.no_refresh:
        api.refresh(payload, nb)
    t_r = time.time() - t1
    t1 = time.time(); mine = fnv1a(api.read(payload, nb)); t_ck = time.time() - t1
    ok = mine == ck
    print(f"{'READ_OK' if ok else 'READ_MISMATCH'} nbytes={nb} gen={gen} remote={ck:016x} local={mine:016x} "
          f"lock_wait_s={t_lock:.4f} refresh_s={t_r:.4f} read+checksum_s={t_ck:.3f} seed_ok={seed==a.seed} refresh={'off' if a.no_refresh else 'on'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
