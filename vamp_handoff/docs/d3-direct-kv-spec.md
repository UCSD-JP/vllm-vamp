# D3 Direct GPU KV Feasibility

Scope: solab s1/s2, Qwen3-14B, TP=1, uniproc, one full-attention KV group,
one fixed-destination session, one transfer, idle workers. Provider and installed
vLLM/Dynamo files are unchanged. Load `solab/d3_agent.py` via the existing
`SPEC_MODULE=d3_agent` switch. This module patches scheduler methods in its own
process only; returning to `SPEC_MODULE=vamp_agent` restores the prior path.

## Data Path and Ownership

1. Before freeing the first substantial source request, pin its full GPU blocks
   (omit two terminal blocks). CPU offload leases/slot IDs are not used.
2. GPU operations and provider calls execute on the scheduler owner thread.
   RPC handlers only enqueue jobs; a small nudge wakes an idle EngineCore.
3. Gather canonical GPU int8 tensors into at most 128 MiB of GPU staging. Copy
   each chunk to CUDA-registered CXL. Synchronize, apply provider fence, then
   publish a versioned record containing GPU hashes/layout/digest and a lock.
4. B acquires that lock, checks layout/bounds, reserves GPU blocks (not visible
   in the prefix index), refreshes the payload, DMA-copies and scatters it.
5. Read back and hash B's actual scattered GPU KV; only on equality publish
   hash->GPU-block entries. Keep destination pins through the measured request.
   No CPU import/restore and no Dynamo KV discovery/events are claimed.
6. B releases GPU pins first; only on acknowledgement may A destroy the record,
   free the lock/payload and release source pins. Failed publication/removal
   preserves tracked resources for recovery. No concurrent real traffic.

## Measurements and Gates

- Fixed model/utilization/pool and the same bounded staging allocation in B0
  and D3. Cold restart both workers before each cell; retain manager/frontends.
- Log canonical pages, GPU block tokens, pool and staging capacity.
- GPU reservation invisible before success; partial publish/checksum failure
  removes hashes and frees blocks. GPU-free tests cover these invariants.
- Hardware: small and approximately 12.8K-token real KV payloads; full SHA256
  over A's gathered KV equals B's scattered KV. Integrity readback cost remains
  in the total operation time but is broken out from DMA/gather/scatter.
- B must show exactly the imported prefix's GPU-hit token count, correct marker,
  and no CPU connector restores for that prefix. Marker alone is insufficient.
- Matched B0 records zero GPU prefix hits on B's first request.
- Inject checksum mismatch after B reserve/scatter: no published hashes, skip
  B's measured request, successful B->A cleanup.
- Compare request latency and preparation+request (gap=0), never label request
  completion latency as pure prefill. Include all nudge/control/verification cost.

Not covered: pressure/concurrent migration, multi-session selection, partial
resident source prefixes, automatic routing, alternate models or GPU layouts,
crash recovery, hardware bandwidth saturation, checksum-free performance.

Native eager GPU->CPU offload remains enabled identically in both arms. D3
bypasses private DRAM for the migration payload, not all background offload or
debug traffic. Integrity verification intentionally reads GPU data back to CPU.
Destination/source pins are released after the measured B requests; unlike the
old staged WAIT hook, cleanup is not a pre-dispatch prerequisite here. Report
cleanup separately and do not directly equate this protocol with gap1/gap2.
