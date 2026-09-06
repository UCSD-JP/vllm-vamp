#!/bin/bash
# Stop the local dynamo.vllm worker *and* its EngineCore child, then wait until the
# GPU is free. `pkill -f dynamo.vllm` alone leaves the EngineCore (comm
# "VLLM::EngineCore", reparented to init) holding the GPU and the agent ports, and
# the next worker launch fails at engine init (observed 2026-09-06 on s2).
pkill -f "[d]ynamo\.vllm" 2>/dev/null
sleep 3
pkill -f "^VLLM::[E]ngineCore" 2>/dev/null
for i in $(seq 1 30); do
  if ! pgrep -f "[d]ynamo\.vllm|^VLLM::[E]ngineCore" >/dev/null && [ -z "$(nvidia-smi --query-compute-apps=pid --format=csv,noheader)" ]; then
    echo "worker stopped, GPU free"; exit 0
  fi
  [ "$i" -eq 10 ] && pkill -9 -f "[d]ynamo\.vllm|^VLLM::[E]ngineCore" 2>/dev/null
  sleep 2
done
echo "worker processes still present:"; pgrep -af "[d]ynamo\.vllm|^VLLM::[E]ngineCore"; exit 1
