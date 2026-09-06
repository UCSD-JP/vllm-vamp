#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""Real-library check that the binding refuses a provider call from a thread other
than the one that first used it (instead of letting the provider abort the process).
Run on a node without an engine: CXL_SHM_LIBRARY=... python3 cxl_thread_guard_check.py"""
import os
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from vamp_cxl.cxl_shm_binding import CtypesProviderApi  # noqa: E402
from cxl_abi_solab import solab_confirmation  # noqa: E402

lib = os.environ["CXL_SHM_LIBRARY"]
api = CtypesProviderApi(solab_confirmation(lib), library_path=lib)
t = threading.Thread(target=api.connect)
t.start(); t.join()
print("RESULT: connected on a worker thread", flush=True)
try:
    api.lock_alloc()
    print("RESULT: GUARD_MISSING - cross-thread call went through", flush=True)
except RuntimeError as exc:
    print("RESULT: GUARD_OK -", str(exc)[:100], flush=True)
os._exit(0)
