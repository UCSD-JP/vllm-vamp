#!/bin/bash
# B0/B1/B2 controlled comparison, one session, identical prompt (same --salt) in
# every arm and gap; only the arm's transfer mechanism, the CXL key and the gap differ.
# Each cell starts from a cold reset of both workers (arm_reset.sh); the manager keeps running.
#   B0: A turns -> (no transfer) -> B turns at t0+gap   = destination local-cold recompute;
#       A's auto-pin is released by a post-hook after the B turns (cleanup gate recorded)
#   B1: A turns -> gd_hook (network KV) in the background from t0 -> WAIT -> B turns
#   B2: A turns -> gf_hook (CXL KV)     in the background from t0 -> WAIT -> B turns
# Gap protocol: t0 = A's last response complete; the next turn arrives at t0+gap in every
# arm; B1/B2 dispatch only after the hook (cleanup included) succeeded (WAIT policy).
# The first failed cell stops the run (logs stay under ~/vamp/ga/<RUN>_<arm>_g<gap>*).
# usage: RUN=<run_id> GAPS="0 5 10 35" compare_arms.sh <salt> [arms...]
#   e.g. RUN=gap1 GAPS="0 5 10 35" compare_arms.sh cmp B0 B1 B2
set -u -o pipefail
SALT="${1:?salt}"; shift
ARMS=("$@"); [ ${#ARMS[@]} -gt 0 ] || ARMS=(B0 B1 B2)
RUN="${RUN:-cmp_${SALT}}"; GAPS="${GAPS:-0}"
VERIFY="${VERIFY:-sha256}"   # staged CXL arm verification: sha256 (baseline) or none (ablation, reported as skipped)
TA=${TURNS_A:-3}; TB=${TURNS_B:-2}; PW=${PREFIX_WORDS:-1000}
export WARMUP_URLS="http://localhost:8080/v1/chat/completions,http://localhost:8081/v1/chat/completions"
for gap in $GAPS; do
for arm in "${ARMS[@]}"; do
  case "$arm" in
    B0) HOOK=""; POST="python ~/vamp/gf_hook.py --cleanup-only";;
    B1) HOOK="python ~/vamp/gd_hook.py"; POST="";;
    B2) HOOK="python ~/vamp/gf_hook.py --key VAMP_KV_${RUN}_${arm}_g${gap} --verify ${VERIFY}"; POST="";;
    *) echo "unknown arm $arm"; exit 2;;
  esac
  CELL="${RUN}_${arm}_g${gap}"
  if ls ~/vamp/ga/${CELL}_runner.jsonl >/dev/null 2>&1; then echo "cell $CELL already exists; choose another RUN"; exit 3; fi
  echo "################ cell $CELL (salt $SALT, gap ${gap}s) $(date -u +%FT%TZ) ################"
  if ! ~/vamp/arm_reset.sh | tail -3; then echo "RESET_FAILED before $CELL"; exit 1; fi
  ~/vamp/gate_cell.sh "$CELL" "vampA=1,vampB=1" 192.168.5.61,localhost -- \
    python ~/vamp/gc_cell.py --turns-a "$TA" --turns-b "$TB" --prefix-words "$PW" --salt "$SALT" --gap-s "$gap" --hook "$HOOK" --post-hook "$POST"
  rc=$?
  echo "[cell $CELL] gate rc=$rc"
  if [ "$rc" -ne 0 ]; then echo "CELL_FAILED $CELL rc=$rc; stopping (logs: ~/vamp/ga/${CELL}*)"; exit "$rc"; fi
done
done
echo "ALL_CELLS_OK run=$RUN gaps=[$GAPS] arms=[${ARMS[*]}]"
