# Claude Development and Codex Validation

## 역할

| 담당 | 범위 |
| --- | --- |
| Claude Code | 기존 작업 branch에서 구현, GPU-free 테스트, commit 인계 |
| Codex | 정확한 diff 리뷰, 로컬 재검증, 승인된 JP/solab 실장비 검증 |
| 사용자 | 범위와 GPU/CXL 자원 사용 승인 |
| Provider | 고정된 외부 구현 공급; 이 연구에서 수정하지 않음 |

handoff branch를 claude/cxl-migration-impl-ndi0p9에 merge한 뒤 README의
hash 검증과 6개 테스트부터 실행합니다. JP SSH 불가는 구현의 blocker가 아닙니다.
프로젝트 root AGENTS와 기존 작업 내용을 보존합니다.

## Claude 인계 항목

```text
branch / commit SHA / base SHA:
변경 파일과 동작:
vLLM hook 또는 Dynamo patch 변경:
GPU-free 테스트 명령과 결과:
새 M1-M12의 구현/통과/미구현 구분:
의도적인 기존 source hash 변경과 근거:
미검증 API/transport/실장비 가정:
Codex에 요청하는 다음 gate:
```

SOURCE_LOCK은 최초 baseline을 기록합니다. 새로운 코드가 기존 hook을 의도적으로
바꾸면 원본 대비 drift를 설명하고 대응 테스트를 제출합니다. lock 재생성으로
변경을 숨기지 않습니다. build_source_lock.py는 최초 포장용 도구이며 개발 시작
조건이 아니고 이미 생성된 lock을 덮어쓰지 않습니다.

## Codex 실장비 검증

1. commit diff에서 수명 보호, no-oracle 입력, baseline 통제, 부작용을 리뷰합니다.
2. 해당 commit의 source/mock 테스트를 재실행합니다.
3. 필요한 자원 승인을 받고 runtime import 경로, 버전, 적용 source hash를 확인합니다.
4. 승인한 최소 patch만 backup/rollback을 확보해 적용합니다.
5. run_id, commit, model/config, KV pool, 실제 전송 경로와 raw 결과 위치를 기록합니다.
6. gate별 PASS/FAIL/BLOCKED/NOT_RUN과 정상 cache miss/오류 fallback을 구분해 보고합니다.

새 Git checkout만으로 현재 실행 중인 vLLM/Dynamo가 변경되는 것은 아닙니다.
검증 commit과 실제 배포 파일의 대응을 남깁니다. 큰 로그/가중치/인증정보는 Git에
넣지 않고 작은 결과 manifest만 연결합니다. manager 시작/clear, 실제 CXL 접근은
일반 GPU 사용 승인과 별개로 안전 절차가 필요합니다.
