# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""Operator ABI confirmation for the solab provider library (2026-09-06).

Every signature below was read from ~/work/include/cxl_shm/{api.h,cacheline.h}
and cross-checked against the exported symbol table (nm -D) of
libcxl_shm_ucsd_N0.so / _N1.so, and exercised end-to-end by solab/cxl_ping.c
(C, provider toolchain) before being confirmed here for ctypes. Lock handles are
`struct { volatile uint64_t lockptr; }` passed by value (one integer register).

The node id is compiled into the library variant (N0 -> nid 0, N1 -> nid 1); each
host must load its own variant: CXL_SHM_LIBRARY=$HOME/vamp/cxl/lib/libcxl_shm.so
where that symlink points at N0 on solab-s2 and N1 on solab-s1.
"""
import os
import sys
from pathlib import Path

HANDOFF = Path(__file__).resolve().parents[1]
if str(HANDOFF) not in sys.path:
    sys.path.insert(0, str(HANDOFF))

from vamp_cxl.cxl_shm_binding import PROPOSED_SIGNATURES, AbiConfirmation  # noqa: E402

LIBRARY_SHA256 = {
    # `sha256sum ~/work/lib/libcxl_shm_ucsd_N?.so` on solab-s2, 2026-09-06
    "libcxl_shm_ucsd_N0.so": "4d035da110d6e527a14beebb190e8865e53ea5b898d88e8a6f910dd0e7c9daac",
    "libcxl_shm_ucsd_N1.so": "b86d60810bcd41e08f23a0109631991e2e943c6fca4e44c30b648431564c6859",
}

CONFIRMED_NAMES = (
    "cxl_shm_connect", "cxl_shm_finalize", "cxl_shm_is_initialized",
    "shmalloc", "shfree", "shm_payload_alloc", "shm_payload_free",
    "cxl_shm_put", "cxl_shm_get", "cxl_shm_destroy",
    "cxl_shm_get_offset", "cxl_shm_get_ptr",
    "cxl_shm_allocate_lock", "cxl_shm_free_lock", "cxl_shm_lock_acquire", "cxl_shm_lock_release",
    "clwb_region_with_barrier", "clflush_region_with_mfence", "clflush_region_with_sfence",
)


def solab_confirmation(library_path: str | None = None) -> AbiConfirmation:
    """Confirmation covering every proposed signature; pins the library hash when
    the node-specific hash is known (env or the resolved symlink target name)."""
    sha = None
    target = Path(library_path or os.environ.get("CXL_SHM_LIBRARY", "")).resolve().name
    if target in LIBRARY_SHA256 and LIBRARY_SHA256[target]:
        sha = LIBRARY_SHA256[target]
    return AbiConfirmation(
        {n: PROPOSED_SIGNATURES[n].digest() for n in CONFIRMED_NAMES}, library_sha256=sha
    )
