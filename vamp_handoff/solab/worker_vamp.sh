#!/bin/bash
# Worker with VampOffloadingSpec (vamp_cxl.vllm_binding via spec_module_path) + probe listener.
# env: TAG (s1|s2) PORT CPU_GB MODEL UTIL MML MNS
export CPATH="$HOME/local/usr/include:$HOME/local/usr/include/python3.10:$HOME/local/usr/include/x86_64-linux-gnu/python3.10:$CPATH"
source "$HOME/venvs/dynamo05_vllm019/bin/activate"
export ETCD_ENDPOINTS=http://192.168.5.62:2379
export NATS_SERVER=nats://192.168.5.62:4222
# fixed per-worker endpoint: each worker lives in its own Dynamo namespace (NS) so a
# frontend started with --namespace NS is a verified single-worker endpoint
export DYN_NAMESPACE="${NS:-dynamo}"
# vLLM derives its block-hash seed from PYTHONHASHSEED; fix it so BlockHash values
# for the same tokens match across hosts (needed for cross-host KV import)
export PYTHONHASHSEED=0
export PYTHONPATH="$HOME/vamp/vamp_handoff:$HOME/vamp/probe:${PYTHONPATH:-}"
MODEL="${MODEL:-Qwen/Qwen3-14B}"; UTIL="${UTIL:-0.90}"; MML="${MML:-16384}"; MNS="${MNS:-16}"
CPU_GB="${CPU_GB:-64}"; TAG="${TAG:-s1}"; PORT="${PORT:-6880}"
LOGD="$HOME/vamp/logs"; mkdir -p "$LOGD"
export VAMP_PROBE_FILE="$LOGD/probe_${TAG}.jsonl"
# in-engine agent (G-D): control + payload ports per worker; pin the first READY run >= 64 blocks
case "$TAG" in s1) export VAMP_AGENT_PORT=7001 VAMP_PAYLOAD_PORT=7101;; s2) export VAMP_AGENT_PORT=7002 VAMP_PAYLOAD_PORT=7102;; esac
export VAMP_PIN_MIN_BLOCKS="${PIN_MIN:-64}"
# CXL path (G-F): node-specific provider library through our symlink dir (s2->N0, s1->N1)
export CXL_SHM_LIBRARY="$HOME/vamp/cxl/lib/libcxl_shm.so"
SPEC_MODULE="${SPEC_MODULE:-vamp_agent}"
CPU_BYTES=$(( CPU_GB * 1024 * 1024 * 1024 ))
KVCFG="{\"kv_connector\":\"OffloadingConnector\",\"kv_role\":\"kv_both\",\"kv_connector_extra_config\":{\"spec_module_path\":\"$SPEC_MODULE\",\"spec_name\":\"VampOffloadingSpec\",\"cpu_bytes_to_use\":$CPU_BYTES,\"block_size_factor\":2,\"eviction_policy\":\"lru\"}}"
CUDA_VISIBLE_DEVICES=0 DYN_SYSTEM_ENABLED=true DYN_SYSTEM_PORT=$PORT \
  VAMP_STATS_FILE="$LOGD/wstats_${TAG}.jsonl" \
  exec python -m dynamo.vllm --model "$MODEL" \
    --kv-transfer-config "$KVCFG" \
    --gpu-memory-utilization "$UTIL" --max-model-len "$MML" --max-num-seqs "$MNS" \
    > "$LOGD/worker_${TAG}.log" 2>&1
