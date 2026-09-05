# 리뷰 가이드 — 실장비 G-A 커밋 (2026-09-05)

대상: Codex 또는 사람 리뷰어. collaboration.md §"Codex 실장비 검증" 1–2항(diff 리뷰, 재검증)을
이 커밋에 적용할 때 볼 곳과 확인 항목을 정리한다.

## 리뷰 대상 커밋

| 커밋 | 내용 | 리뷰 초점 |
| --- | --- | --- |
| `c33f083f7` | 최소 handoff 패키지 (문서, source lock, reference) | 이미 검토됨 |
| `f289a6256` | GPU-free policy/adapter/replay/M1–M12 mock | 이미 검토됨 |
| `49104a098` | GPU-free로 구현한 backend: `vllm_binding.py`, `network_transport.py`, `cxl_shm_binding.py`, receipt patch | **미리뷰** — 아래 §3 |
| `fa9e85bef` | **실장비 G-A 결과 + 배포 산출물** (`solab/`, `docs/gate-results-2026-09-05.md`) | **이번 리뷰** — 아래 §2 |

빠른 확인:
```bash
git log --oneline 2a69949..claude/cxl-migration-impl-ndi0p9     # 4 commits, base = upstream v0.19.0
git show --stat fa9e85bef
python -S vamp_handoff/tools/verify_source_lock.py               # PASS, 51 entries 유지
python -S -m unittest discover -s vamp_handoff/tests            # 49 (torch 없으면 5 skip)
```

## 1. 이 커밋이 주장하는 것 / 주장하지 않는 것

주장하는 것:
- `VampOffloadingSpec`이 **vLLM 소스 무수정으로** 실제 v0.19.0 엔진(EngineCore)에서 `spec_module_path`로 로드되고,
  manager·handler·bridge가 생성되며, CPU READY 통지가 store 완료 시점에 실제로 발화한다.
- offload ON/OFF에서 **GPU-local prefix hit 계측이 동일**하다(21,280/25,710). 즉 OffloadingConnector 경로가
  기존 계측을 왜곡하지 않는다.

주장하지 않는 것:
- CPU→GPU **restore 경로**는 G-A에서 발동하지 않았다(working set 4.3K ≪ pool 97.5K, eviction 0). restore는
  별도 pressure run(`CPUOffloadingSpec`, connector hit ~50%)에서만 관측됐고 `VampOffloadingSpec`로는 아직 아니다.
- **2-GPU routing은 G-A 범위 밖**이다. 명세대로 단일 worker(s2 worker 내림, etcd 인스턴스 1개 검증)로 돌렸다.
- export lease / import reservation / bridge gather는 **엔진 안에서 호출되지 않았다** — 부팅·등록만 확인. 호출 경로는
  G-D(RPC)에서.
- CXL·network 전송 없음. receipt patch 미적용.

## 2. `fa9e85bef` 파일별 리뷰 포인트

### `solab/vamp_probe.py` (spec_module_path 래퍼)
- import 시점에 `register_listener(FileListener())` — 엔진이 spec을 만들기 **전에** 등록되는지. (실측: `listener_registered`가
  `Creating offloading spec` 로그와 같은 초 단위에 선행)
- `VampOffloadingSpec` **서브클래스**로 이름을 유지해 `spec_name` 매칭. `get_manager`/`get_handlers`를 감싸 생성 로그만 추가하고
  동작은 바꾸지 않는다 — super() 호출 순서 확인.
- listener는 **scheduler thread에서 호출**되며 파일 append + lock. 운영용이 아닌 probe이므로 latency 영향은 G-B(headroom)에서 관찰 대상.
- registry가 process-global이라 **uniproc executor에서 spec이 2회 생성**(scheduler role + worker role)되어도 manager/bridge가 각각
  덮어쓰기로 등록된다. multiproc executor(TP>1)에서는 worker 프로세스에 listener만 있고 manager는 없다 — 이 가정이 문서화됐는지.

### `solab/worker_vamp.sh` (런처)
- `PYTHONPATH=~/vamp/vamp_handoff:~/vamp/probe` 로 `vamp_cxl`과 `vamp_probe`를 노출. 설치본 site-packages 미변경.
- `--kv-transfer-config`만 사용(`--connector` 미지정). 설치본에는 patch7(`--connector` default None)이 필요하며,
  `patches/dynamo-solab.patch`에 이미 포함되어 있다(§4.1에서 확인).
- `cpu_bytes_to_use=64GiB`, `block_size_factor=2`, `eviction_policy=lru`.

### `solab/ga_cell.sh` (cell 절차)
- warm-up 요청은 cell 밖(결과 폐기) — stale-instance 500을 cell에서 격리하기 위함. **"오류를 숨긴다"로 읽힐 수 있으니**
  gate-results에 warm-up 결과(500 발생 여부)를 기록한 것을 확인.
- etcd 인스턴스 수 == 1 검증 실패 시 ABORT.
- sidecar/probe **라인 수 기준 창 분리**: 동일 파일에 append되므로 cell 시작 라인 수를 기록해 tail로 잘라낸다. 부팅 이벤트는
  `_probe_full`에만 있다.

### `solab/ga_report.py`
- sidecar `hits`/`queries`는 per-update **delta → Σ**. cumulative로 읽으면 안 된다(Track 2 교훈 유지).
- connector hits/queries가 None이면 "connector off"로 표기.

### `docs/gate-results-2026-09-05.md`
- collaboration.md 5항(run_id/commit/model·config/KV pool/전송 경로/raw 위치)과 6항(gate별 상태, 정상 miss vs 오류 fallback 구분)
  충족 여부. 특히 **첫 OFF 셀 invalid 처리 사유**가 숨겨지지 않고 기록됐는지.

## 3. `49104a098` 리뷰 시 함께 볼 것 (실측이 뒷받침하는 부분만)

- `VampCPUOffloadingManager.complete_store` → `listener.on_blocks_ready(newly_ready)`: 실측 6회, block 수(268, 2×5)가
  prefix/suffix 구조와 정합 — **READY 판정 로직(`not block.is_ready` → READY 전이)이 실제 store 완료와 일치**함을 뒷받침.
- `prepare_store` → `on_blocks_evicted`: 실측 0회(tier 여유). eviction 통지는 **미검증**.
- `get_handlers`가 `cpu_to_gpu_handler.src_tensors`로 bridge 생성: 실측 bridge 1 tensor, 26,214 blocks, 2,621,440 B/block —
  `cpu_bytes_to_use / (KV/token × 16)` = 64GiB / 2.5MiB 와 일치.
- mailbox drain(`lookup`/`take_events`)과 lease/import API는 **엔진 안에서 호출된 적 없음** — 단위 테스트 근거만.

## 4. 리뷰어가 확인해야 할 미해결 항목

1. ~~`patches/dynamo-solab.patch`에 patch7 포함 여부~~ → **확인됨(2026-09-05): 포함.** `args.py` hunk에
   `-        default=["nixl"],` / `+        default=None,` 존재(7 hunks, args/main/protocol/publisher). 설치본과 drift 없음.
2. `vllm_binding.py` docstring은 "scheduler process"/"worker process"를 구분해 쓰지만, **uniproc executor에서는 같은
   프로세스에서 spec이 2회 생성**된다는 점(실측: 같은 pid 120552에서 manager_created와 handlers_registered 모두 발생)과
   TP>1(multiproc)에서 worker 프로세스에는 manager가 없다는 점은 언급하지 않는다. 동작 버그는 아니고 **문서 공백** —
   `current_manager()`/`current_bridge()`가 어느 프로세스에서 유효한지 한 문장 추가 권장.
3. ~~G-B 이후: "worker당 4개 고정" 수단~~ → **G-B 실행 결과(같은 날 후속 커밋)**: stock KV router는 affinity를 만들지 않는다
   (순차 48요청 매핑에서 7/8 세션이 worker 이동, router-side cached blocks 항상 0). 통과 조건(카운터 해석·무오류)은 PASS,
   "고정" 전제는 BLOCKED. 해제에는 `patches/dynamo-vamp-receipt.patch` 적용 + pinning 경로가 필요하며 이는 별도 승인 항목.
   또한 G-B 1차는 restore 셀과 prefix 생성기를 공유해 **cache 오염으로 invalid** 처리됨 — `solab/pressure_run.py --salt`와
   cell 전 worker 재기동이 절차에 추가됐다. 리뷰 시 `gate-results` §G-B의 invalid 사유 기록이 숨김 없이 남았는지 확인.

## 5. 재현 (solab, GPU 자원 승인 필요)

```bash
# s1
TAG=s1 PORT=6880 CPU_GB=64 setsid bash ~/vamp/worker_vamp.sh </dev/null >/dev/null 2>&1 &
# s2 (frontend/etcd/nats 기동 상태에서)
bash ~/vamp/ga_cell.sh ga_on 6 400
python3 ~/vamp/ga_report.py --cell ga_on --runner ~/vamp/ga/ga_on_runner.jsonl \
  --wstats ~/vamp/ga/ga_on_wstats.jsonl --probe ~/vamp/ga/ga_on_probe_full.jsonl
```
raw 결과: jpserver `asplos_paper/solab_testbed/results/ga_2026-09-05/` (17 files, 80 KB).

## 6. 리뷰 반영 (Codex 2026-09-05 → 후속 커밋)

리뷰 분류 기준(현재 실험을 막는 문제 / 비교를 왜곡하는 문제 / 나중에 해도 되는 개선)에 따라 처리했다.
범위는 **실험용 시스템**(모델 1, GPU 2, migration 세션 1, 목적지 고정, metadata 관리 주체 1, 전송 한 건씩, 고정 용량, cancellation 미지원·오류 시 중단)이다.

| 지적 | 확인 | 처리 | 검증 |
| --- | --- | --- | --- |
| mailbox: 취소된 Future 실행 → lease 누수 + `InvalidStateError`가 scheduler step으로 전파 | **재현** (실제 `CPUOffloadingManager`, s1) | `_drain_mailbox`가 `set_running_or_notify_cancel()` 실패 시 명령을 건너뜀(`mailbox_cancelled` 카운터) | `test_cancelled_mailbox_command_is_skipped_without_leak` |
| mailbox: 요청 없는 gap에서 drain 정지 → 사전 publication 불가 | 코드상 확인(drain이 lookup/take_events에만 있음) | ① listener가 `on_blocks_ready`(owner thread) 안에서 `acquire_export_lease`를 **동기 호출**해 pin을 확보하는 경로를 공식화 ② `prepare_store`/`complete_store` 끝에서도 drain | `test_listener_acquires_lease_synchronously_in_ready_callback` |
| trace: finish 시점에 `response_done`을 써서 뒤따르는 usage가 저장본에서 빠짐 | 코드상 확인 + **실측**: Dynamo 0.5.0은 usage를 모든 chunk에 실어 보냄(usage-only chunk 없음) | ① runner가 **어떤 chunk에 실린 usage든** 기록 ② HTTP backend가 finish chunk를 보류하고 stream 종료 시 finish 시각을 가진 단일 DONE을 방출(trailing usage가 먼저 처리됨) | `test_usage_is_in_the_written_response_done_for_both_stream_styles` — **저장된 이벤트**를 검사(openai/dynamo 두 스타일) |
| CXL: fence 실패가 DONE으로 남음 | 코드상 확인(COMPLETED 발행 후 fence, 예외 삼켜짐) | fence를 **COMPLETED 이전**에 실행, 실패 시 FAILED(proof 없음) | `test_fence_failure_is_failed_not_done` |
| CXL: read 전 refresh/flush 경로 없음 | 코드상 확인(의도된 fail-closed였으나 read 쪽엔 규약이 없었음) | `refresh` callable 도입; 없으면 read job FAILED(emulated provider만 예외) | `test_read_without_refresh_fails_closed_on_non_emulated_provider` |
| shared-store: 용량이 인스턴스별 → 예산 초과 예약 | **재현**(2-block 예산에 3 block) | **단일 관리자 모드**: 디렉토리 레코드에 owner 기록, 다른 인스턴스는 `read_only` → `REJECTED_READ_ONLY` | `test_second_instance_is_read_only_no_double_accounting` |
| shared-store: 다른 인스턴스의 READY 항목 읽기 거부 | 코드상 확인(`FOREIGN_ENTRY_LOCK_UNAVAILABLE`, 의도된 fail-closed) | **미해결·설계 방향만**: 단일 관리자가 (offset, nbytes, generation, checksum)을 우리 제어 채널로 전달하고 reader는 lock 없이 generation/checksum 검증 후 읽기, 관리자는 reader 완료 전 slot을 해제하지 않음. G-F 착수 시 구현 | — |
| G-B "고정" 미충족; receipt ≠ targeting | 동의 | 상태를 "headroom 계측 진단 완료 / 고정 배치 미검증"으로 정정; targeting은 단일-worker endpoint 또는 experiment router로 | gate-results §G-B |
| 보고 문구(36/36, 13배, affinity 없음) | 동의 | gate-results·solab README·메모리 문구 정정 | — |

미검증으로 남는 것: 실제 CXL library에서의 fence/refresh 동작(G-E), 단일 관리자 cross-host read 경로(G-F), export lease → bridge gather → network 전송 → destination import의 end-to-end(G-D).
