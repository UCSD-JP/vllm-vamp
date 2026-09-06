#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
# Single-namespace frontend = fixed endpoint for exactly the workers in that namespace.
# usage: NS=vampA PORT=8080 frontend_ns.sh
source "$HOME/venvs/dynamo05_vllm019/bin/activate"
export ETCD_ENDPOINTS=http://192.168.5.62:2379 NATS_SERVER=nats://192.168.5.62:4222
NS="${NS:?}"; PORT="${PORT:?}"; LOGD="$HOME/vamp/logs"; mkdir -p "$LOGD"
exec python -m dynamo.frontend --router-mode "${ROUTER_MODE:-round-robin}" --namespace "$NS" --http-port "$PORT" > "$LOGD/frontend_${NS}.log" 2>&1
