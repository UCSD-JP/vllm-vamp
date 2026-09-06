#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
# Generalized gate cell: warm-up (excluded) -> expected-instance check -> snapshot
# window markers on every worker -> run the given runner command -> snapshot windows.
# usage: gate_cell.sh <cell> <expected_instances> <worker_hosts:comma> -- <runner cmd...>
#   e.g. gate_cell.sh gb 2 192.168.5.61,localhost -- python ~/vamp/pressure_run.py --sessions 8 ...
set -u -o pipefail
CELL="${1:?cell}"; EXPECT="${2:?expected instances}"; HOSTS="${3:?worker hosts}"; shift 3
[ "${1:-}" = "--" ] && shift
RUNNER=("$@"); [ ${#RUNNER[@]} -gt 0 ] || { echo "runner cmd required"; exit 2; }
source ~/venvs/dynamo05_vllm019/bin/activate
mkdir -p ~/vamp/ga; export ETCDCTL_API=3
OUT=~/vamp/ga/${CELL}
echo "[cell $CELL] warm-up (excluded)"; WARMUP_URLS="${WARMUP_URLS:-http://localhost:8080/v1/chat/completions}" python3 - <<'PY'
import json,urllib.request,os
b=json.dumps({"model":"Qwen/Qwen3-14B","messages":[{"role":"user","content":"warmup /no_think"}],"max_tokens":4}).encode()
for u in os.environ["WARMUP_URLS"].split(","):
    r=urllib.request.Request(u,data=b,headers={"Content-Type":"application/json"})
    try: urllib.request.urlopen(r,timeout=120).read(); print("  warmup ok", u)
    except Exception as e: print("  warmup err (recorded, excluded):", u, str(e)[:80])
PY
sleep 2
# EXPECT is either a number (namespace "dynamo") or a list "ns1=n1,ns2=n2" for fixed endpoints
if [[ "$EXPECT" == *=* ]]; then
  IFS=, read -ra EXP <<< "$EXPECT"
  for pair in "${EXP[@]}"; do ns=${pair%%=*}; want=${pair#*=}
    N=$(~/bin/etcdctl --endpoints=http://localhost:2379 get instances/$ns/backend/generate --prefix --keys-only 2>/dev/null | grep -c .)
    echo "[cell $CELL] namespace $ns generate instances = $N (expected $want)"; [ "$N" = "$want" ] || { echo "ABORT"; exit 2; }
  done
else
  N=$(~/bin/etcdctl --endpoints=http://localhost:2379 get instances/dynamo/backend/generate --prefix --keys-only 2>/dev/null | grep -c .)
  echo "[cell $CELL] registered generate instances = $N (expected $EXPECT)"; [ "$N" = "$EXPECT" ] || { echo "ABORT"; exit 2; }
fi
run_on() { local h="$1"; shift; if [ "$h" = "localhost" ]; then bash -c "$*"; else ssh -p 2022 "ucsd@$h" "$*"; fi; }
tag_of() { case "$1" in 192.168.5.61) echo s1;; *) echo s2;; esac; }
IFS=, read -ra HL <<< "$HOSTS"
for h in "${HL[@]}"; do t=$(tag_of "$h")
  run_on "$h" "cat ~/vamp/logs/wstats_${t}.jsonl 2>/dev/null | wc -l" > ${OUT}_${t}_wstats_start.txt
  run_on "$h" "cat ~/vamp/logs/probe_${t}.jsonl 2>/dev/null | wc -l" > ${OUT}_${t}_probe_start.txt
done
FLOG="${FRONTEND_LOG:-$HOME/vamp/logs/frontend.log}"; FL_START=$(cat "$FLOG" 2>/dev/null | wc -l)
echo "[cell $CELL] run: ${RUNNER[*]}"; date -u +%FT%TZ > ${OUT}_start_ts.txt
"${RUNNER[@]}" --out ${OUT}_runner.jsonl > ${OUT}_runner.log 2>&1; RC=$?
tail -6 ${OUT}_runner.log
sleep 4; date -u +%FT%TZ > ${OUT}_end_ts.txt
for h in "${HL[@]}"; do t=$(tag_of "$h")
  WS=$(cat ${OUT}_${t}_wstats_start.txt); PS=$(cat ${OUT}_${t}_probe_start.txt)
  run_on "$h" "tail -n +$((WS+1)) ~/vamp/logs/wstats_${t}.jsonl" > ${OUT}_${t}_wstats.jsonl
  run_on "$h" "tail -n +$((PS+1)) ~/vamp/logs/probe_${t}.jsonl 2>/dev/null" > ${OUT}_${t}_probe.jsonl
  run_on "$h" "cat ~/vamp/logs/probe_${t}.jsonl 2>/dev/null" > ${OUT}_${t}_probe_full.jsonl
  echo "[cell $CELL] $t: wstats=$(wc -l < ${OUT}_${t}_wstats.jsonl) probe_window=$(wc -l < ${OUT}_${t}_probe.jsonl)"
done
tail -n +$((FL_START+1)) "$FLOG" 2>/dev/null | grep -o "Selected worker: [0-9]*, logit: [0-9.]*, cached blocks: [0-9]*" > ${OUT}_router.txt
echo "[cell $CELL] router decisions in window: $(wc -l < ${OUT}_router.txt)"
echo "[cell $CELL] runner rc=$RC $([ $RC -eq 0 ] && echo CELL_OK || echo CELL_INVALID)"
exit $RC
