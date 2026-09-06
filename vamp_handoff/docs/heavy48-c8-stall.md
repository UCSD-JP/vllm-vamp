# Heavy-48 c=8 Stall Record

Bundle: `h48_window8_20260906`, 2026-09-06, before per-worker admission cap.

## Observed

- B0 completed 383 requests, the recompute checkpoint, and strict cleanup.
- B1 completed 314/383 pressure requests, then no further responses for at least
  181.61 seconds. Network export/import had not started.
- The last archived s2 stats were running=0, waiting=8, GPU KV usage=0.984886.
  NVIDIA utilization was 0%. The process continued emitting idle stats.
- Read-only agent status on both nodes: active leases=0, no candidate,
  network and CXL import stages idle, no CXL export held, no payload, queue=0.
- B2 was not run in this bundle. This is not evidence of a network/CXL failure.

The B1 runner was terminated, its logs archived, and both experiment workers
stopped. No external CXL payload lease was held. Manager/frontends were retained.
The original files are preserved at
`/home/ucsd/vamp/heavy48_results/h48_window8_20260906/` on s2.

## Interpretation

This is an observed pressure/admission/restore stall, not a proven root cause.
The initial runner bounded global active requests at eight but allowed all eight
to concentrate on one worker as other sessions completed. Several long external
restore requests can reserve most of the local KV pool. The observations are
consistent with blocked admission/restore progress, but do not identify the
precise scheduler transition responsible. The custom adapter itself has not
been excluded by a native-only reproduction.

Do not claim a native vLLM bug, a network failure, or a solved deadlock.
Do not compare the old B0 cell with a differently configured B1/B2 cell.

## Bounded Demo Workaround

The revised bundle `h48_window8_c4w2_20260906` uses the same traces and eight-turn
window, at most four active requests globally, and at most two per worker.
Before each arm, its launch gate checks that two maximum-sized prompts in the
assigned window plus capped outputs fit within the measured GPU token pool.
This offline audit is an experiment configuration bound, not an online policy
using future session lifetime. All three arms must rerun under this same cap.

Two FIFO/admission tests were added; the complete new test file has ten passing
tests in the installed vLLM environment. The workaround's runtime outcome is
recorded separately in the final Heavy-48 results document.
