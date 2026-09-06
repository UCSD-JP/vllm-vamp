# Near-complete Heavy-48 replay, arms S and L (rp1, 2026-09-06)

## What Was Executed

`solab/replay_suite.py --run rp1 --arms S L --sessions 48 --turns 0`: all recorded
turns of all 48 SWE-agent sessions (2,211 turns, 55,234,646 prompt tokens, max
prompt 62,382), closed loop (a session's next turn is ready when its previous
response completes; no think gaps), output cap 16 tokens, cold worker reset per
arm (heavy48_agent, MML 65,536 with static YaRN, CPU offload 64 GiB, GPU pool
97,408 tokens per node), fixed namespace endpoints vampA:8080 -> s1 and
vampB:8081 -> s2. Manager/frontends/etcd/NATS retained. No migration in either arm.

Admission: a turn is dispatched to a worker only if that worker's in-flight prompt
tokens + this prompt + 16 <= 0.8 x pool = 77,926 tokens; global concurrency 6.
Neither arm stalled (the c=8 concentration stall did not recur). Both arms
completed 2,211/2,211 with prompt-token identities matching the audit, and the
probe token identity `GPU-hit + CPU-queried == prompt` holds for every worker.

Decision layer (the only difference):

- **S**: owner = session_index % 2, every turn goes to its owner.
- **L**: same owner, but when the owner is over its admission budget and the other
  worker is under it, the turn goes to the other worker and is recomputed there
  (owner unchanged; the session may bounce back next turn).

## Results (one run per arm)

| | S | L |
| --- | ---: | ---: |
| Wall time, all 2,211 turns | **4,872 s** | 7,666 s |
| Aggregate throughput | 11,336 tok/s | 7,205 tok/s (-36%) |
| HTTP latency (dispatch -> done) p50 / p90 / p99 | 2.35 / 26.4 / 36.9 s | 19.2 / 32.4 / 40.2 s |
| TTFT p50 / p90 / p99 | 1.16 / 21.1 / 30.0 s | 13.2 / 26.1 / 33.4 s |
| Turns moved off owner | 0 | 917 |
| Worker changes between consecutive turns of a session | 0 | 892 |
| Requests / prompt tokens, s1 : s2 | 1,173 : 1,038 / 28.9M : 26.3M | 1,132 : 1,079 / 29.1M : 26.1M |
| GPU-hit tokens (last lookup per request) | 6.6% | 3.3% |
| CPU-tier restored tokens | 65.4% | 33.8% |
| **Recomputed tokens** | **28.0%** | **62.9%** |
| CPU-tier evicted blocks, s1 + s2 | 0.91M | 2.12M |
| Admission wait (client ready queue) p50 / max | 20.6 s / 944 s | 93.1 s / 1,250 s |

Per-turn HTTP latency in L: moved turns p50 21.2 s, stay turns p50 17.6 s (both far
above S's 2.35 s: after a bounce the returning turn finds a stale prefix at the
owner, and the whole system is congested by recompute).

## Reading the Result

Load-only rerouting without moving the KV **destroys locality**: each of the 892
bounces recomputes a 10-60K-token prefix on a worker that has never seen it, the
private DRAM tiers fill with duplicated prefixes (evictions x2.3), the recomputed
share of tokens goes from 28% to 63%, and total time rises 57%. The intended
benefit (balance) barely materializes: 29.1M/26.1M vs 28.9M/26.3M tokens. This is
the "load loses" regime measured at full-replay scale on the two-node testbed:
the decision layer must weigh where the KV is (reuse value) and the cost of moving
or recomputing it, not only queue load. It is the motivation for the valuation
arms (V-local / V-TCP / V-CXL in routing-valuation-spec.md), which choose both
destination and action with a cost model.

## Non-claims and Caveats

- One run per arm (n=1). Wall time and token shares are robust to noise at this
  scale; the latency percentiles are single-run distributions, not equivalence
  or significance claims.
- Admission wait is a closed-loop artifact (all 48 sessions ready at t=0) and is
  reported separately; do not present it as end-user latency.
- rp1 rows were written by the runner revision where the field named
  `arrival_to_done_s` holds the HTTP (dispatch -> done) latency and
  `admission_wait_s` is separate; later revisions name them `http_latency_s` and
  `arrival_to_done_s = admission_wait_s + http_latency_s`.
- S is not "the good policy": its 28% recompute and 65% DRAM restore show the
  private-DRAM-per-worker substrate under pressure; it is the no-decision baseline.
- No stock Dynamo KV-router arm was run (needs a single-namespace topology and its
  own KV-visibility gate). No migration arm was run here.

## Raw

- s2: `/home/ucsd/vamp/replay_results/rp1/` (S/, L/ client cell.jsonl, per-arm
  probe/sidecar slices, worker boot logs, source_sha256.json, manifests)
- JP: `solab_testbed/results/replay_rp1_2026-09-06/`
