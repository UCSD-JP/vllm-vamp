#!/bin/bash
# Cold reset of both workers between comparison arms (run on s2 = node 0).
# Definition (review 2026-09-06): worker + EngineCore processes are restarted (GPU and
# CPU tiers start empty); the CXL manager, etcd, nats and both frontends stay up.
# Ready = both agents answer with a bridge, both namespaces have exactly one generate
# instance in etcd, and a real request succeeds on both frontends.
set -u
S1=${S1:-192.168.5.61}
ssh -o BatchMode=yes -p 2022 ucsd@$S1 '~/vamp/worker_stop.sh' || { echo "s1 stop failed"; exit 1; }
~/vamp/worker_stop.sh || { echo "s2 stop failed"; exit 1; }
ssh -o BatchMode=yes -p 2022 ucsd@$S1 'NS=vampA TAG=s1 PORT=6880 CPU_GB=64 PIN=1 setsid ~/vamp/worker_vamp.sh > /dev/null 2>&1 < /dev/null & echo s1 launched'
NS=vampB TAG=s2 PORT=6881 CPU_GB=64 PIN=0 setsid ~/vamp/worker_vamp.sh > /dev/null 2>&1 < /dev/null &
echo "s2 launched"
probe() { timeout 5 python3 - "$1" "$2" <<'PY' 2>/dev/null
import json, socket, sys
s = socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=3); s.sendall(b'{"cmd":"status"}\n')
d = json.loads(s.makefile().readline()); print("ready" if d.get("ok") and d.get("bridge_block_bytes") else "nobridge")
PY
}
for i in $(seq 1 40); do
  sleep 10
  a=$(probe $S1 7001); b=$(probe localhost 7002)
  echo "[$i] A=${a:-down} B=${b:-down}"
  [ "$a" = "ready" ] && [ "$b" = "ready" ] && break
done
[ "$a" = "ready" ] && [ "$b" = "ready" ] || { echo "agents not ready"; exit 1; }
sleep 3
export ETCDCTL_API=3
for ns in vampA vampB; do
  N=$(~/bin/etcdctl --endpoints=http://localhost:2379 get instances/$ns/backend/generate --prefix --keys-only 2>/dev/null | grep -c .)
  [ "$N" = "1" ] || { echo "namespace $ns has $N generate instances (expected 1)"; exit 1; }
done
source ~/venvs/dynamo05_vllm019/bin/activate
python3 - <<'PY' || exit 1
import json, urllib.request
b = json.dumps({"model": "Qwen/Qwen3-14B", "messages": [{"role": "user", "content": "reset-check /no_think"}], "max_tokens": 2}).encode()
for u in ("http://localhost:8080/v1/chat/completions", "http://localhost:8081/v1/chat/completions"):
    r = urllib.request.Request(u, data=b, headers={"Content-Type": "application/json"})
    d = json.load(urllib.request.urlopen(r, timeout=180)); assert d.get("choices"), u
    print("  endpoint ok", u)
PY
echo "RESET_OK: workers restarted, agents+bridge ready, etcd vampA=1 vampB=1, both endpoints answered"
