# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""GPU-free shared-CXL offload scheduling and turn-boundary migration.

Pure policy, fake clock/transport/store, controlled session replay and
analyzer. Nothing here imports vLLM, torch, Dynamo or a CXL library; the
real adapter boundary is declared in :mod:`vamp_cxl.kv_transfer_adapter`
and is bound to the installed engine only in a separately approved gate.
"""
