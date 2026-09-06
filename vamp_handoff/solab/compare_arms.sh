#!/bin/bash
# B0/B1/B2 controlled comparison, one session, identical prompt (same --salt) in
# every arm; only the arm's transfer mechanism and the CXL key differ. Each arm
# starts from a cold reset of both workers (arm_reset.sh); the manager keeps running.
#   B0: A turns -> (no transfer, gap 0) -> B turns  = destination local-cold recompute;
#       A's auto-pin is released by a post-hook after the B turns (cleanup gate recorded)
#   B1: A turns -> gd_hook (network KV)  -> B turns
#   B2: A turns -> gf_hook (CXL KV)      -> B turns
# The first failed arm stops the run (logs stay under ~/vamp/ga/cmp_<salt>_<arm>*).
# usage: compare_arms.sh <salt> [arms...]   e.g. compare_arms.sh cmp B0 B1 B2
set -u -o pipefail
SALT="${1:?salt}"; shift
ARMS=("$@"); [ ${#ARMS[@]} -gt 0 ] || ARMS=(B0 B1 B2)
TA=${TURNS_A:-3}; TB=${TURNS_B:-2}; PW=${PREFIX_WORDS:-1000}
export WARMUP_URLS="http://localhost:8080/v1/chat/completions,http://localhost:8081/v1/chat/completions"
for arm in "${ARMS[@]}"; do
  case "$arm" in
    B0) HOOK=""; POST="python ~/vamp/gf_hook.py --cleanup-only";;
    B1) HOOK="python ~/vamp/gd_hook.py"; POST="";;
    B2) HOOK="python ~/vamp/gf_hook.py --key VAMP_KV_${SALT}_b2"; POST="";;
    *) echo "unknown arm $arm"; exit 2;;
  esac
  echo "################ arm $arm (salt $SALT) $(date -u +%FT%TZ) ################"
  if ! ~/vamp/arm_reset.sh | tail -3; then echo "RESET_FAILED before arm $arm"; exit 1; fi
  CELL="cmp_${SALT}_${arm}"
  ~/vamp/gate_cell.sh "$CELL" "vampA=1,vampB=1" 192.168.5.61,localhost -- \
    python ~/vamp/gc_cell.py --turns-a "$TA" --turns-b "$TB" --prefix-words "$PW" --salt "$SALT" --hook "$HOOK" --post-hook "$POST"
  rc=$?
  echo "[arm $arm] gate rc=$rc"
  if [ "$rc" -ne 0 ]; then echo "ARM_FAILED $arm rc=$rc; stopping (logs: ~/vamp/ga/${CELL}*)"; exit "$rc"; fi
done
echo "ALL_ARMS_OK"
