#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
# Exceptional full re-initialization of the UCSD CXL slice, used only when the arena was
# initialized by a node 0 that no longer runs (our manager then attaches as nid!=0 and no
# lock thread exists anywhere). Steps: stop our manager -> cxl_clear_ucsd ([64,128) GiB zero)
# -> start_server.sh via the checked wrapper -> verify nid 0, lock thread, "empty arena".
set -u
export PATH="$HOME/work/bin:$PATH" LD_LIBRARY_PATH="$HOME/vamp/cxl/lib:$HOME/work/lib"  # our dir first: libcxl_shm.so -> node-specific provider build (N0 on s2, N1 on s1)
LOGD="$HOME/vamp/cxl"; mkdir -p "$LOGD"; OUT="$LOGD/fresh_start_$(date -u +%Y%m%dT%H%M%SZ).log"
{
echo "== $(date -u +%FT%TZ) stop existing manager (ours, nid!=0) =="
for p in $(pgrep -f '^(\S*/)?start_cxl_manager$|start_server\.sh'); do echo "kill $p $(tr '\0' ' ' < /proc/$p/cmdline | cut -c1-60)"; kill "$p" 2>/dev/null; done
sleep 2; pgrep -f '^(\S*/)?start_cxl_manager$' && { echo "still alive, SIGKILL"; pkill -9 -f '^(\S*/)?start_cxl_manager$'; sleep 1; }
rm -f /dev/shm/cxl_shm_bootstrap_ucsd && echo "local bootstrap removed"
echo "== $(date -u +%FT%TZ) cxl_clear_ucsd (our slice [64,128) GiB) =="
t0=$(date +%s); cxl_clear_ucsd; rc=$?; echo "clear rc=$rc elapsed=$(( $(date +%s) - t0 ))s"
[ $rc -eq 0 ] || { echo "CLEAR_FAILED"; exit 2; }
echo "== $(date -u +%FT%TZ) start manager =="
bash "$HOME/vamp/cxl/cxl_manager_start.sh"; rc=$?
P=$(pgrep -f '^(\S*/)?start_cxl_manager$' | head -1)
echo "manager pid=$P threads=$([ -n "$P" ] && ls /proc/$P/task | wc -l)"
[ -n "$P" ] && for t in $(ls /proc/$P/task); do echo "  tid $t comm=$(cat /proc/$P/task/$t/comm)"; done
L=$(ls -t $LOGD/manager_*.log | head -1); echo "== manager log $L =="; cat "$L"
echo "FRESH_START_DONE rc=$rc"
} > "$OUT" 2>&1
echo "$OUT"
