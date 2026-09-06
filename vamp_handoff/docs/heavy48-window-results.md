# Heavy-48 Feasibility Results, 2026-09-06

## What Was Executed

**PASS: three matched arms**, each with 48 sessions, first up to eight turns,
383 pressure requests, then one explicit source anchor and a forced A-to-B
continuation. Each pressure phase processed 4,491,425 prompt tokens without
input truncation. This is **not the full 2,211-turn replay**.

The matched bundle is `h48_window8_c4w2_20260906`: four active HTTP requests
globally, at most two per worker, static owner assignment. GPU pool was exactly
97,408 tokens per node in all three arms. CPU offload was 64 GiB per node.
Qwen3-14B used static YaRN and MML=65,536. Installed versions: vLLM 0.19.0,
Dynamo 0.5.0, torch 2.10.0, Transformers 4.57.6.

See [protocol and scope](heavy48-window-spec.md) for input normalization,
admission, source anchor, timing boundaries, and the unchanged provider boundary.

## Pressure Phase

| Metric | B0 | B1 | B2 |
| --- | ---: | ---: | ---: |
| Requests completed | 383/383 | 383/383 | 383/383 |
| Phase elapsed, seconds | 260.18 | 259.59 | 260.26 |
| s1 GPU hit, request-accounted | 1.20% | 1.21% | 1.21% |
| s2 GPU hit, request-accounted | 1.38% | 1.38% | 1.38% |
| s1 CPU hit among connector queries | 80.89% | 80.89% | 80.89% |
| s2 CPU hit among connector queries | 81.26% | 81.26% | 81.26% |
| s1 CPU evicted blocks | 0 | 0 | 0 |
| s2 CPU evicted blocks | 592 | 592 | 592 |

The GPU hit accounting uses the last lookup per request ID, not the raw sidecar
ratio: admission retries can increment the GPU query counter more than once.
For every node/arm, the fingerprint multiset matches the client, and
`GPU hit tokens + connector query tokens == client prompt tokens` exactly.
No export lease was held during pressure. Running/waiting maxima were two.
GPU usage peaks ranged from 0.760 to 0.824; this gauge alone is not a physical
resident-prefix footprint or eviction count.

This demonstrates substantial **DRAM-served KV reuse** under GPU capacity
pressure. CPU eviction on s2 is real, but these misses are not all eviction
churn: first-touch prefill is substantial. Do not infer CXL capacity benefits
from this phase: all arms use the same private-DRAM path until the checkpoint.

## Actual Migration Checkpoint

The source anchor has 12,376 prompt tokens; the next recorded prompt at B has
13,192. Of 771 conservative reusable full blocks, three were already CPU-READY
at B. B1/B2 moved the remaining **768 blocks, 2,013,265,920 bytes (1.875 GiB)**.

| Arm | Preparation incl. controls | B HTTP completion | Arrival-to-completion |
| --- | ---: | ---: | ---: |
| B0: recompute at B | 1.094 s | 4.802 s | 5.897 s |
| B1: private 1 GbE TCP | 26.663 s | 1.208 s | 27.872 s |
| B2: shared CXL, DRAM-staged | 6.566 s | 1.220 s | 7.787 s |

All are n=1 observations, gap=0, with SHA256 verification enabled. Preparation
includes metadata/selection nudges, hook process startup, import, and cleanup.
B0 also pays the common diagnostic selection/release work; this is not the
minimal control cost of a production recompute path. Its HTTP time is reported
separately so that overhead is visible. The source anchor itself is separately
timed and precedes the defined arrival.

Mechanism evidence:

- B0: 48 GPU-hit tokens, zero connector-hit tokens on the measured B request.
- B1/B2: 48 GPU-hit plus **12,288 CPU-restored tokens**, leaving 856 prompt
  tokens to compute. The imported payload is actually used.
- B2: provider export, cross-host import, SHA256 verification and commit,
  B-before-A cleanup, key/payload/hash destruction and lease return all passed.
- All three continuation outputs have the same SHA256 digest. Output was capped
  at 16 tokens and realized usage was 15; this is not a SWE solution score.
- Both hook cleanup gates and final cleanup passed for every matched arm.

B2 hook breakdown: CPU gather 0.818 s, CXL write 0.231 s, fence 0.150 s,
refresh 0.152 s, CXL-to-CPU copy 0.658 s, source/destination SHA256
1.187/1.942 s. Hook-internal total was 5.894 s; controller-observed hook wall
time was 6.033 s. Do not estimate a checksum-free result by subtraction and
present it as measured.

**Reading the result:** preparation over this CXL path was shorter than the
testbed's 1 GbE TCP path, but CXL WAIT still lost to recompute at gap=0 with
verification. Ready-at-B serving latency alone would hide that loss.

## Failure Preserved

The earlier c=8 bundle passed B0 but stalled in the B1 pressure phase at
314/383, before network import began. It is not part of the matched table.
The stall lasted at least 181.61 seconds with s2 running=0, waiting=8 and
GPU utilization=0. Export leases were zero and both transport state machines
were idle. See [the preserved stall record](heavy48-c8-stall.md).

The per-worker cap is a bounded testbed workaround, **not a proven root-cause
fix**. A native-only reproduction and scheduler-state diagnosis remain open.

## Code and Validation

- `solab/heavy48_workload.py`: tokenizer audit, fingerprints, pool-log parser.
- `solab/heavy48_agent.py`: unpinned metadata during pressure, owner-thread
  CPU-READY selection at the quiescent checkpoint, request-level lookup trace.
- `solab/heavy48_replay.py`: fixed trace window and existing staged hooks.
- `solab/heavy48_reset.py`, `worker_heavy48.sh`, `heavy48_suite.py`: cold arm
  isolation, explicit long-context configuration, capacity gate, source/log archive.
- `solab/heavy48_report.py`: request identity and token-accounting cross-checks.
- New tests: **12/12 PASS** in the installed vLLM environment after the final
  parser cleanup. The final parser reproduces the archived analysis exactly.
- Existing suite: 50 PASS, eight installed-vLLM tests skipped locally.
  SOURCE_LOCK: 51/51 PASS. No provider, kernel, installed vLLM/Dynamo source edits.
- Pre-commit passed with shellcheck skipped because the binary was unavailable;
  `bash -n` passed. The executed source snapshots preserve the pre-cleanup
  boot-log parser; the final parser is tested against the same archived logs.

## Artifacts and Next Step

On s2: `/home/ucsd/vamp/heavy48_results/` contains the matched bundle,
the c=8 partial bundle, and the two-session TCP wiring smoke. Each bundle
contains exact source snapshots/hashes, boot logs, per-arm probe/sidecar slices,
client JSONL, hook/cleanup logs and the full tokenizer audit.

JP archive:
`/home/jp/paper_resource/asplos_paper/solab_testbed/results/heavy48_2026-09-06/`.

Both experiment GPU workers were stopped after the matched bundle. The CXL
manager and infrastructure were retained.

This establishes a Heavy replay pressure workload and a real staged migration
checkpoint. It does **not** establish online routing benefit, concurrent live
migration, persistent shared-cache capacity benefit, or Heavy-48 D3 direct DMA.
The next step is a longer, nonuniform-lifetime pressure replay and migration
while other sessions remain active, with explicit admission and memory ownership
budgets. D3 needs per-session GPU selection/reservation before that substitution.
