# Near-complete replay with a routing/migration decision layer

## Goal

Run (near-)all recorded turns of the Heavy-48 SWE-agent trace over the two-node
testbed, varying only the decision layer, and report per-turn serving cost and
migration work. This is the first experiment where a decision layer is compared,
not just a transfer mechanism. It is still a fixed testbed, one model, TP=1, two
A6000 nodes, recorded prompts (a serving replay, not an agent-correctness score).

## Scale

- 48 sessions, 7-75 turns each, 2,211 turns total, 55.2M prompt tokens, max
  prompt 62,382 tokens (audit `heavy48_qwen14b_audit.json`).
- Recorded inter-turn `execution_time` is tool time, median 0.12 s, total 655 s:
  effectively no think-gap, so pressure is set by prompt length and concurrency.
- "Near-complete" = all turns of every session, in recorded order per session.
  If wall time forces a cap, cap by turns-per-session uniformly and say so.

## Admission (fixes the c=8 stall)

The c=8 stall was many long external-restore requests concentrating on one worker
until its KV pool was reserved and admission blocked. Replace the fixed
per-worker request cap with a **token budget**: a request is admitted to a worker
only if (sum of in-flight prompt tokens on that worker) + this prompt + output
cap <= budget, where budget = floor(0.8 * GPU_KV_pool_tokens). Otherwise it waits
in the ready queue. Global concurrency stays bounded (e.g. 6). This is an
experiment configuration bound (uses only the arriving prompt length), not an
online policy that peeks at future lifetimes. Record admission waits separately.

## Arms (decision layer only; identical inputs, cold reset per arm)

Owner of a session = the worker that served its first turn. A turn's prompt reuses
that session's prefix, so KV locality is per session.

- **stock**: single Dynamo namespace, `--router-mode kv`, both workers registered.
  The stock KV router decides placement. No Vamp migration. Baseline for "what
  the substrate already does".
- **S (static)**: fixed owner = `session_index % 2`, no migration. Baseline for
  "no decision".
- **L (load rebalance, recompute)**: when a worker's admission queue depth exceeds
  the other's by a threshold, the next ready turn of a waiting session is sent to
  the lighter worker, which recomputes its prefix (no migration). Tests "move the
  request, pay recompute".
- **M (load rebalance, prepare-and-migrate)**: same trigger as L, but before
  dispatching the moved turn, migrate that session's source prefix from the old
  owner to the new worker over staged CXL (existing gf_hook path, `verify=gpu64`
  once the staged path supports it; sha256 until then, labeled), then serve.
  Tests "move the request, pay migration instead of recompute". WAIT policy: the
  moved turn dispatches after migration+cleanup succeeds; if migration fails, fall
  back to recompute on the new worker and record the fallback.

All arms: same 0.8-pool token budget, same global concurrency, same trace order.
Migration in M happens only at a session boundary for the moved session; other
sessions keep serving (this is the first "migrate while others are active" step,
but the migrated session itself is idle at its own boundary).

## Metrics (per arm)

- Per-turn: arrival->response-complete, TTFT, worker, prompt tokens, GPU-hit
  tokens (last lookup per request id), CPU-restored tokens, admission wait.
- Migration (M): count, bytes, per-migration hook time (transfer_s) and cleanup,
  verify mode, fallbacks.
- Whole run: total wall, p50/p90/p99 of arrival->done and TTFT (report as
  distributions/ranges, not equivalence claims), per-worker DRAM/GPU hit rates,
  CPU stores/evictions, total migrated bytes.
- Token accounting identity per request (`gpu_hit + connector_query == prompt`)
  must hold or the request-level hit interpretation is withheld.

## Gates

- Cold reset workers per arm; retain manager/frontends/etcd/NATS; fresh CXL arena
  before the M arm (and before the bundle if a prior bundle was killed).
- One registered worker per namespace (two for stock's single namespace); no
  outstanding leases at start.
- Every expected turn completes; every migration's cleanup gate passes.
- Archive per-arm client JSONL, probe/sidecar slices, worker boot logs, source
  hashes, and a manifest. Raw not committed; linked by path.

## Non-claims

This does not establish production routing, concurrent live migration (the moved
session is idle at its boundary), persistent shared-cache capacity benefit, or a
general CXL-vs-network result. It is a controlled decision-layer comparison on one
recorded workload at testbed scale.

## Build order

1. Token-budget admission + full-replay harness with the S arm and the stock
   baseline; a short smoke (first 8 turns) then the full run. Confirm no stall.
2. Add L (recompute rebalance).
3. Add M (staged-CXL migrate rebalance) reusing gf_hook.
4. Report; then consider D3 direct migration as M's transfer once per-session GPU
   reservation under pressure exists.
