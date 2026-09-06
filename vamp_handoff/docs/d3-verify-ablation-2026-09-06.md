# D3 Direct-Path Verification-Cost Ablation, 2026-09-06

## What Was Executed

Four cold-reset cells, identical prompt (salt `w1`, 1000 words, 13,819 prompt
tokens, 861 exported blocks = 2,257,059,840 bytes), gap=0, one measurement each,
on a freshly initialized CXL arena. The direct GPU->CXL->GPU path (D3) is run
with three verification modes; B0 recompute is the baseline. Primary metric is
arrival-to-response-complete at gap=0. New code: a `verify` mode on the D3 GPU
bridge and record; provider, kernel and installed vLLM/Dynamo are unchanged.

| Cell | Arrival->done, gap=0 | B HTTP | Export op | Import op | Verify exp/imp | DMA exp/imp | Verified |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | :---: |
| Recompute B0 | 4.707 s | 4.570 s | - | - | - | - | - |
| Direct, SHA-256 (host readback) | 13.058 s | 0.594 s | 6.143 s | 5.968 s | 5.299 / 5.365 s | 0.310 / 0.215 s | yes |
| Direct, gpu64 (per-block GPU sums) | **2.277 s** | 0.556 s | 0.784 s | 0.593 s | 0.011 / 0.016 s | 0.310 / 0.212 s | yes |
| Direct, none (skipped) | 2.311 s | 0.560 s | 0.788 s | 0.616 s | 0 / 0 s | 0.310 / 0.212 s | no |

All four cells passed their reuse, no-CPU-restore and B-before-A cleanup gates.
For the two direct cells that beat recompute, B's first request reused the
imported GPU KV directly (GPU-hit prefix, zero CPU external restore).

## Reading the Result

- The earlier D3 conclusion "direct migration is slower than recompute at gap=0"
  was **entirely the SHA-256-over-host-readback verification cost** (10.7 s of
  the 12.5 s preparation), not the CXL or DMA path. The DMA itself is
  0.31 + 0.21 s; register + visibility + gather/scatter add about 0.7 s.
- A GPU-resident per-block 64-bit checksum (`gpu64`) costs 0.01 s and yields the
  same time as skipping verification (2.277 vs 2.311 s), while still detecting
  block misplacement/order errors and gross corruption. It is **not
  cryptographic**: it does not defend against adversarial collisions. SHA-256
  remains the correctness baseline that proved byte equality; the performance
  path is explicitly labeled `gpu64`.
- With `gpu64`, direct GPU->CXL->GPU migration at gap=0, with an integrity
  check, is about half the arrival-to-done of recompute (2.28 vs 4.71 s), and
  B's own response is 0.56 vs 4.57 s.

This is n=1 per cell, one model, TP=1, single transfer, idle worker. It does not
establish a distribution, a policy benefit, or online migration under load. It
does establish that the verification implementation, not the CXL/DMA path, set
the previous break-even, and that a cheap GPU-side integrity check moves the
break-even below gap=0 for this prefix size.

## Raw

- s2: `/home/ucsd/vamp/d3_results/verify_20260906T074532/`
- JP: `solab_testbed/results/d3_verify_2026-09-06/`
- Each cell has `cell.jsonl`, per-node worker/probe/sidecar logs, `manifest.json`
  with the executed command and source SHA256s.

## Operational Note

The first attempt crashed in `shm_payload_alloc -> _acquire_lock` (native abort,
no Python exception): a prior killed bundle left the shared allocator lock held,
so every later payload allocation timed out. B0 recompute (no CXL) passed, which
localized the fault to the CXL path. `cxl_fresh_start.sh` (manager restart +
`cxl_clear_ucsd`) cleared it. Rule: run a fresh start before a CXL bundle when a
prior bundle was killed.
