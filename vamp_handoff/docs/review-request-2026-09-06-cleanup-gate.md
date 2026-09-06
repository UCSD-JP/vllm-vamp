# Codex 리뷰 요청 — 실패 주입·정리 게이트 결과와 이슈 (2026-09-06)

대상 커밋: `bf589151e`(agent 스레드 친화성 수정) → `dd347096f`(hook 정리 경로·arm 드라이버) + 미커밋 `gd_hook.py`(B1 정리 게이트, 배포만·미실행).
이전 리뷰(807795231 기준)에서 남긴 항목 중 **(1) 실패 전파, (2) controller-serialized cleanup, 실패 주입 wrong_key + checksum, 목적지 auto-pin off**는 실장비에서 닫았고, **B0/B1/B2 비교와 gap 실험은 아직 실행 전**입니다. 아래 §3의 이슈 6건에 대한 판단을 요청합니다.

## 1. 발생한 사고 — 첫 실패 주입 셀에서 A EngineCore crash

`fail_wrongkey`(첫 시도): B prepare는 주입대로 실패("key … not found"), B cleanup 정상. 그 다음 **A `cleanup`(export scope) RPC가 ~480 s 무응답 후 빈 응답 → A agent 연결 거부 → s1 worker 로그에 "EngineCore died unexpectedly"**.

원인(확정, 2단계 증거):
1. worker 로그의 native stack: `cxl_shm_free_lock → _free_lock → _acquire_lock` 직전에 provider가 `[-1:-1:-1] [_acquire_lock] lock bit: 0` → `lock timeout` → abort. 직전 호출들은 모두 `[1: 9:136571]`(nid 1, rid 9, pid) 로 찍힘.
2. 격리 재현(엔진 밖 Python): 같은 스레드에서 shmalloc/payload_alloc/lock_alloc/put → lock_free/destroy/payload_free/shfree 는 **전부 정상**. 스레드 A에서 connect+alloc, **스레드 B에서 lock_free** → `[-1:-1:-1] global_lock_acquire req bit must be cleared` + core dump.

즉 provider의 rank identity `my_id`가 `__thread`(TLS)이고, agent의 control server가 **RPC마다 새 스레드**를 만들기 때문에 export(스레드 1)와 cleanup(스레드 2)이 다른 id로 보였습니다. G-F(gf2)가 통과한 것은 B의 후속 단계(`get_ptr/refresh/read`)가 TLS를 안 보는 호출이었기 때문 — 우연이었습니다.

수정(`bf589151e`): agent에 `ThreadPoolExecutor(max_workers=1, thread_name_prefix="vamp-cxl")` 하나를 두고 `cxl_export / cxl_import_prepare / cxl_import_status / cleanup` 를 그 스레드로 직렬화. 다른 스레드에서 `_cxl_api()`를 부르면 native abort 대신 Python `RuntimeError`. 기록: `provider-api-findings-2026-09-06.md` §4.

## 2. 실패 주입 게이트 결과 (수정 후, 양 worker cold start)

| 셀 | 주입 지점 | 관측 | 정리 | 판정 |
|---|---|---|---|---|
| `fail_wrongkey2` | B `cxl_import_prepare`에 존재하지 않는 key | A export 801 blocks 2,099,773,440 B 완료 → B "key VAMP_KV_fk2_MISSING not found" | B cleanup(import idle) → **A cleanup: lock_free+destroy+payload_free+shfree 완료**(`cxl_freed` 기록), lease release 게시 → nudge → A/B leases 0, candidate 없음 | **CLEANUP_GATE ok=true**, hook 4.1 s, 셀은 의도대로 `CELL_INVALID`(B 턴 생략) |
| `fail_checksum2` | B reservation 이후 기대 digest를 0으로 오염 | B `reserve_posted` → nudge → reserved 864 → refresh 0.176 s → sha256 2.547 s **불일치 검출** → abort 게시(`import_aborted`) | nudge로 abort 드레인 → B cleanup(failed→idle) → A cleanup(`cxl_freed` 2,264,924,160 B + hashes 27,648 B) → leases 0 | **CLEANUP_GATE ok=true**, hook 5.7 s, `CELL_INVALID` |

세부 로그: `~/vamp/ga/fail_wrongkey2_runner.log`, `fail_checksum2_runner.log`, s1/s2 `probe_*.jsonl`(`cleanup`, `cxl_import_failed` 이벤트). 실패 전파: hook rc≠0 → gc_cell `cell_invalid` 기록·B 턴 생략·rc 1 → gate_cell `CELL_INVALID`.

첫 `fail_checksum` 시도는 **hook 버그**로 무효: 상태 조회 한 번에 reserved→refresh→sha256→failed 까지 진행되므로 `wait_stage("reserved")` 호출 자체가 실패를 관측했는데 그 줄이 try 블록 밖이라 정리 없이 exit 3. 수정 후(`dd347096f`) 재실행 = 위 표. 무효 시도로 남은 상태(B abort 미드레인, A export+lease)는 새로 넣은 `gf_hook.py --cleanup-only`로 회수(게이트 ok).

## 3. 리뷰 요청 이슈

1. **스레드 친화성 대책의 충분성.** 지금은 agent 내부 4개 명령만 전용 스레드로 보냅니다. `AgentListener.on_blocks_ready`(스케줄러 스레드)는 provider를 부르지 않으므로 현재는 안전하지만, 향후 "도착 즉시 export" 같은 훅이 스케줄러 스레드에서 provider를 부르면 같은 crash가 재발합니다. 규칙을 "프로세스당 provider 스레드 1개, 모든 호출은 `_cxl_run` 경유"로 코드 리뷰 체크리스트에 올리는 것으로 충분한지, 아니면 바인딩(`CtypesProviderApi`) 레벨에서 스레드 검사를 강제해야 하는지.
2. **lock timeout = 480 s 동안 EngineCore가 멈춤.** provider의 lock 대기는 abort로 끝나며, 그 동안 A는 serving을 못 했습니다(agent RPC가 EngineCore 프로세스 안에서 돌기 때문). 실험용 시스템 범위에서는 "다른 스레드 호출 금지"로 충분하다고 보지만, cleanup을 EngineCore 밖(별 프로세스)으로 빼야 한다고 보시는지.
3. **payload arena가 free 후에도 재사용되지 않는 것처럼 보임.** payload_off 추이: gf 2.28 GiB → gf2 4.08 → fk 6.65(crash로 미해제) → fk2 8.75(해제) → ck(해제) → ck2 **12.95 GiB**. free한 영역이 있어도 새 할당이 항상 위로 올라갑니다(bump 성향 또는 같은 크기 free-list 미재사용). 유효 63 GiB에서 2.2 GB 전송을 반복하면 ~25회 뒤 고갈 가능. 비교 arm 전에 `cxl_fresh_start.sh`(manager 재기동+`cxl_clear_ucsd`)로 arena를 초기화할지 — "arm 간 manager 재기동 금지" 규칙과 충돌하므로 **arm 시작 전 1회만** 하는 안을 제안합니다. 현재 누수: fk 2.1 GB(crash) + gf/gf2 4.4 GB(정리 게이트 도입 전) = 약 6.5 GB.
4. **cold reset 절차의 함정.** `pkill -f dynamo.vllm`만 하면 `VLLM::EngineCore` 고아가 GPU 44 GB와 agent 포트를 계속 잡아 다음 기동이 engine init에서 실패합니다(s2에서 1회 발생). `worker_stop.sh`(EngineCore 포함 kill + GPU free 대기)와 `arm_reset.sh`(양 노드 stop→launch→agent 응답 대기, manager 무접촉)를 추가했습니다. arm 간 cold reset 정의를 "worker 프로세스 재기동(GPU/CPU tier 초기화), manager·etcd·nats·frontend 유지"로 두는 것이 맞는지.
5. **B0/B1/B2 비교 설계 확인 (`compare_arms.sh`, 미실행).** 동일 `--salt`(동일 토큰), 1 세션, A 3턴 → hook → B 2턴, arm마다 cold reset, 바뀌는 것은 전송 메커니즘과 CXL key만. B0는 hook 없음(gap 0) → B 첫 턴 = local-cold 재계산. **질문**: B0에 B1/B2의 hook 시간(≈7 s)과 같은 인위적 gap을 주는 arm(B0-gap)도 넣어야 "gap 자체의 영향"을 분리할 수 있는데, 1차 비교에는 B0(gap 0)만으로 갈지. 또 warm-up은 두 endpoint 모두에 넣었습니다(B 첫 요청 JIT 편향 제거) — 허용되는지.
6. **B1 hook에 정리 게이트 추가(미실행).** `gd_hook.py`에 gf_hook과 같은 `cleanup_and_verify`(B cleanup import → A cleanup export[CXL 객체 없음, lease release] → nudge → 검증)와 실패 시 정리 경로를 넣었습니다. 네트워크 경로엔 checksum 주입 옵션이 없는데, B1도 동일 주입 매트릭스가 필요하다고 보시는지(현재 판단: CXL 경로만 공유 자원이므로 B1은 정상 경로의 정리 게이트만).

## 3b. Codex 리뷰(99d9915ea) 반영 상태 — `941fb3c32`…

| 지적 | 반영 | 검증 |
|---|---|---|
| B1 cleanup이 네트워크 상태 미정리 | `_cleanup(import)`가 network reservation/stage/payload 버퍼 초기화 + receiver 큐 드레인; commit/abort 미드레인·진행 중이면 거부(`import_abort` 명령 추가) | `b1_gate`: committed→idle, 버퍼 2.26 GB 폐기; `fail_transfer`: failed→idle, 부분 버퍼 2.10 GB 폐기 |
| CLEANUP_GATE 거짓 성공 | strict gate 11개 검사(cleanup ok ×2, lease, candidate, cxl_export_held, net/cxl stage idle, payload 버퍼, 큐) → `failed_checks` 출력 | 4셀 모두 `failed_checks: []` |
| destroy 실패 무시 | 실패 시 payload/hashes 해제 안 함, 포인터 residual 반환, `ok=false` → hook rc 6 → 셀 실패 | 코드 경로(실장비 재현 불가 — provider destroy 실패 조건 없음) |
| compare_arms 계속 진행 | 첫 실패 arm에서 nonzero 종료, reset 실패도 중단 | 스크립트 |
| ① 바인딩 thread ID 검사 | `CtypesProviderApi._fn`이 첫 호출 스레드를 기억, 다른 스레드는 `RuntimeError` | s1 실장비 `cxl_thread_guard_check.py` → `GUARD_OK` |
| ④ cold reset 정의 | `arm_reset.sh`: worker+EngineCore만 재기동, agent+bridge 응답·etcd 인스턴스 1/1·양 endpoint 실요청 확인 후 `RESET_OK` | 1회 통과 |
| ⑥ B1 reservation 이후 전송 실패 1건 | `gd_hook --inject transfer`(sender error marker at chunk 3) | `fail_transfer` gate ok |
| B0의 A pin 정리 | `gc_cell --post-hook "gf_hook --cleanup-only"` (B 턴 후 A pin 반납 + gate 기록) | arm 실행에서 확인 |
| 준비/정리 시간 분리 | hook JSON `transfer_s` / `cleanup_s` | b1_gate 29.9 / 0.22 s |
| ③ 묶음 전 fresh start 1회 | 양 worker 정지 → `cxl_fresh_start.sh` → nid 0·lock thread 확인 → arms | 실행 중 |

## 4. 부수 사항

- 표현 정정 반영: agent 주석 "zero-copy view" → "CXL 매핑 view, `import_payload`가 CPU tier로 복사".
- git 실수 1건: `git push origin HEAD`가 원격에 잉여 브랜치 `cxl-migration-impl`을 만들어 즉시 삭제, tracked 브랜치 `claude/cxl-migration-impl-ndi0p9`로 재푸시. 원격 히스토리 변경 없음.
- 카운터 의미: agent `counters`는 프로세스 수명 누적(예: checksum2의 `import_aborted: 2` = 무효 시도 1 + 재실행 1). 셀 단위 값은 gate_cell 윈도 스냅샷 차이로 읽어야 함.
- 현재 노드 상태: A/B worker 기동 중(A pid 138294, B pid 229372, 둘 다 새 agent), leases 0, CXL manager pid 221454 상주. 비교 arm은 §3-3/5 판단 후 `~/vamp/compare_arms.sh cmp B0 B1 B2`로 실행 가능.
