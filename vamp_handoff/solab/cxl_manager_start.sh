#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
# Start the provider CXL manager on node 0 (solab-s2) ONCE per CXL experiment batch.
# Preconditions are checked; completion is judged from the manager's own log lines,
# not from a fixed sleep. The provider script/binaries are not modified.
set -u
LOGD="$HOME/vamp/cxl"; mkdir -p "$LOGD"
LOG="$LOGD/manager_$(date -u +%Y%m%dT%H%M%SZ).log"
export PATH="$HOME/work/bin:$PATH" LD_LIBRARY_PATH="$HOME/vamp/cxl/lib:$HOME/work/lib"  # our dir first: libcxl_shm.so -> node-specific provider build (N0 on s2, N1 on s1)

# comm is truncated to 15 chars, so match the full command line anchored on the executable name
if pgrep -f '^(\S*/)?start_cxl_manager$' >/dev/null; then
  echo "REFUSE: start_cxl_manager already running (pid $(pgrep -f '^(\S*/)?start_cxl_manager$' | head -1)); restarting would re-initialize the slice"; exit 3
fi
if pgrep -af "libcxl|cxl_ping|vamp_cxl_agent" 2>/dev/null | grep -v pgrep | grep -q .; then
  echo "REFUSE: CXL user processes present:"; pgrep -af "libcxl|cxl_ping|vamp_cxl_agent" | grep -v pgrep; exit 3
fi
[ -e /dev/shm/cxl_shm_bootstrap_ucsd ] && echo "note: stale local bootstrap shm present (start_server.sh removes it)"
echo "starting manager, log=$LOG"
# whole background group detached from our stdio, otherwise the caller (ssh) hangs while the manager lives
( cd "$HOME/work/bin" && exec setsid bash -c "./start_server.sh > '$LOG' 2>&1" ) > /dev/null 2>&1 < /dev/null &
for i in $(seq 1 120); do
  sleep 1
  if grep -q "DONE and Waiting forever" "$LOG" 2>/dev/null; then break; fi
  if ! pgrep -f '^(\S*/)?start_cxl_manager$' >/dev/null && [ $i -gt 3 ]; then echo "FAIL: manager exited"; cat "$LOG"; exit 2; fi
done
grep -q "DONE and Waiting forever" "$LOG" || { echo "FAIL: no completion line after 120 s"; tail -20 "$LOG"; exit 2; }
echo "--- manager log (init summary) ---"
grep -E "initialized|already set|arena|Heap|hash initialized|DONE|empty" "$LOG" | head -20
echo "--- state ---"
echo "pid=$(pgrep -f '^(\S*/)?start_cxl_manager$' | head -1) bootstrap=$(ls /dev/shm/cxl_shm_bootstrap_ucsd 2>/dev/null || echo missing)"
echo "MANAGER_READY log=$LOG"
