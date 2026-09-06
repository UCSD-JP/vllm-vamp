#!/bin/bash
# B0/B1/B2 controlled comparison, one session, identical prompt (same --salt) in
# every arm; only the arm's transfer mechanism and the CXL key differ. Each arm
# starts from a cold reset of both workers (arm_reset.sh); the manager keeps running.
#   B0: A turns -> (no transfer) -> B turns   = destination local-cold recompute
#   B1: A turns -> gd_hook (network KV)  -> B turns
#   B2: A turns -> gf_hook (CXL KV)      -> B turns
# usage: compare_arms.sh <salt> [arms...]   e.g. compare_arms.sh cmp B0 B1 B2
set -u -o pipefail
SALT="${1:?salt}"; shift
ARMS=("$@"); [ ${#ARMS[@]} -gt 0 ] || ARMS=(B0 B1 B2)
TA=${TURNS_A:-3}; TB=${TURNS_B:-2}; PW=${PREFIX_WORDS:-1000}
export WARMUP_URLS="http://localhost:8080/v1/chat/completions,http://localhost:8081/v1/chat/completions"
for arm in "${ARMS[@]}"; do
  case "$arm" in
    B0) HOOK="";;
    B1) HOOK="python ~/vamp/gd_hook.py";;
    B2) HOOK="python ~/vamp/gf_hook.py --key VAMP_KV_${SALT}_b2";;
    *) echo "unknown arm $arm"; exit 2;;
  esac
  echo "################ arm $arm (salt $SALT) ################"
  ~/vamp/arm_reset.sh | tail -2 || { echo "reset failed before $arm"; exit 1; }
  CELL="cmp_${SALT}_${arm}"
  if [ -n "$HOOK" ]; then
    ~/vamp/gate_cell.sh "$CELL" "vampA=1,vampB=1" 192.168.5.61,localhost -- \
      python ~/vamp/gc_cell.py --turns-a "$TA" --turns-b "$TB" --prefix-words "$PW" --salt "$SALT" --hook "$HOOK"
  else
    ~/vamp/gate_cell.sh "$CELL" "vampA=1,vampB=1" 192.168.5.61,localhost -- \
      python ~/vamp/gc_cell.py --turns-a "$TA" --turns-b "$TB" --prefix-words "$PW" --salt "$SALT"
  fi
  echo "[arm $arm] gate rc=$?"
done
