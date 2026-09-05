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

이것은 새 shared-CXL M1-M12 gate가 아닙니다. 새 runner/policy/adapter/mock은
상세 명세에 따라 이제 구현할 대상입니다. hash 검증 실패 시 원인을 먼저 확인하고
테스트만 녹색으로 만들기 위해 SOURCE_LOCK을 재생성하지 않습니다.

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
