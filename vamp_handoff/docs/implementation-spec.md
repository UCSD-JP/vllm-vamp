# Shared-CXL Offload Scheduling and Turn-Boundary Migration

작성일: 2026-09-05. 상태: 구현용 상세 명세 v0.1; 결과 또는 novelty 확정 문서가 아니다.

대상: Claude Code 구현 담당자와 VAMP 연구 리뷰어.

> 기존 vllm-vamp handoff 개정: 이 저장소에서는 JP/solab 접속 없이 개발한다.
> 아래 원래 서버 경로는 provenance이며 작업 시작 조건이 아니다.
> provider shared-memory 구현과 API/ABI는 고정된 외부 의존성이다.
> 소스/헤더/바이너리를 포함하거나 수정·재구현하지 않는다.
> 우리 adapter로 해결할 수 없는 기능은 blocker로 보고한다.

## 0. 작업 지시와 권한

이 문서에 따라 먼저 코드와 GPU-free mock을 구현한다. GPU 작업, 서버 재시작, CXL manager 시작 및 실제 CXL write는 사용자에게 해당 자원 사용을 승인받은 뒤 별도 gate로 진행한다. 명세 전달 자체를 실험 launch 승인으로 해석하지 않는다.

- 작업 저장소: 현재 vllm-vamp checkout. JP 접속은 개발의 선행 조건이 아니다.
- 구현 예정 위치: 기존 vllm 모듈의 최소 hook 및 새 workload/policy/adapter 모듈. Dynamo는 handoff reference와 실제 upstream 경로의 별도 patch로 관리한다.
- 원래 서버 경로는 /home/jp/paper_resource/asplos_paper/solab_testbed이며 실장비 배포 provenance로만 사용한다. 인증정보를 문서/코드/로그에 넣지 않는다.
- 루트 AGENTS.md, vamp_handoff/README.md, 이 docs의 code-map.md와 fixed-shared-memory-api.md를 읽는다. baseline reference의 원격 명령과 이전 gate는 자동 실행하지 않는다.
- 오래된 GPU driver 미설치·pool 크기 기록은 최신 boot 로그와 구분한다. 오래된 문서의 다른 실험 우선순위를 이번 작업의 launch 지시로 해석하지 않는다.
- 기존 파일과 미커밋 변경을 보존한다. 기존 CASS/Router 코드를 먼저 살펴 재사용하며 별개의 routing 시스템을 불필요하게 만들지 않는다.
- 명칭은 policy router를 우선한다. synthetic replay를 LangGraph, ADK, SWE-agent 실행 결과로 부르지 않는다.
- 각 작업 전 다음 단계와 이유를 알리고, 관측 사실 / 설계 / 미검증 사항을 구분해 보고한다.

### 안전상 금지

UCSD 영역은 /dev/dax1.0의 device offset [64 GiB, 128 GiB)라고 제공받았다. 실제 mapping과 allocator의 상대 offset 기준은 API/제공자 확인이 필요하다. 하위 영역을 읽거나 쓰지 않는다. slice 전체를 memset하거나 bootstrap/allocator를 임의 초기화하지 않는다.

start_server.sh는 bootstrap 상태를 삭제할 수 있어 실행 금지다. manager startup/clear/connect 절차를 제공자가 승인한 뒤 node 0인 s2에서만 시작한다. 커널 교체, driver 재설치, reboot, 타 사용자 프로세스 종료는 이 작업 범위 밖이다. 정리는 run 소유 PID만 대상으로 한다.

## 1. 연구 목표와 범위

질문: 다음 turn의 실행 위치가 아직 확정되지 않았을 때, 어떤 reusable KV를 언제 shared CXL에 저장해야 향후 migration의 이득이 저장·공간·경합 비용을 넘는가?

두 결정은 구분한다.

1. Turn 종료 시점: shared tier에 지금 저장 / 저장 연기 / 저장 생략.
2. 다음 turn 도착 시점: worker 선택 및 local reuse / network copy / CXL restore / recompute.

v0는 동일 모델이 두 GPU에 상주하는 turn-boundary migration만 다룬다. 진행 중 decode, streaming connection, model weight, optimizer state 이동은 제외한다. 이미 CXL에 있는 KV는 source GPU에서 다시 전송하지 않고 목적지에서 공유 사본을 복원한다.

### v0의 정확한 offload 경계

현재 vLLM의 GPU→private DRAM offload는 계산 진행 중에도 발생한다. v0는 이 경로를 공통 기반으로 유지하고, 완료된 CPU KV의 private DRAM→shared CXL publication 시점을 제어한다. 따라서 v0를 GPU eviction scheduling 또는 모든 GPU→DRAM 전송 시점 최적화라고 주장하지 않는다.

CXL 저장과 source 사본 삭제는 별개 결정이다. v0에서 source private DRAM은 동일한 LRU 계열 규칙을 사용하고, CXL publication 성공만을 이유로 source를 즉시 제거하지 않는다. 사본 중복은 용량과 전송량에 모두 반영한다.

## 2. 확인된 기반과 아직 없는 기능

2026-09-05 코드/로그 리뷰 기준:

| 항목 | 상태 |
| --- | --- |
| Qwen3-14B BF16, A6000 worker 2개 | serving 확인 |
| Dynamo 0.5.0 + vLLM 0.19.0 | 호환 패치가 적용된 설치본; 정확한 freeze/patch hash 보존 필요 |
| GPU KV pool | 최근 97,552 tokens/worker; 매 boot 재확인 |
| CPUOffloadingSpec | private DRAM offload 및 connector hit 관측 |
| 공유 CXL 실제 KV export/import | 미구현/미검증 |
| Network KV migration | 미검증; 사용 가능한 transport와 connector 결합 방식 확인 필요 |
| Policy router의 정확한 worker targeting | migration용 gate 필요 |
| CXL direct GPU DMA | 미검증; staging 경로부터 허용 |

현재 설치본의 검토 대상은 venv 아래 vllm/v1/kv_offload/{abstract.py,cpu/manager.py,worker/cpu_gpu.py}, distributed/kv_transfer/kv_connector/v1/offloading/scheduler.py와 v1/core/sched/scheduler.py다.

- CPU manager는 block hash와 local block ID를 관리한다. local ID를 곧바로 cross-host byte offset으로 해석하면 안 된다.
- CPU tensor는 worker 측 handler가 소유한다. scheduler의 CPU metadata만으로 안전한 payload export가 완성되지 않는다.
- prepare_load/complete_load는 기존 local 수명 관리의 참고점이지 외부 RPC용 thread-safe lease API가 아니다.
- get_computed_blocks가 GPU prefix를 조회하고 별도로 통계를 기록한 뒤 connector를 조회한다. connector 사용만으로 GPU 조회가 사라지는 구조가 아니다.
- connector query 분모는 GPU에서 못 찾은 토큰이다. GPU와 connector hit rate를 단순 합산하지 않는다.
- spec_module_path 확장 훅은 존재하지만, 새 transport와 delayed publication을 무패치로 붙일 수 있다는 보장은 없다.

README의 32-session 기존 실험은 성공 127/128이고 1건은 stale worker registration 관련 오류다. 입력 길이 합 기준 footprint는 약 400,258 tokens였다. free의 shared 수치로 CPU offload 할당량이나 cross-host 공유를 증명하지 않는다.

## 3. 첫 Workload: Controlled Session Replay

실제 agent framework가 아닌 KV 재사용/전송을 통제하는 synthetic workload다. 기존 pressure_run.py를 회귀 기준으로 보존하고 새 runner를 만든다.

| 필드 | 기본값 |
| --- | --- |
| model | Qwen/Qwen3-14B, 동일한 model/tokenizer revision |
| sessions | 32 |
| turns_per_session | 4: turn_id 0, 1, 2, 3 |
| reusable_prefix_tokens | 세션당 정확히 8,192의 검증된 공통 prefix |
| max_output_tokens | 32; temperature 0; /no_think 유지 |
| max_outstanding_requests | 8 |
| initial_owner | 균형 배정: worker별 16 sessions |
| gap_s | 시나리오별 0 또는 10; 이후 transfer calibration에 맞춰 보강 |
| migration_turn | 2; 그 전 turn 0과 1은 최초 owner |
| migrating_sessions | seed로 선택한 각 owner의 8개; 총 16개 |
| after_migration | turn 3도 새 owner 유지 |
| admission | per-session closed-loop; 전체-turn barrier 없음 |

### Token과 prefix 생성

세션별 고유 내용이 공통 header 뒤 너무 늦게 등장하지 않도록 구성한다. 전체 model input을 tokenizer와 실제 chat template로 인코딩하고, 같은 세션의 모든 turn에 대한 longest common prefix를 검증한다. 그중 완전한 KV block 경계에 맞는 8,192 tokens를 대상 prefix로 정한다. 전체 prompt 길이는 suffix/template 때문에 8,192보다 클 수 있다.

세션 ID별 토큰화 차이를 허용하되 목표 재사용 prefix 길이는 실제 측정으로 맞춘다. 단어 수×상수로 환산하지 않는다. 서로 다른 세션 사이의 의도치 않은 공통 prefix 길이도 manifest에 기록한다. 모든 요청은 prompt+출력 여유가 max_model_len 16,384 이내여야 한다.

32×8,192 = 262,144 reusable tokens, 현재 총 GPU pool의 약 1.34배다. 160 KiB/token 환산으로 target payload는 약 40 GiB다. 전체 활성 footprint, full prompt token 합, unique reusable block 합은 서로 다른 값으로 보고한다.

### 도착 시간과 숨겨야 할 정보

next_ready = previous_response_done + configured_gap. 세션 간 동시 진행을 허용한다. concurrency 제한 때문에 못 보낸 시간은 client admission delay로 기록한다. 정책별 응답 시간이 다르면 절대 도착 시간도 달라지는 closed-loop 실험임을 명시한다. 동일 seed와 gap 규칙을 유지하되 동일 wall-clock arrival trace라고 주장하지 않는다.

미래 목적지, migration 대상 목록, 다음 gap, 마지막 turn 번호는 runner 전용 ground truth다. policy observation/config/reason 문자열에 노출하지 않는다. 처음 끝나는 세션에 app-level close를 통지한다면 모든 비교군에 동일하게 통지한다. 미래 정보가 필요한 oracle 분석은 사후 별도 라벨로만 수행한다.

### 시나리오

| ID | 이동 | Gap | 목적 |
| --- | --- | --- | --- |
| STAY | 없음 | 10 s | remote use가 없을 때 shared write 비용 |
| MOVE_IMMEDIATE | turn 2에 절반 이동 | 0 s | 사전 저장 window가 거의 없는 경우 |
| MOVE_GAP | 같은 이동 | 10 s | 사전 publication의 가치 |
| MOVE_CONTENDED | MOVE_GAP과 같음 | 10 s | 별도 승인된 CXL read/write 부하하의 경합 |

첫 두 시나리오는 STAY와 MOVE_GAP이다. MOVE_CONTENDED의 외부 부하는 동일 seed/예산/요청 스케줄과 달성 부하를 기록하며 사용자 승인 영역만 쓴다. 무제한 background stress는 금지한다.

이 균일 workload는 기능과 전송 비용 분리용이다. 이를 통과한 뒤 8 sessions×4,096 + 16×8,192 + 8×12,288 tokens의 혼합 길이 arm을 추가한다. 총 prefix token은 그대로다. 이후 reuse 횟수/간격 다양성을 추가해야 선택적 admission의 이득을 주장할 수 있다.

## 4. Baseline 계약과 용량

첫 캠페인에서는 destination과 migration 시점을 runner가 고정한다. policy는 미래 destination을 볼 수 없으며 실제 요청이 도착하면 현재 destination만 전달받는다.

| ID | Shared publication | 목적지 local miss 처리 |
| --- | --- | --- |
| B0_RECOMPUTE | 없음 | 원격 prefix 재계산 |
| B1_NETWORK | 없음 | source private DRAM에서 실제 network copy; source miss면 재계산 |
| B2_EAGER | turn 종료 및 CPU READY 이후 가능한 빨리 저장 | CXL READY면 복원; 아니면 재계산 |
| P_VALUE | 아래 선택/지연 정책 | B2와 동일한 demand 규칙 |
| P_DELAY | admission은 B2와 동일, write 시작만 지연 | timing 효과 분리용 추가 진단군 |

목적지 GPU/private DRAM에 이미 유효한 local prefix가 있으면 모든 군이 사용한다. 최초 cross-host gate에서는 목적지 prefix가 local-cold임을 검증한다. B0를 약하게 만들기 위해 모든 natural local hit을 막지 않는다.

v0에서 CXL 항목이 WRITING/PENDING이면 demand는 기다리지 않고 recompute한다(wait_budget_ms=0). 전송 중 bytes를 읽지 않는다. queued publication은 취소할 수 있고, 이미 시작한 전송은 completion까지 안전하게 정리한다. CXL군에 암묵적인 network fallback을 섞지 않는다.

B1의 source miss, copy 실패, recompute fallback은 반드시 별도 reason으로 기록한다. 오류 fallback으로 serving이 성공해도 migration gate의 copy 성공으로 세지 않는다. 첫 B1은 CPU staging 기반 host-to-host 경로를 명시한다. 제공되거나 설치 가능한 검증된 transport를 우선 조사한다. 임시 Python/TCP payload 구현은 correctness prototype이지 최종 high-performance network baseline이 아니다. 더 빠른 지원 경로를 임의로 배제하지 않는다.

### 용량 통제

- B0/B1: private CPU payload 합을 shared 구성의 전체 lower-tier payload 예산과 동일하게 맞춘다. 명목 상한은 worker별 64 GiB.
- B2/P: private CPU 명목 상한 worker별 32 GiB + shared CXL 명목 상한 64 GiB.
- CXL metadata/allocator 예약과 block rounding을 제외한 실제 payload capacity를 산출하고 B0/B1의 합을 그 값에 맞춘다. 실제 사용량을 맞추는 것이 아니라 사용 가능한 payload 예산을 맞춘다.
- GPU pool, 모델, block layout, source CPU policy는 고정한다. temporary staging와 in-flight 보호 메모리는 별도 고정 상한으로 계측하며 숨은 cache로 사용하지 않는다.
- 이 설계는 같은 총 lower-tier capacity의 시스템 비교다. 매체만의 효과는 추가로 동일 source 상태에서의 단일 transfer probe로 분리한다.

## 5. 데이터 모델과 코드 수준 경계

아래 이름은 새 프로젝트 인터페이스다. vLLM/TraCT에 이미 존재하는 API라고 가정하지 않는다. Python 3.10 호환으로 구현하고 기존 repo의 타입/테스트 패턴을 우선한다. 시계, transport, metrics provider는 주입한다.

### 식별자

- RunKey = run_id + cell_id + seed. 모든 로그와 namespace에 포함한다.
- RequestKey = RunKey + session_id + turn_id + attempt_id. task identity는 명시 헤더와 trace에 넣는다.
- ModelKey = model revision + tokenizer/template revision + KV dtype + layout/version + TP/PP 구성.
- PrefixKey = ModelKey + tenant/run isolation salt + 실제 engine prefix block hash chain + complete-token count.
- PayloadRef = PrefixKey + allocator instance ID + generation + relative offset + length + layout descriptor.
- LeaseId/JobId = 고유 ID. raw virtual pointer나 local block ID를 다른 host의 식별자로 전달하지 않는다.

Offset은 UCSD slice-relative로 정규화한 wrapper에서 검사한다. device offset = slice_start + relative_offset을 적용하는 층은 정확히 한 곳이다. 제공 API가 이미 slice-relative mapping을 한다면 64 GiB를 두 번 더하지 않는다. 실제 API의 기준을 먼저 확인해야 한다.

### 제안 인터페이스

~~~python
class WorkerKVAdapter:
    def inspect_capabilities(self) -> CapabilityReport: ...
    def acquire_ready_prefix(self, prefix, deadline_ns) -> "LeaseOrMiss": ...
    def release(self, lease_id) -> None: ...
    def export(self, lease_id, transport, destination) -> "JobId": ...
    def import_and_restore(self, request, payload_ref) -> "JobId": ...
    def poll(self) -> "list[TransferEvent]": ...

class SharedKVStore:
    def reserve(self, prefix, nbytes, writer_id) -> "ReservationOrExisting": ...
    def commit_ready(self, reservation, completion_proof) -> None: ...
    def acquire_ready(self, prefix, reader_id) -> "LeaseOrMiss": ...
    def release(self, lease_id) -> None: ...
    def abort(self, reservation, completion_proof) -> None: ...

class OffloadPolicy:
    def on_turn_end(self, event, observation) -> "OffloadDecision": ...
    def on_state_change(self, event, observation) -> "list[OffloadDecision]": ...
    def on_request(self, event, observation) -> "DemandDecision": ...
    def on_transfer_result(self, event) -> None: ...
~~~

OffloadDecision은 STORE_NOW / DEFER / SKIP, DemandDecision은 LOCAL / NETWORK_COPY / CXL_RESTORE / RECOMPUTE와 reason을 가진다. 각 decision에 input snapshot ID, estimate 유효성, 선택 이유를 남긴다. 함수를 호출했다고 작업 완료로 간주하지 않는다. 모든 비동기 작업은 실제 completion event를 기다린다.

RoutingAdapter는 request와 지정 destination을 받아 실제 worker receipt까지 연결해야 한다. downstream router가 다시 목적지를 바꾸지 않도록 구현한다. 정확한 targeting 경로가 확인되기 전에는 worker별 single-target endpoint/namespace를 후보로 검토하되 작동한다고 가정하지 않는다.

Residency는 PrefixKey → 여러 worker/tier의 READY 사본 집합이다. last_executor는 별도 필드다. 마지막 worker 한 곳에만 사본이 있다는 기존 근사 ledger를 이 테스트의 ground truth로 사용하지 않는다.

acquire_ready_prefix는 owner thread/RPC에서 READY 확인과 pin을 원자적으로 수행해야 한다. scheduler metadata와 worker tensor의 lifetime을 연결하는 adapter가 필요하다. async callback에서 기존 manager 내부 dict를 직접 수정하지 않는다. DEFER 중에는 payload lease를 오래 붙잡지 않고 metadata만 보유하며, 전송 시작 직전에 다시 acquire한다.

CapabilityReport 필수 항목: 정확한 target worker 지정, CPU-ready 통지/조회, export lifetime 보호, destination import 지원, engine-compatible hash/layout, 실제 전송 완료 통지, cancellation semantics, shared offset 검사, cross-host visibility primitive. 미지원 항목을 true로 하드코딩하지 않는다.

## 6. Lifecycle와 동기화 불변식

Source CPU READY와 CXL READY는 서로 다른 상태다. turn 종료만으로 source CPU payload가 완성됐다고 가정하지 않는다.

~~~text
CPU pending -> CPU READY -> acquire source lease
CXL ABSENT -> RESERVED -> WRITING -> READY -> EVICTING -> ABSENT
                             | failure/cancel
                             -> ABORTING -> ABSENT (after transfer settles)
~~~

1. STORE_NOW라도 source CPU가 미완료면 WAIT_SOURCE_READY다. turn-end와 CPU-ready 두 조건이 모두 만족해야 CXL 전송을 시작한다.
2. source lease 획득 → CXL reserve → payload write → transfer completion → 제공자 규약의 visibility/fence → READY metadata publication → source lease 해제 순서다.
3. reserve 실패 시 source lease를 즉시 해제한다. local eviction 때문에 재획득 실패하면 SOURCE_EVICTED로 skip한다. 실측 없이 숨은 source pin을 유지하지 않는다.
4. reader는 READY 확인과 generation 검증, reader lease 획득을 같은 보호 범위에서 수행한다. read 전 visibility 조치와 import completion까지 lease를 유지한다.
5. writer/reader가 보호한 slot은 재할당할 수 없다. generation과 allocator ID가 다르면 stale completion/handle을 거부한다.
6. 동일 prefix의 중복 reserve는 단일 writer로 합치거나 명시적인 ALREADY_WRITING/READY를 반환한다. payload write를 중복 실행하지 않는다.
7. network/CXL/DMA 취소는 이미 시작한 장치 접근을 즉시 종료한다고 가정하지 않는다. 완료 또는 확실한 quiescence 이전에 slot/lease를 회수하지 않는다.
8. process crash 복구와 안전한 fencing이 미구현이면 해당 실험을 중단하고 보고한다. 임의 timeout만으로 원격 메모리를 회수하지 않는다.
9. CPU cache flush만으로 GPU DMA coherence를 추정하지 않는다. 실제 CXL mapping·driver·제공 라이브러리 규약을 확인한다. staging을 쓰는 경우 각 hop의 completion/visibility 경계를 검증한다.
10. prefix hash/layout mismatch, checksum mismatch, offset 경계 위반은 즉시 correctness failure다. 성능 cell을 계속 진행하지 않는다.

부분 prefix는 완전한 연속 block까지만 복원한다. suffix와 마지막 미완성 block은 계산한다. decode tail 전체 이전이나 임의 block hole 복원은 v0 범위 밖이다.

## 7. 정책 명세: baseline은 고정하고 실험 정책은 정직하게 표시

### 공통 전송 executor

전경 demand read를 speculative background write보다 우선한다. 이 규칙은 B2와 P에 공통 적용한다. B2를 의도적으로 blocking write/FIFO로 느리게 만들지 않는다. 이미 시작한 DMA를 강제 중단하지 않으며 backend가 지원하는 안전한 chunk 경계에서 스케줄링한다.

최초 기본값: source당 진행 중 publication 1개, testbed 전체 2개. chunk는 한 개의 engine offload block을 기준으로 시작한다(현재 설정이 유지되면 32 tokens, 약 5 MiB). 실제 granularity와 staging memory 상한을 capability report에 기록한다. staging 예산은 host당 64 MiB로 시작하되 backend가 더 필요하면 명시적으로 조정하고 모든 비교군의 해당 경로에 같은 기준을 적용한다.

### B2_EAGER

종료 통지되지 않은 session의 재사용 가능한 prefix를 turn 종료 시 enqueue한다. source가 READY가 아니면 기다리고, 이미 CXL READY/WRITING이면 중복 저장하지 않는다. publication은 응답 critical path에서 동기적으로 완료를 기다리지 않는다. READY source lease는 실제 전송을 시작할 때 획득한다.

### P_DELAY: timing 진단군

B2와 같은 prefix를 고려하되, snapshot이 stale이거나 shared demand read가 진행/대기 중이면 DEFER한다. state-change event 또는 100 ms timer에서 재평가한다. 최초 고려 시점부터 5 s를 넘기면 SKIP_DEFER_TIMEOUT한다. 대기 동안 payload를 pin하지 않는다. 이 정책이 공통 read-priority executor 이상의 이득을 보장한다고 주장하지 않는다.

### P_VALUE: 구현 검증용 가치 점수

이것은 구현과 계측을 검증하는 명시적인 휴리스틱이며 기존 CASS의 검증된 비용식 또는 논문의 최종 정책으로 부르지 않는다. B0/B1/B2 capability와 별도 calibration이 통과한 뒤만 실장비에서 켠다. 아래 값은 mock 이외에는 임의의 가짜 실측값으로 채우지 않는다.

입력 추정값은 같은 모델/경로/크기의 별도 probe에서 얻는다.

- T_net_ms: source CPU READY 이후 network copy를 통해 destination GPU에서 usable KV가 되기까지의 service time.
- T_recompute_ms: 목적지 local-cold prefix prefill 시간. full response latency를 대입하지 않는다.
- T_cxl_read_ms: 이미 CXL READY인 payload가 목적지 GPU에서 usable KV가 되기까지의 service time.
- T_cxl_write_ms: CPU READY부터 shared READY까지의 service time. queue 대기는 따로 기록한다.
- remote_reuse_weight: 초기 고정 0.5. 학습되거나 보정된 확률이 아니라 민감도 분석 가능한 정책 계수다.

~~~text
avoided_remote_service_ms = max(0, min(T_net_ms, T_recompute_ms) - T_cxl_read_ms)
weighted_benefit_ms = remote_reuse_weight * avoided_remote_service_ms
publication_charge_ms = T_cxl_write_ms
eligible = weighted_benefit_ms > (1 + margin) * publication_charge_ms
margin = 0.10
~~~

전체 async write 시간을 차감하는 보수적 ranking surrogate다. 이 점수를 곧바로 expected TTFT 또는 실제 지연 감소량이라고 해석하지 않는다. v0는 공간 가치에 대한 미검증 계수를 추가하지 않고 하드 capacity/lease 제한을 사용한다. queue forecast, eviction probability, online weight tuning은 이번 첫 구현 범위 밖이다.

결정 순서:

1. SESSION_CLOSED이면 SKIP_CLOSED. CXL READY이면 SKIP_ALREADY_READY, WRITING이면 SKIP_ALREADY_WRITING.
2. 요청 prefix와 ModelKey/layout이 맞지 않으면 fail closed.
3. turn 종료부터 경과 시간이 5 s 이상이면 SKIP_DEFER_TIMEOUT한다. 이 검사는 모든 DEFER/WAIT 반환보다 먼저 적용한다.
4. calibration 미비/범위 밖이면 SKIP_UNCALIBRATED. NaN/음수 비용은 invalid configuration으로 기록한다.
5. eligible가 false이면 SKIP_LOW_VALUE.
6. shared snapshot age가 1 s 초과이면 DEFER_STALE; demand read 진행/대기 중이면 DEFER_READ_PRESSURE.
7. source가 CPU READY가 아니면 WAIT_SOURCE_READY. 두 조건이 충족되면 slot과 lease를 재확인해 STORE_NOW.
8. 실패/취소는 §6의 lifecycle로 정리한다. 성공으로 재분류하거나 조용히 retry loop를 만들지 않는다.

위 순서는 timer마다 동일하게 적용한다. timer/state event에서 5 s clock을 새로 시작하지 않는다. 같은 prefix의 후속 turn은 새 consideration ID로 다시 평가할 수 있으나 이전 attempt의 pin/job과 혼동하지 않는다. source eviction, 새로운 request, session 종료, transfer completion 이벤트도 재평가를 유발한다. B0/B1의 결정은 P_VALUE shadow 계산의 예외/지연에 의존하지 않는다. P_DELAY에도 같은 deadline 우선 검사를 적용한다.

### 숫자 워크스루 (mock 전용, 실측 아님)

T_net=180 ms, T_recompute=900 ms, T_cxl_read=80 ms라면 weighted benefit은 0.5×(180-80)=50 ms다.

| 조건 | 판정 |
| --- | --- |
| write=35 ms, snapshot fresh, read 없음 | 50 > 38.5이므로 STORE_NOW |
| 같은 비용, demand read 1개 | DEFER_READ_PRESSURE; source를 pin하지 않음 |
| write=200 ms | 50 < 220이므로 SKIP_LOW_VALUE |
| defer 뒤 source 사본이 퇴출됨 | SKIP_SOURCE_EVICTED; 가짜 restore 금지 |
| 다음 request가 CXL WRITING 중 도착 | RECOMPUTE_NOT_READY; 부분 payload 사용 금지 |

균일 8K trace에서는 P가 거의 전부 저장하거나 거의 전부 생략할 수도 있다. 정상 결과이며 결과를 보고 계수를 바꾸지 않는다. 선택성 주장은 길이/재사용 다양성 arm과 고정된 별도 calibration에서 검증한다.

## 8. GPU-free mock과 assertion

fake clock/transport/store로 같은 policy 코드를 실행한다. mock 성공은 hardware capability 통과가 아니다. 오류를 주입하고, 각 mock 후 lease/slot/job 개수가 원상 복귀하는지 검증한다.

| Mock | 주입/시나리오 | 필수 assertion |
| --- | --- | --- |
| M1 Manifest | session/turn/token 길이, 고정 seed | prefix 일치, 세션 격리, MML 이내, 지정 worker mapping 결정적 |
| M2 Publication | CPU ready 및 visibility 완료를 각각 지연 | 두 조건 이전에는 CXL READY/read 불가 |
| M3 Eviction | lookup 뒤 source eviction, defer 중 eviction | atomic acquire 또는 명시 miss; stale block export 없음 |
| M4 Reader race | reader lease 동안 evict 요청 | reader 완료 전 slot 재사용 없음 |
| M5 Duplicate/ABA | 두 writer, 이전 generation의 늦은 completion | 단일 유효 writer; stale completion이 새 항목을 변경하지 않음 |
| M6 Cancel/error | write/read 도중 timeout/cancel/transport error | quiescence 전 해제 없음; job마다 terminal event 정확히 1개 |
| M7 Policies | 위 숫자와 stale/read-pressure 케이스 | reason/score/시간 경계 정확; 5 s 이후 무한 defer 없음 |
| M8 Baselines | shadow P 코드에 exception 주입 | B0/B1 target/action 불변 |
| M9 No oracle | 과거는 같고 미래 이동/gap만 다른 두 manifest | 같은 관측 history까지 policy decision 동일 |
| M10 Capacity | metadata reserve, rounding, 겹치는 lease | 예산 초과/다른 slice 접근 거부; 중복 사본도 점유로 계산 |
| M11 Runner | 느린 세션, role-only SSE, usage-only SSE | 전역 barrier 없음; 실제 token 전 TTFT 확정 금지; client delay 보존 |
| M12 Integration events | out-of-order CPU ready/turn done/result | request 연관 유지, 중복 admission/store 없음, 모든 decision 추적 가능 |

## 9. 실장비 gate와 중단 조건

GPU-free 구현 완료 시 mock 결과와 capability gap을 먼저 보고한다. 아래 gate 실행에는 사용자 자원 승인이 필요하다. 실장비 연결 불가 상태에서는 hardware gate를 pending으로 두고 source/mock 구현을 진행한다. provider 코드나 API를 수정해 gate를 우회하지 않는다.

| Gate | 내용 | 통과 조건 |
| --- | --- | --- |
| G-A | 1 session 동일 worker 반복, CPU offload OFF/ON | 반복 local GPU hit 계측 정합, 실제 target/출력/usage 확인 |
| G-B | 8 sessions, 각 worker 4개 고정 | headroom에서 GPU/connector 카운터 해석 가능; 오류 없음 |
| G-C | B0 강제 turn-boundary 이동 | 목적지 local-cold, 실제 다른 worker 실행, recompute 확인 |
| G-D | B1 한 세션 실제 KV network migration | source/destination checksum 및 prefix/layout 일치, import+restore 완료 |
| G-E | CXL 작은 payload의 cross-host 안전 검증 | startup 승인, offset 경계, lock/visibility, generation 검증 |
| G-F | B2 한 세션 실제 CXL KV migration | local-cold 목적지에서 shared READY prefix 복원 및 결과 정합 |
| G-G | 요청 없는 gap에서 publication/defer | decision 시점과 실제 DRAM→CXL bytes 시작/완료가 일치 |
| G-H | P_VALUE 고정 calibration | 실제 경로별 측정값과 frozen config, no-oracle mock 통과 |

G-E는 GPU-free일 수 있어도 실제 CXL 접근 gate이므로 자동 실행하지 않는다. 데이터가 shared CXL이 아닌 tmpfs/network dummy backend라면 별도 emulated 라벨이며 G-F 통과로 세지 않는다.

checksum/offset/잘못된 generation/잘못된 worker 실행은 즉시 중단한다. stale worker registration, 누락된 usage/transfer event, cross-cell cache 오염은 해당 cell invalid 처리한다. CXL miss에 따른 정상 recompute와 infrastructure failure를 구분한다. fallback으로 결과를 숨기지 않는다.

G-D나 G-F가 막히면 다른 GPU-free 작업은 진행하되 해당 baseline과 최종 비교를 완료라고 쓰지 않는다. 첫 launch에 필요한 시간은 import/compile/registration 확인 전 약속하지 않는다.

## 10. Cell matrix와 calibration

### 별도 calibration

모델과 경로를 고정하고 4K/8K/12K prefix의 recompute, CPU→GPU, network→GPU, CPU→CXL, CXL→GPU를 독립적으로 측정한다. no-queue probe에서 service time을 구하고 queue wait를 별도 보존한다. 전송 시작 전 대기나 후처리를 CUDA event duration에 포함된 것으로 착각하지 않는다. NUMA placement, CPU affinity, NIC/link 설정, staging 여부를 기록한다.

실제 사용 가능한 경로만 calibration table에 넣는다. 네트워크 RTT에서 payload bandwidth를 추정하지 않는다. interpolate는 측정한 크기 범위 안에서만 허용하고 interpolation 방법을 고정한다. calibration 결과를 본 cells에 적용한 뒤 자동 수정하지 않는다.

### 최초 full-prefix cells

| Cell family | Workload | 비교군 | 목적 |
| --- | --- | --- | --- |
| F-STAY | STAY | B0/B1/B2/P_VALUE | 4 cells; remote benefit 없는 조건 |
| F-GAP | MOVE_GAP | B0/B1/B2/P_VALUE | 4 cells; 사전 publication 조건 |
| D-DELAY | MOVE_GAP | P_DELAY | timing-only 추가 진단 |
| E-IMMEDIATE | MOVE_IMMEDIATE | 동일 4군 | 첫 8 cells 통과 후 |
| E-CONTENDED | MOVE_CONTENDED | 동일 4군 + 필요 시 P_DELAY | 실제 부하 계측과 승인 후 |

첫 8 cells는 기능/관측용 1회이며 최종 성능 통계가 아니다. 유효한 cell에 대해 최소 3 paired seeds와 실행 순서 균형화를 적용한다. 32-session 규모의 소수 request로 안정적인 p99/일반화 이득을 주장하지 않는다. 초기에는 raw sample count와 p50/p95를 중심으로 보고한다.

cell마다 fresh run_id/namespace, router state, CPU/GPU/shared-cache의 해당 run state를 분리한다. 전체 CXL allocator를 clear하지 않고 승인된 run 소유 entry만 정리한다. private caches의 cold 상태는 owned worker 재시작 또는 검증된 reset으로 만든다. 새로운 hash salt만으로 점유 상태까지 cold가 되었다고 가정하지 않는다.

## 11. Trace와 분석 명세

중앙 request trace + worker KV lifecycle + transfer trace + sidecar를 RunKey/RequestKey/PrefixKey/JobId로 연결한다. JSONL은 event 단위로 누적 저장하고 종료 시에만 쓰는 기존 방식은 피한다. flush 정책과 계측 overhead는 모든 정책에 동일하게 적용한다.

### 관측값과 추정값

| 종류 | 필드 예 |
| --- | --- |
| 관측 | actual_worker_id, input_tokens, generated_tokens, gpu_hit_tokens, connector_hit_tokens, actual_transfer_bytes |
| 수명 | cpu_ready, cxl_state, generation, active_leases, resident_copies |
| 시각 | ready/dispatch/first_token/response_done, turn_done, enqueue/start/complete/visible |
| 추정 | estimated_recompute_ms, estimated_network_ms, estimated_cxl_read_ms, estimated_cxl_write_ms |
| 정책 계수 | remote_reuse_weight, margin, defer_budget_ms |
| 판정 | action, reason, snapshot_id, calibration_id, fallback_reason |

monotonic duration은 같은 clock domain 안에서만 뺀다. host 간 monotonic timestamp를 직접 빼지 않는다. cross-host timeline에는 wall-clock sync 상태/불확실성 또는 coordinator 관측 시각을 함께 둔다.

### 핵심 지표

- TTFT: client dispatch부터 첫 실제 generated token까지. role/usage-only SSE는 token이 아니다. reasoning/content 첫 token을 각각 보존하고 primary 정의를 고정한다.
- Client admission delay: request ready부터 dispatch까지. end-to-end completion은 별도 기록한다.
- Migration: 지정 destination 변경 / 실제 worker 변경 / 외부 KV 재사용 성공을 각각 집계한다. min_cost 같은 reason 수를 migration 수로 대신하지 않는다.
- Prefix reuse: GPU, destination private DRAM, CXL, network import의 실제 경로와 hit tokens를 구분한다. pooled connector 총 hit 하나로 tier attribution을 대신하지 않는다.
- Transfer: enqueue wait, source wait, service, visibility completion, bytes, direction, source/destination tier. GPU→DRAM 공통 비용도 기록한다.
- Publication: 고려/선택/시작/READY/취소/실패 수, READY 전 다음 turn 도착 비율, observation horizon 내 미사용 bytes.
- Capacity: configured payload capacity, 실제 allocator 점유, source/target duplicate bytes, pinned/staging high-watermark.
- Correctness: HTTP/engine 오류, output 검증, import 실패, fallback, checksum mismatch, dangling lease/slot.

사전 write를 TTFT 밖에서 수행했다고 비용 0으로 처리하지 않는다. 반대로 모든 write 시간의 합을 foreground latency에 무조건 더하지도 않는다. latency, bytes, overlap, 공유 자원 점유를 함께 보고한다. 관측 종료까지 사용되지 않은 payload는 unreused_within_horizon이며 영구적으로 불필요했다는 뜻이 아니다.

correctness gate에서 실제 source payload와 import된 payload checksum을 비교한다. 비트 동일성 검증을 위해 필요한 추가 GPU copy는 성능 cell과 분리하거나 모든 군에 동일 적용한다. 생성 결과의 marker 검증만으로 KV 복원 정합성이 증명됐다고 하지 않는다.

## 12. 파일 구성과 구현 순서

기존 repo 패턴을 확인한 뒤 다음 정도의 작은 모듈로 시작한다. 동등한 기존 구현이 있으면 중복 생성하지 않는다.

| 예정 파일 | 책임 |
| --- | --- |
| session_workload.py | manifest 생성, tokenizer/LCP 검증, 시나리오 ground truth |
| session_replay.py | per-session runner, streaming, request trace, target 검증 |
| offload_policy.py | pure policy, 상태/결정 타입, fake-clock 실행 |
| kv_transfer_adapter.py | worker/store/transport 계약과 실제 adapter 경계 |
| test_offload_mock.py | M1~M12와 숫자 예제 |
| summarize_migration_run.py | 계측 join, baseline/fallback 구분, capacity/latency 보고 |
| configs/ | frozen model/policy/workload configs와 calibration 참조 |

큰 generic framework나 새 distributed runtime부터 만들지 않는다. worker thread/RPC 접합 및 transport-specific 코드는 규모가 커질 때만 파일을 분리한다.

구현 순서:

1. Read-only capability audit. 기존 TraCT 전달물, network transport, CPU export/import hook, exact targeting의 사용 가능/부족 항목을 보고한다.
2. Manifest/runner/pure policy와 M1~M12, analyzer를 GPU-free로 구현한다. tokenizer가 로컬에 없으면 임의 다운로드 대신 asset 위치를 확인한다.
3. 기존 코드의 최소 adapter와 B0 경로를 연결한다. API 부족으로 설치본 패치가 필요하면 변경 범위와 rollback 방법을 먼저 명시한다.
4. 사용자 승인 후 G-A~G-D; network copy completion을 실제 관측한다.
5. 별도 CXL 안전 승인 후 G-E~G-G; CXL adapter를 기존 API/TraCT 기능에 접합한다.
6. 독립 calibration과 G-H 후 첫 8 cells. P_VALUE가 실패/무이득이어도 결과를 그대로 보고하고 자동 튜닝하지 않는다.
7. 결과 리뷰 후 heterogeneous trace, contention, 실제 routing 결합을 확장한다.

최초 구현 보고에는 파일 diff, mock별 결과, capability matrix, 아직 실행하지 않은 gate, 다음 launch에 필요한 승인만 포함한다. 모델 변경/패키지 업그레이드/driver 작업으로 scope를 넓히지 않는다.

## 13. SWE-bench 확장과 claim boundary

synthetic 경로가 안정화되면 기존 SWE-bench agent trajectory의 실제 prompt를 같은 runner에 연결한다. trace 출처, 모델 입력으로 렌더링한 token 길이, prefix LCP, tool gap 출처를 검증한다. 현재 MML에 맞지 않는 trajectory를 조용히 truncate하지 않는다. 길이별 subset과 제외 사유를 보고하거나 별도 장비/config arm으로 분리한다.

원래 tool 시간 정보가 없으면 controlled-gap replay라고 표기한다. 기록된 prompt를 replay한 결과는 live agent 실행이나 SWE-bench task 해결률이 아니다. replay-generated 응답을 원본 trajectory의 성공 판단으로 쓰지 않는다.

shared KV storage, migration, read-prioritized transfer 자체를 novelty로 주장하지 않는다. B0/B1 대비 개선은 데이터 경로의 가치이고, B2 대비 개선과 비용 분석이 있어야 selective publication의 가치를 논의할 수 있다. 최종 연구 가설은 후속 routing 결합과 선행연구 검토까지 남아 있다.

## 14. Claude Code에 전달할 시작 프롬프트

~~~text
이 명세를 읽고 shared-CXL offload/migration의 GPU-free 구현부터 진행하세요.
먼저 로컬 AGENTS.md, README.md, code-map과 fixed-shared-memory-api 문서를 읽고
capability audit 결과를 보고하세요. JP 접속을 시작 조건으로 삼지 마세요.
기존 pressure runner는 보존하고 provider 구현과 API는 수정하지 마세요.
Manifest, per-session replay, pure policy, mock, analyzer를 먼저 구현하고
M1~M12 결과와 실제 backend에 필요한 최소 변경을 제시하세요.
가짜 KV 전송이나 sleep을 실제 network/CXL 성능으로 보고하지 마세요.
미래 migration/gap 정보는 policy 입력에서 격리하세요.
GPU launch, worker restart, CXL manager 시작/초기화, 실제 CXL write는
별도 사용자 승인 전 수행하지 마세요. 패키지/커널/driver를 임의 변경하지 마세요.
작업 전 다음 단계와 이유를 설명하고 기존 사용자 변경과 결과를 보존하세요.
~~~
