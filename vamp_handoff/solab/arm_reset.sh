#!/bin/bash
# Cold reset of both workers between comparison arms (run on s2 = node 0).
# Stops dynamo.vllm + EngineCore on s1 and s2, relaunches both with the fixed
# per-worker parameters, and waits until both in-engine agents answer. The CXL
# manager is NOT touched (review 2026-09-06: never restart it between arms).
set -u
S1=${S1:-192.168.5.61}
ssh -o BatchMode=yes -p 2022 ucsd@$S1 '~/vamp/worker_stop.sh' || { echo "s1 stop failed"; exit 1; }
~/vamp/worker_stop.sh || { echo "s2 stop failed"; exit 1; }
ssh -o BatchMode=yes -p 2022 ucsd@$S1 'NS=vampA TAG=s1 PORT=6880 CPU_GB=64 PIN=1 setsid ~/vamp/worker_vamp.sh > /dev/null 2>&1 < /dev/null & echo s1 launched'
NS=vampB TAG=s2 PORT=6881 CPU_GB=64 PIN=0 setsid ~/vamp/worker_vamp.sh > /dev/null 2>&1 < /dev/null &
echo "s2 launched"
probe() { timeout 3 bash -c "echo '{\"cmd\":\"status\"}' | nc -w 2 $1 $2" 2>/dev/null | head -c 12; }
for i in $(seq 1 40); do
  sleep 10
  a=$(probe $S1 7001); b=$(probe localhost 7002)
  echo "[$i] A=${a:-down} B=${b:-down}"
  if [ -n "$a" ] && [ -n "$b" ]; then sleep 5; echo "BOTH_AGENTS_UP"; exit 0; fi
done
echo "workers did not come up"; exit 1
