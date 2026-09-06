#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
# Dedicated YaRN launcher; leaves the existing small-context launcher unchanged.
set -eu
export CPATH="$HOME/local/usr/include:$HOME/local/usr/include/python3.10:$HOME/local/usr/include/x86_64-linux-gnu/python3.10:${CPATH:-}"
export ETCD_ENDPOINTS=http://192.168.5.62:2379 NATS_SERVER=nats://192.168.5.62:4222
export DYN_NAMESPACE="$NS" PYTHONHASHSEED=0
export PYTHONPATH="$HOME/vamp/vamp_handoff:$HOME/vamp/probe:${PYTHONPATH:-}"
export VAMP_PROBE_FILE="$HOME/vamp/logs/probe_${TAG}.jsonl"
case "$TAG" in
  s1) export VAMP_AGENT_PORT=7001 VAMP_PAYLOAD_PORT=7101;;
  s2) export VAMP_AGENT_PORT=7002 VAMP_PAYLOAD_PORT=7102;;
  *) exit 2;;
esac
export VAMP_AGENT_PIN=0 VAMP_PIN_MIN_BLOCKS=64
export CXL_SHM_LIBRARY="$HOME/vamp/cxl/lib/libcxl_shm.so"
export CUDA_VISIBLE_DEVICES=0 DYN_SYSTEM_ENABLED=true DYN_SYSTEM_PORT="$PORT"
export VAMP_STATS_FILE="$HOME/vamp/logs/wstats_${TAG}.jsonl"
exec "$HOME/venvs/dynamo05_vllm019/bin/python" -m dynamo.vllm \
  --model Qwen/Qwen3-14B \
  --kv-transfer-config '{"kv_connector":"OffloadingConnector","kv_role":"kv_both","kv_connector_extra_config":{"spec_module_path":"heavy48_agent","spec_name":"VampOffloadingSpec","cpu_bytes_to_use":68719476736,"eviction_policy":"lru"}}' \
  --hf-overrides '{"rope_parameters":{"rope_type":"yarn","factor":4.0,"original_max_position_embeddings":32768,"rope_theta":1000000.0}}' \
  --gpu-memory-utilization 0.90 --max-model-len 65536 --max-num-seqs 16 \
  > "$HOME/vamp/logs/worker_${TAG}.log" 2>&1
