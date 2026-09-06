# Heavy-48 Window and Migration Checkpoint

## Scope

This is a fixed-testbed feasibility experiment, not production routing. It uses
all 48 recorded SWE-agent sessions but only the first up to eight turns per
session (383 requests), not the complete 2,211-turn trace. Inputs are not
truncated. Tool roles and content arrays are normalized as in the existing
SWE replay client. Generated answers do not change later recorded prompts.
Consequently this is a serving replay, not an agent or SWE correctness score.

The complete audit has 55,234,646 prompt tokens, a maximum prompt of 62,382
tokens, and session lengths 7-75 turns. The eight-turn window does not yet
exercise that full lifetime skew. Its maximum prompt is 46,707 tokens.

## Fixed Setup

- Two RTX A6000 nodes, Qwen3-14B, TP=1 per node.
- Dynamo namespace vampA:8080 -> s1, vampB:8081 -> s2, one worker each.
- Native vLLM OffloadingConnector plus existing Vamp CPU manager/transport hooks.
- CPU offload 64 GiB per worker, GPU utilization 0.90, max sequences 16.
- MML 65,536 with static YaRN factor 4, original context 32,768,
  theta 1,000,000. No VLLM_ALLOW_LONG_MAX_MODEL_LEN bypass.
- The installed Transformers/vLLM uses `rope_parameters` in `--hf-overrides`.
  Legacy `rope_scaling` overrides did not enable YaRN in this snapshot.
- [Qwen's model card](https://huggingface.co/Qwen/Qwen3-14B) supplies the YaRN
  parameters. This changes numerical model configuration from the old
  short-context gates, so their latency is not a matched baseline here.
- Initial exploratory boot: 89,392 GPU KV tokens per node. Subsequent cold
  B0 and B1 boots: 97,408 per node. Capture actual boot logs for every arm;
  do not carry the exploratory pool value into the comparison.
- Request cap 16 output tokens, temperature zero; record realized output count.
  No replayed tool/think delays in the pressure phase.
- Source/provider/kernel/installed vLLM/Dynamo implementations remain unchanged.

## Pressure Phase

Sort the trace filenames, assign owner `index % 2`, and put all sessions in a
FIFO ready queue. The initial c=8 bundle allowed eight requests globally with
no per-worker limit; its B1 pressure phase stalled before any migration hook.
The revised comparison allows four globally and at most two per worker. Select
the earliest eligible ready request without reordering the others. Verify the
per-worker concurrency times the window's maximum prompt plus output cap is
strictly below the actual GPU pool. This is a bounded-admission workaround for
this testbed, not a root-cause fix or a production memory admission algorithm.
Append a session's next turn when its current response completes. There is no
global per-turn barrier and no overlap within a session. Natural completion
order can differ across arms; exact request issue order is recorded.

Disable automatic export pins on both nodes during this phase. Record prompt
fingerprints and completed GPU block hashes without retaining their payloads.
The policy here is static owner assignment, not CASS or stock Dynamo KV routing.
Per-request pressure TTFT/latency is measured from HTTP dispatch, excluding
time waiting in the controller's ready queue. Whole-phase elapsed time includes
that queue. Do not label the per-request samples end-user arrival-to-completion.

Pressure behavior is identical by design in B0/B1/B2; arm differences start at
the checkpoint. Do not attribute replay throughput differences to CXL. Report
actual GPU/CPU hits, CPU stores/evictions, queue pressure, and per-turn latency.
The sum of final prompt lengths (835,477 tokens) is a pressure candidate, not an
exact deduplicated physical footprint or proof of eviction by itself.

## Checkpoint

After all pressure requests finish, repeat turn 7 of the predetermined source
session `astropy__astropy-14182` at A. This explicit source anchor is separately
timed in every arm. It is not claimed to have remained resident during pressure.
The next recorded request is turn 8, forced to B.

1. Match server metadata to the source prompt's exact token SHA256 fingerprint.
2. Bound transferable full blocks by the LCP of the source and arriving prompt;
   conservatively exclude two source tail blocks.
3. Query B's CPU manager for already READY hashes. Transfer only missing hashes,
   so shared system-prefix blocks do not trigger the import duplicate guard.
4. Acquire one CPU export lease on A's scheduler thread; require every selected
   block READY. No hidden recompute on a failed readiness check.
5. B0 releases the lease without copying. B1 uses existing gd_hook TCP import.
   B2 uses existing gf_hook shared-CXL import. All use SHA256 verification.
6. Existing strict B-before-A cleanup must pass before dispatch to B. If B
   cleanup refuses, preserve A and fail the cell.
7. Send the actual next prompt to B. Record its TTFT and completion latency,
   preparation time, and arrival-to-completion including all control nudges.

Arrival is defined immediately after the explicit source-anchor response.
This is a quiescent, zero-exogenous-gap checkpoint with experimental control
costs included. It is not a measurement of overlapped live migration under load.
Also report hook-only time and destination serving time separately.

B1 is this testbed's 1 GbE TCP prototype, not optimized RDMA. B2 is
A CPU -> CXL pool -> B CPU -> GPU, not D3 GPU-direct migration. CPU and CXL
payload ownership/visibility follow the existing fixed provider APIs.

## Gates and Evidence

- Cold restart experiment workers per arm; retain manager/frontends/etcd/NATS.
- Verify one registered worker per namespace and no outstanding leases.
- Audit source file SHA256; prompt usage must equal tokenizer audit counts.
- Checkpoint fingerprint must match the server, not just prompt length.
- Archive only each arm's appended probe/sidecar log segment plus full boot log.
  Sidecar hit/query fields are per-update deltas: sum, do not difference them.
- GPU query/hit counters include repeated admission attempts under pressure.
  Keep their raw totals for auditing, but do not call their ratio a per-request
  cache hit rate. Deduplicate GPU lookups by request ID (last lookup), match the
  server fingerprint multiset to the client, and verify
  `last_gpu_hit_tokens + connector_query_tokens == client_prompt_tokens`.
  If that identity fails, withhold the request-level hit interpretation.
- Check all expected requests and all cleanup gates. Stop the bundle on failure.
- Inspect destination GPU/CPU lookup and imported bytes, not latency alone.
- Exact source snapshots and SHA256 accompany each bundle. No deployment edits
  while a bundle is running.

## Next Research Gate

The next policy experiment must allow nonuniform session lifetimes and migration
decisions before the replay drains. Extend one capability at a time: a longer
replay window/full replay, request-arrival cache/load signals, then online
STAY/recompute/prepare-and-migrate. D3 needs per-session GPU reservation and
bounded ownership under pressure before substituting it for staged B2.

This window alone cannot establish routing novelty, lifetime-aware scheduling,
aggregate CXL capacity benefits, or a general CXL-versus-network speedup.
