# Code Map

경로는 vllm-vamp 저장소 루트 기준입니다. vLLM은 기존 checkout을 직접 참조하고
Dynamo의 필요한 일부 코드만 reference로 제공합니다.

## 기존 vLLM 코드

| 파일 | 확인할 책임 |
| --- | --- |
| vllm/v1/kv_offload/factory.py | 외부 spec_module_path hook; delayed publication API 자체는 아님 |
| vllm/v1/kv_offload/abstract.py | lookup 및 prepare/complete load/store |
| vllm/v1/kv_offload/cpu/manager.py | local CPU hash→block metadata, READY, refcounts |
| vllm/v1/kv_offload/worker/cpu_gpu.py | 실제 CPU tensor, GPU transfer, async completion |
| vllm/v1/kv_offload/mediums.py | local block ID/layout; cross-host byte offset과 구분 |
| vllm/distributed/kv_transfer/kv_connector/v1/offloading/scheduler.py | prefix matching, per-step store, request lifecycle |
| vllm/v1/core/kv_cache_manager.py | GPU-local lookup 및 통계 |
| vllm/v1/core/sched/scheduler.py | local lookup 후 connector lookup, 할당/복원 연결 |

가장 먼저 볼 경계는 CPU manager metadata와 worker tensor lifetime의 연결입니다.
turn 종료가 모든 GPU→CPU 전송 완료를 뜻하지 않으며, manager의 내부 dict를
임의 callback thread에서 수정하면 안 됩니다. 새 export/import bridge는 원자적인
READY 확인과 pin, 실제 completion, 오류 시 lease 반환을 보장해야 합니다.

## Dynamo Reference

vamp_handoff/source_context/dynamo/vllm 아래 .py.ref는 upstream v0.5.0에
solab의 실제 변경을 적용한 source입니다. args/main/protocol/publisher 네 파일의
변경은 patches/dynamo-solab.patch와 pristine reference로 분리했습니다.

| 파일 | 용도 |
| --- | --- |
| args.py.ref | connector 기본값과 vLLM argument drift |
| main.py.ref | AsyncLLM 초기화와 worker integration |
| protocol.py.ref | 요청/응답 타입과 metadata |
| publisher.py.ref | sidecar와 GPU/connector 통계 |
| handlers.py.ref | 실제 요청 처리 및 engine 접합 |
| kv_router/scheduler.rs.ref | native worker 선택과 비용 |
| kv_router/indexer.rs.ref | router prefix-state index |
| kv_router/sequence.rs.ref | worker registration/lifecycle |

이 reference subset은 설치 가능한 Dynamo 전체 패키지가 아닙니다. 신규 Dynamo
변경은 원본 경로를 대상으로 별도 patch로 전달하고 Codex가 정확한 설치본에 적용해
검증합니다. 기존 네 파일 patch는 재적용이 필요한 지시가 아니라 이미 검증된
baseline 상태의 기록입니다. 실제 runtime에는 임의로 중복 적용하지 않습니다.

## Baseline의 한계

- 기존 CASS는 placement-only 근사 ledger이며 모든 host/tier 사본을 추적하지 않습니다.
- 기존 pressure runner는 전체-turn barrier와 대략적인 token 계산을 사용합니다.
- 기존 지연은 full-response 시간이며 streaming TTFT가 아닙니다.
- 기존 sidecar는 exception을 숨길 수 있어 신규 trace의 성공 gate로 그대로 쓰면 안 됩니다.
- 기존 worker launcher는 고정 경로/log를 사용합니다. 새 run은 PID/namespace/log 소유권을 분리합니다.

provider shared-memory 구현/API는 고정된 외부 의존성입니다. 여기서 추가하는
application metadata/lease/trace adapter와 provider manager/allocator 수정은
명확히 구분합니다. provider 구현과 header를 이 repo에 추가하지 않습니다.
