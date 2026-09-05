# VAMP CXL Migration Handoff

이 디렉터리는 기존 vllm-vamp 저장소 안에서 Claude가 개발을 시작하도록 만든
최소 인계 패키지입니다. JP 접속이나 별도 저장소 clone은 시작 조건이 아닙니다.
기존 루트 README, AGENTS, vLLM 코드 및 다른 branch는 변경하지 않습니다.

## 기준 버전 정정

원격 main/asplos-vamp와 upstream v0.19.0 태그를 전체 SHA로 대조했습니다.
모두 아래 commit을 가리킵니다.

```text
2a69949bdadf0e8942b7a1619b229cb475beef20
```

따라서 이 기준은 정확히 v0.19.0 태그의 commit입니다. 로컬 tag가 없거나
v0.20 deprecation 문구가 있다는 사실만으로 release 이후 main이라고 판단할 수
없습니다. SOURCE_LOCK.json에는 tag 이름뿐 아니라 전체 SHA와 파일 hash를 고정했습니다.

## 읽기 순서

1. 저장소 루트 AGENTS.md와 이 README.
2. [구현 상세 명세](docs/implementation-spec.md).
3. [기존 코드와 hook 위치](docs/code-map.md).
4. [고정된 외부 shared-memory API 계약](docs/fixed-shared-memory-api.md).
5. [Claude 개발 / Codex 검증 절차](docs/collaboration.md).
6. [Read-only capability audit와 최소 backend 변경 제안](docs/capability-audit.md).

## 초기 검증: hash + 테스트 6개

저장소 루트에서 실행합니다. 루트 지침대로 uv로 관리한 .venv를 사용하며,
이미 Python 3.10 이상의 .venv가 있으면 그대로 사용합니다.

```bash
uv venv --python 3.12 .venv
.venv/bin/python -S vamp_handoff/tools/verify_source_lock.py
.venv/bin/python -S -m unittest discover -s vamp_handoff/tests -v
```

기존 .venv에 대해서는 생성 명령을 반복하지 마세요. -S는 sitecustomize와 설치
패키지 startup 영향을 제외합니다. 이 6개 테스트는 stdlib만 사용하고 vLLM,
Dynamo, torch 또는 실제 CXL library를 import하지 않습니다. upstream vLLM 자체의
테스트/build는 별도이며, 그때는 루트 AGENTS의 전체 환경/의존성 지침을 따릅니다.

| 테스트 | 검증 대상 |
| --- | --- |
| 01 | 기존 vLLM hook 26개 + 인계 reference/patch의 총 51개 hash |
| 02 | provider 코드 미포함, 구현/API 고정 경계 |
| 03 | 실제 Dynamo 변경이 args/main/protocol/publisher 네 파일인지 |
| 04 | 현재 vLLM offload hook의 함수와 구문 |
| 05 | 회수한 Dynamo source 구문과 sidecar 존재 |
| 06 | 기존 placement CASS 회귀 시나리오 |

이것은 새 shared-CXL M1-M12 gate가 아닙니다. M1-M12는 아래
"GPU-free 구현 상태"의 `tests/test_offload_mock.py`에 있습니다. hash 검증 실패 시
원인을 먼저 확인하고 테스트만 녹색으로 만들기 위해 SOURCE_LOCK을 재생성하지 않습니다.

## 포함 범위

| 경로 | 역할 |
| --- | --- |
| ../vllm/v1/kv_offload | 기존 repo의 실제 vLLM 코드; 복제하지 않음 |
| source_context/dynamo | backend Python 9개, router Rust 3개, 원본 LICENSE |
| patches/dynamo-solab.patch | solab 설치본의 실제 호환/계측 변경; Dynamo 원본 tree 기준 |
| patches/pristine | 변경된 네 파일의 원본 reference |
| baselines | CASS/router와 pressure/smoke/offload 코드의 historical reference |
| docs/environment-versions.json | 실제 solab-s2 패키지 버전; runtime image가 아님 |

회수한 소스는 .py.ref/.rs.ref/.sh.ref로 보존했습니다. byte-exact한 참고 파일을
실행 모듈/자동 launch 스크립트로 혼동하거나 lint가 원본을 바꾸는 것을 방지합니다.
이 디렉터리의 typos 설정은 reference만 제외하며 새 코드와 문서는 계속 검사합니다.
source lock 검증 후 테스트 06만 기존 pure CASS source를 명시적으로 로드합니다.
실제 구현은 새로운 모듈 또는 기존 vLLM hook에서 하고 reference는 보존하세요.
Dynamo 변경이 필요하면 추가 patch와 대응 테스트를 작성해 Codex에 전달합니다.

## 변경 불가인 외부 의존성

provider의 shared-memory manager/allocator/locks/library 및 API/ABI는 고정입니다.
우리 수정 대상이 아니며 source, header, binary, submodule을 포함하지 않습니다.
우리 adapter가 기존 API를 호출하는 범위만 구현합니다. 안전하게 표현할 수 없는
요구는 blocker로 보고하고 provider 코드/API 수정을 해결책으로 전제하지 않습니다.

## 첫 구현 범위

- 고정 길이 prefix를 재사용하는 per-session multi-turn replay.
- B0 recompute / B1 network copy / B2 eager CXL / P selective-deferred publication.
- GPU→private DRAM은 공통 기반으로 유지하고 private DRAM→CXL 저장 시점 제어.
- fake clock/transport로 lifecycle, no-oracle, baseline 불변성, 오류 cleanup 검증.
- 정확한 target worker, CPU-ready export lease, destination import의 capability audit.

미래 migration 대상이나 다음 turn 시각을 policy에 알려주지 않습니다. 가짜 payload나
sleep을 실제 KV transfer 성능으로 보고하지 않습니다. CXL 관리 코드의 구현 부담을
provider 변경으로 넘기지 않습니다. 기존 synthetic/CRG 결과를 SWE-agent나 실제
LangGraph 실행 결과로 이름만 바꾸지 않습니다.

## Git 협업

Claude는 작업 branch에서 구현하고 commit SHA, 테스트 결과와 미검증 조건을
전달합니다. Codex는 같은 commit을 리뷰한 뒤 승인된 JP/solab 환경에서 검증합니다.
여기에 담긴 과거 launch 명령은 실행 승인으로 해석하지 않습니다.

첫 인계 검증 환경: uv로 생성한 CPython 3.12.13, GPU/CXL 미사용.
hash와 6개 테스트는 이 구조에서 통과했습니다. 실제 migration과 신규 M1-M12는
아직 구현/검증되지 않았습니다. 전체 runtime, GPU wheel, 모델, 실제 trace payload는
이 최소 패키지에 포함하지 않습니다.

## GPU-free 구현 상태 (branch claude/cxl-migration-impl-ndi0p9)

명세 §12의 파일을 `vamp_handoff/vamp_cxl/` 패키지로 구현했습니다. stdlib만 사용하고
vLLM, torch, Dynamo, CXL library를 import하지 않습니다. 기존 vLLM hook 26개와
reference 51개의 hash는 변경하지 않았습니다(`verify_source_lock.py` PASS).

| 파일 | 책임 |
| --- | --- |
| `vamp_cxl/keys.py` | RunKey/RequestKey/ModelKey/PrefixKey/PayloadRef/LeaseId/JobId |
| `vamp_cxl/offload_policy.py` | 순수 policy: B0/B1/B2/P_DELAY/P_VALUE, value score, calibration table, shadow wrapper |
| `vamp_cxl/kv_transfer_adapter.py` | WorkerKVAdapter/SharedKVStore/RoutingAdapter 계약, capability report, fake clock/transport/store/worker, read-priority executor, §6 lifecycle coordinator, OffsetMapper |
| `vamp_cxl/session_workload.py` | manifest 생성, tokenizer/template 주입, LCP 측정, public/ground-truth 분리 |
| `vamp_cxl/session_replay.py` | per-session closed-loop runner, SSE 분류, event-level JSONL trace, target 검증, HTTP backend 골격 |
| `vamp_cxl/simulation.py` | fake backend + coordinator + runner 결합, GPU-free cell 실행 CLI |
| `vamp_cxl/summarize_migration_run.py` | trace join, baseline/fallback 구분, capacity/latency 보고 |
| `configs/*.json` | frozen workload/policy/executor/capacity, mock calibration(라벨 `mock`) |
| `tests/test_offload_mock.py` | M1-M12, 숫자 워크스루, cell smoke, config 고정 검사 |
| `vamp_cxl/vllm_binding.py` | 실제 vLLM v0.19.0 hook 접합: READY/eviction 통지, export lease(mailbox), import 예약, CPU payload bridge, `VampOffloadingSpec` |
| `vamp_cxl/network_transport.py` | B1 host-to-host TCP payload transport (correctness prototype) |
| `vamp_cxl/cxl_shm_binding.py` | 고정 외부 API 위의 shared store, ctypes 바인딩(확인된 signature만), chunk copy transport, EMULATED provider |
| `patches/dynamo-vamp-receipt.patch` | Dynamo `handlers.py`에 worker receipt와 wrong-worker 거부 추가 (미적용 patch) |
| `tests/test_vllm_binding.py` | 실제 `CPUOffloadingManager`에 대한 CPU-only 테스트 (torch 없으면 skip) |
| `tests/test_network_transport.py`, `test_http_backend.py`, `test_cxl_shm_binding.py`, `test_dynamo_patch.py` | loopback socket, 로컬 SSE 서버, emulated provider, patch 적용 검사 |

실행:

```bash
# stdlib only (vLLM binding 테스트 5개는 skip)
.venv/bin/python -S -m unittest discover -s vamp_handoff/tests -v
cd vamp_handoff && ../.venv/bin/python -S -m vamp_cxl.simulation --sessions 32 --seed 0

# vLLM scheduler-side binding까지 실행: torch + requirements/common.txt 설치 후 -S 없이
# (GPU wheel/빌드 불필요; CPU-only import로 CPUOffloadingManager를 실제로 구동)
uv pip install --python .venv/bin/python torch
uv pip install --python .venv/bin/python -r requirements/common.txt
.venv/bin/python -m unittest discover -s vamp_handoff/tests -v
```

구현/검증 구분 표는 [docs/capability-audit.md](docs/capability-audit.md) 하단에 있습니다.

### M1-M12 상태

| Mock | 상태 | 검증한 것 |
| --- | --- | --- |
| M1 Manifest | PASS | 32×8,192 prefix 일치, 세션 격리, MML 이내, owner 16/16, 이동 8/8, ground truth 미노출 |
| M2 Publication | PASS | CPU READY 전과 visibility 전에는 CXL READY/read 불가; DEFER 중 pin 없음 |
| M3 Eviction | PASS | lookup 뒤 eviction → 명시 miss; pinned copy는 eviction 거부; defer 중 eviction → SKIP_SOURCE_EVICTED, stale export 없음 |
| M4 Reader race | PASS | reader lease 동안 EVICTING 유지, slot 재할당 없음; release 후 새 generation으로 재사용 |
| M5 Duplicate/ABA | PASS | 두 번째 writer는 ALREADY_WRITING; 이전 generation completion 거부; coordinator 중복 turn_end 단일 저장 |
| M6 Cancel/error | PASS | write 실패/cancel/read 실패 후 lease/slot/job 원상 복귀; job당 terminal 1개; quiescence 전 해제 없음; silent retry 없음 |
| M7 Policies | PASS | 50 > 38.5 STORE_NOW, read 1개 DEFER_READ_PRESSURE, write=200 SKIP_LOW_VALUE, 5 s 뒤 SKIP_DEFER_TIMEOUT(재시작 없음), stale/uncalibrated/NaN/closed/mismatch |
| M8 Baselines | PASS | shadow P_VALUE 예외 주입 시 B0/B1 결정·target 불변, shadow_failed 표시 |
| M9 No oracle | PASS | 과거 동일·미래 이동만 다른 두 manifest에서 첫 이동 dispatch 전 결정 동일 |
| M10 Capacity | PASS | metadata reserve/rounding 반영 capacity, 초과 거부, slice 밖 offset 거부, origin 미확인 시 device offset 계산 거부, 중복 사본 점유 계산 |
| M11 Runner | PASS | 전역 barrier 없음, role/usage-only chunk는 TTFT 미확정, admission delay 기록, misroute 시 target 미검증 기록 |
| M12 Integration | PASS | out-of-order CPU ready/turn done/result에서 단일 store, consideration ID 추적, 미지 result 무시 |

Mock 통과는 hardware capability 통과가 아닙니다. 32-session STAY/MOVE_GAP cell은
fake 시계로 실행되며 latency는 sequencing용 상수입니다.

### 구현했지만 실장비 검증이 남은 것

- vLLM 접합(`vllm_binding.py`)은 vLLM 소스를 바꾸지 않고 `spec_module_path`로
  끼웁니다. scheduler 측은 실제 manager로 검증했고, worker 측 handler와 engine
  기동은 G-A에서 확인합니다. `SOURCE_LOCK.json`의 `new_cxl_runtime_implemented`는
  실제 CXL 접근이 없으므로 계속 `false`입니다.
- Dynamo patch는 pristine reference에 적용·구문 검사만 했습니다. 설치본 적용은
  Codex가 backup/rollback과 함께 수행합니다.
- `CxlSharedKVStore`는 EMULATED provider로만 검증했습니다. 실제 library는 운영자의
  ABI 확인(`AbiConfirmation`) 전에는 로드/호출되지 않습니다.

### 아직 하지 않은 것 / 할 수 없는 것

- G-A ~ G-H 실장비 gate, 실제 calibration, MOVE_IMMEDIATE/MOVE_CONTENDED 부하.
- provider 확인이 필요한 항목: offset origin, visibility/fence primitive, lock
  handle ABI, crash recovery. 코드는 확인 전 fail closed로 동작합니다.
- scheduler↔worker 간 RPC(export lease 요청을 worker bridge로 전달하는 경로)는
  in-process mailbox까지만 있고 프로세스 간 전달은 미구현입니다.
- 균일 8K trace에서 P_VALUE는 mock calibration으로 전부 STORE_NOW를 냅니다.
  명세대로 정상 결과이며 계수를 바꾸지 않았습니다.
