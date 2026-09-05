#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
# G-A cell procedure: (1) warm-up request outside the cell so a stale router entry
# cannot land inside it, (2) verify exactly one registered instance, (3) run the
# 1-session x N-turn closed loop, (4) snapshot s1 sidecar + probe into ~/vamp/ga/<cell>_*.
# usage: ga_cell.sh <cell_name> [turns] [prefix_words]
set -u
CELL="${1:?cell}"; TURNS="${2:-6}"; PW="${3:-400}"
source ~/venvs/dynamo05_vllm019/bin/activate
mkdir -p ~/vamp/ga
export ETCDCTL_API=3
echo "[cell $CELL] warm-up (excluded)"; python3 - <<'PY'
import json,urllib.request
b=json.dumps({"model":"Qwen/Qwen3-14B","messages":[{"role":"user","content":"warmup /no_think"}],"max_tokens":4}).encode()
r=urllib.request.Request("http://localhost:8080/v1/chat/completions",data=b,headers={"Content-Type":"application/json"})
try: urllib.request.urlopen(r,timeout=120).read(); print("  warmup ok")
except Exception as e: print("  warmup err (ignored):", str(e)[:80])
PY
N=$(~/bin/etcdctl --endpoints=http://localhost:2379 get instances/dynamo/backend/generate --prefix --keys-only 2>/dev/null | grep -c .)
echo "[cell $CELL] registered generate instances = $N"; [ "$N" = "1" ] || { echo "ABORT: expected 1 instance"; exit 2; }
# mark sidecar/probe line counts so the cell window can be isolated
S1="ssh -p 2022 ucsd@192.168.5.61"
$S1 "wc -l < ~/vamp/logs/wstats_s1.jsonl 2>/dev/null || echo 0" > ~/vamp/ga/${CELL}_wstats_start.txt
$S1 "wc -l < ~/vamp/logs/probe_s1.jsonl 2>/dev/null || echo 0" > ~/vamp/ga/${CELL}_probe_start.txt
echo "[cell $CELL] run"; date -u +%FT%TZ > ~/vamp/ga/${CELL}_start_ts.txt
python ~/vamp/routing_smoke.py --sessions 1 --turns "$TURNS" --prefix-tokens "$PW" --out ~/vamp/ga/${CELL}_runner.jsonl 2>&1 | tail -3
sleep 3; date -u +%FT%TZ > ~/vamp/ga/${CELL}_end_ts.txt
WS=$(cat ~/vamp/ga/${CELL}_wstats_start.txt); PS=$(cat ~/vamp/ga/${CELL}_probe_start.txt)
$S1 "tail -n +$((WS+1)) ~/vamp/logs/wstats_s1.jsonl" > ~/vamp/ga/${CELL}_wstats.jsonl
$S1 "tail -n +$((PS+1)) ~/vamp/logs/probe_s1.jsonl 2>/dev/null" > ~/vamp/ga/${CELL}_probe.jsonl
$S1 "cat ~/vamp/logs/probe_s1.jsonl 2>/dev/null" > ~/vamp/ga/${CELL}_probe_full.jsonl
echo "[cell $CELL] snapshot: wstats=$(wc -l < ~/vamp/ga/${CELL}_wstats.jsonl) probe_window=$(wc -l < ~/vamp/ga/${CELL}_probe.jsonl) probe_full=$(wc -l < ~/vamp/ga/${CELL}_probe_full.jsonl)"
