# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""Bounded GPU gather/scatter and registered CXL DMA, fixed single-GPU layout."""

import ctypes
import hashlib
import time

import torch
from d3_blocks import chunk_ranges


class GpuBridge:
    def __init__(self, tensors, capacity=128 << 20):
        if not tensors or any(
            t.ndim != 2
            or t.dtype != torch.int8
            or not t.is_cuda
            or not t.is_contiguous()
            for t in tensors
        ):
            raise ValueError("expected canonical contiguous CUDA int8 KV tensors")
        self.tensors = tensors
        self.pages = [t.shape[1] for t in tensors]
        self.block_bytes = sum(self.pages)
        self.capacity = (capacity // self.block_bytes) * self.block_bytes
        if self.capacity == 0:
            raise ValueError("staging buffer smaller than one block")
        self.stage = torch.empty(
            self.capacity, dtype=torch.int8, device=tensors[0].device
        )
        with open("/proc/self/maps") as f:
            path = next(line.split()[-1] for line in f if "libcudart" in line)
        self.cuda = ctypes.CDLL(path)
        for name, signature in {
            "cudaHostRegister": [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint],
            "cudaHostUnregister": [ctypes.c_void_p],
            "cudaMemcpy": [
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_size_t,
                ctypes.c_int,
            ],
            "cudaDeviceSynchronize": [],
        }.items():
            getattr(self.cuda, name).argtypes = signature
            getattr(self.cuda, name).restype = ctypes.c_int

    def check(self, rc):
        if rc:
            raise RuntimeError(f"CUDA runtime error {rc}")

    def gather(self, block_ids):
        ids = torch.tensor(block_ids, dtype=torch.int64, device=self.stage.device)
        pos = 0
        for tensor, page in zip(self.tensors, self.pages):
            count = len(block_ids) * page
            torch.index_select(
                tensor,
                0,
                ids,
                out=self.stage[pos : pos + count].view(len(block_ids), page),
            )
            pos += count
        torch.cuda.synchronize()
        return pos

    def scatter(self, block_ids):
        ids = torch.tensor(block_ids, dtype=torch.int64, device=self.stage.device)
        pos = 0
        for tensor, page in zip(self.tensors, self.pages):
            count = len(block_ids) * page
            tensor.index_copy_(
                0, ids, self.stage[pos : pos + count].view(len(block_ids), page)
            )
            pos += count
        torch.cuda.synchronize()

    VERIFY_MODES = ("sha256", "gpu64", "none")

    def gpu_block_sums(self, nblocks, nbytes):
        """Per-block 64-bit wraparound sums computed on the GPU over the staging
        chunk. Non-cryptographic: detects block misplacement/order errors and
        gross corruption without reading 2 GiB back to the host."""
        x = self.stage[:nbytes].view(torch.int64).view(nblocks, -1)
        return x.sum(dim=1).cpu().numpy().tobytes()

    def transfer(self, block_ids, ptr, to_cxl, api, verify="sha256"):
        """verify: 'sha256' = read every chunk back to the host and SHA-256 it
        (correctness baseline); 'gpu64' = per-block GPU sums hashed (no readback);
        'none' = verification skipped (recorded as skipped, never as verified)."""
        if verify is True:
            verify = "sha256"
        if verify is False or verify is None:
            verify = "none"
        if verify not in self.VERIFY_MODES:
            raise ValueError(f"unknown verify mode {verify}")
        size = len(block_ids) * self.block_bytes
        timings = dict(
            register_s=0.0,
            gather_s=0.0,
            dma_s=0.0,
            scatter_s=0.0,
            visibility_s=0.0,
            verify_s=0.0,
            unregister_s=0.0,
        )
        sha = hashlib.sha256()
        start = time.monotonic()
        self.check(self.cuda.cudaHostRegister(ptr, size, 0))
        timings["register_s"] = time.monotonic() - start
        try:
            if not to_cxl:
                start = time.monotonic()
                api.refresh(ptr, size)
                timings["visibility_s"] += time.monotonic() - start
            for lo, hi in chunk_ranges(len(block_ids), self.block_bytes, self.capacity):
                ids = block_ids[lo:hi]
                nbytes = len(ids) * self.block_bytes
                offset = lo * self.block_bytes
                if to_cxl:
                    start = time.monotonic()
                    self.gather(ids)
                    timings["gather_s"] += time.monotonic() - start
                start = time.monotonic()
                dst, src = (
                    (ptr + offset, self.stage.data_ptr())
                    if to_cxl
                    else (self.stage.data_ptr(), ptr + offset)
                )
                self.check(self.cuda.cudaMemcpy(dst, src, nbytes, 2 if to_cxl else 1))
                self.check(self.cuda.cudaDeviceSynchronize())
                timings["dma_s"] += time.monotonic() - start
                if not to_cxl:
                    start = time.monotonic()
                    self.scatter(ids)
                    timings["scatter_s"] += time.monotonic() - start
                if verify != "none":
                    start = time.monotonic()
                    # Verify the scattered destination KV, not only staging.
                    if not to_cxl:
                        self.gather(ids)
                    if verify == "sha256":
                        sha.update(self.stage[:nbytes].cpu().numpy().tobytes())
                    else:
                        sha.update(self.gpu_block_sums(len(ids), nbytes))
                    timings["verify_s"] += time.monotonic() - start
            if to_cxl:
                start = time.monotonic()
                api.fence(ptr, size)
                timings["visibility_s"] += time.monotonic() - start
        finally:
            start = time.monotonic()
            self.check(self.cuda.cudaHostUnregister(ptr))
            timings["unregister_s"] = time.monotonic() - start
        digest = sha.hexdigest() if verify != "none" else None
        return dict(
            timings,
            verify=verify,
            digest=digest,
            sha256=digest if verify == "sha256" else None,
            nbytes=size,
            staging_bytes=self.capacity,
            chunks=len(
                list(chunk_ranges(len(block_ids), self.block_bytes, self.capacity))
            ),
        )
