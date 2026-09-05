# 실장비 Gate 결과 — 2026-09-05 (solab-s1/s2)

collaboration.md §"Codex 실장비 검증" 5·6 항에 따른 기록. 큰 로그는 Git에 넣지 않고
위치만 연결한다. 이 문서의 수치는 전부 실장비 실측이며 mock/emulated 값이 아니다.

## 대응 관계 (검증 commit ↔ 배포 파일)

| 항목 | 값 |
| --- | --- |
| 검증 commit | `49104a098de9e171acaec458b6b5cf62dc21f387` (`claude/cxl-migration-impl-ndi0p9`) |
| 배포 대상 | solab-s1, solab-s2 `~/vamp/vamp_handoff/{vamp_cxl,configs}` (`DEPLOYED_COMMIT.txt` 동일 SHA) |
| 배포 방식 | jpserver worktree `/home/jp/vllm-vamp-cxl` → rsync. 설치본 vLLM/Dynamo 소스는 변경하지 않음 |
| 추가 배포 (이 branch의 `solab/`에 보존) | `vamp_probe.py`(spec_module_path 래퍼+listener 파일 로그), `worker_vamp.sh`, `ga_cell.sh`, `ga_report.py` |
| Dynamo patch | `patches/dynamo-vamp-receipt.patch` **미적용**. 설치본에는 기존 solab 호환 patch(1–6)+patch7(`--connector` default None) 적용 상태 |
| runtime | Dynamo 0.5.0 + vLLM 0.19.0, venv `~/venvs/dynamo05_vllm019`, Python 3.10.12, torch 2.10.0+cu128 |
| 모델/설정 | Qwen/Qwen3-14B bf16, `--gpu-memory-utilization 0.90 --max-model-len 16384 --max-num-seqs 16` |
| GPU KV pool | **97,552 tokens** (A6000 48GB, 유휴 상태 부팅) |
| CPU tier (ON 셀) | `cpu_bytes_to_use`=64 GiB, `block_size_factor`=2, `eviction_policy`=lru → manager 26,214 blocks × 2,621,440 B |
| 토폴로지 | s2 = etcd+nats+frontend(`--router-mode kv`, :8080), s1 = 단일 worker. s2 worker는 내려서 target 단일화 |
| 전송 경로 | GPU↔host DRAM (vLLM `CpuGpuOffloadingHandlers`). CXL·network 미사용 |
| workload | `routing_smoke.py --sessions 1 --turns 6 --prefix-tokens 400` (prompt 4,285 tok, `/no_think`, max_tokens 32, temperature 0), s2 로컬 실행 |
| raw 결과 | s2 `~/vamp/ga/<cell>_{runner,wstats,probe,probe_full}.jsonl`, `<cell>_{start,end}_ts.txt` |

## Cell 절차 (`solab/ga_cell.sh`)

1. warm-up 요청 1건 (cell 밖, 결과 폐기) — 아래 "stale-instance" 때문에 필수
2. etcd `instances/dynamo/backend/generate` 등록 수 == 1 검증 (아니면 ABORT)
3. s1 sidecar/probe 파일 라인 수 기록 → cell 실행 → 라인 수 차이로 cell 창(window) 분리
4. runner JSONL + sidecar 창 + probe 창/전체 스냅샷

## 발견: Dynamo 0.5.0 KV router stale-instance 라우팅

worker 교체(`removed model` 22:17:38 → `added model` 22:18:27) 후 **78초가 지난 첫 요청**을
router가 죽은 `instance_id=7587897347262428735`로 보내 500(`instance_id not found`)을 냈다.
spec §9의 "stale worker registration → cell invalid"가 실제로 발동하는 사례. 첫 OFF 셀
(`ga_off`)은 turn 0이 이 500이라 **invalid 처리**하고 warm-up 절차를 추가해 `ga_off2`로
재실행했다. ON 셀의 warm-up도 같은 500을 흡수했다(정상 동작 확인).

## G-A: 1 session 동일 worker 반복, CPU offload OFF/ON

통과 조건: 반복 local GPU hit 계측 정합, 실제 target/출력/usage 확인.

### ON (`ga_on`, VampOffloadingSpec via `spec_module_path=vamp_probe`)

부팅 probe (EngineCore pid 120552):
```
listener_registered  spec_module=vamp_probe
handlers_registered  bridge_num_blocks=26214 bridge_block_bytes=2621440 bridge_tensors=1
manager_created      manager=VampCPUOffloadingManager num_blocks=26214 offloaded_block_size=16 eviction_policy=lru
```
vLLM 로그 `Creating offloading spec with name: VampOffloadingSpec` 2회(scheduler role + worker role, 같은 pid — uniproc executor).

| turn | latency | prompt_tokens | content |
| --- | --- | --- | --- |
| 0 | 1.592 s | 4285 | SESSION0_TURN0_OK |
| 1–5 | 0.472–0.481 s | 4285 | SESSION0_TURN{1..5}_OK |

sidecar (Σ delta, cell 창 74 records):
- GPU prefix: hits **21,280** / queries **25,710** = **82.8%**
  - 검산: queries = 6 turn × 4,285 tok = 25,710 (정확). hits = 5 turn × 268 blocks × 16 = 21,440 ≈ 21,280 (suffix 경계 반올림). 기대 5/6 재사용과 정합.
- connector: hits 0 / queries **4,430** — turn 0 cold miss 시 CPU tier 조회(당시 비어 있음), 이후 GPU hit이라 미조회. 정합.
- peak_kv_usage 0.044, peak_running 1, peak_waiting 0 (headroom 상태)

probe (cell 창): **READY 통지 정확히 6회** — 268, 2, 2, 2, 2, 2 blocks (prefix 1회 + turn별 새 suffix), 간격 ≈ 1.48 s = turn 주기. evicted 0. `counters={'ready_notifications': 6}`, `active_leases=0`.

### OFF (`ga_off2`, `--connector none`)

| turn | latency | prompt_tokens | content |
| --- | --- | --- | --- |
| 0 | 1.593 s | 4285 | SESSION0_TURN0_OK |
| 1–5 | 0.470–0.476 s | 4285 | SESSION0_TURN{1..5}_OK |

sidecar (cell 창 74 records): GPU prefix hits **21,280** / queries **25,710** = **82.8%**.
connector 통계 없음(connector 없음). peak_kv_usage 0.044, running 1, waiting 0.

### G-A 판정: **PASS**

| 통과 조건 | OFF | ON | 판정 |
| --- | --- | --- | --- |
| 반복 local GPU hit 계측 정합 | 21,280 / 25,710 (82.8%) | 21,280 / 25,710 (82.8%) | **동일**. 기대(5/6 재사용)와 정합 |
| 실제 target | 단일 등록 인스턴스(etcd 검증) | 동일 | 토폴로지로 보장. receipt 기반 검증은 patch 미적용이라 NOT_RUN |
| 출력 | SESSION0_TURN{0..5}_OK | 동일 | 일치 |
| usage | prompt 4285 / completion 9 × 6 | 동일 | 일치 |
| 지연 | turn0 1.593 s, 이후 0.470–0.476 s | turn0 1.592 s, 이후 0.472–0.481 s | headroom에서 offload ON 오버헤드 측정 불가 수준 |

추가로 ON에서만 관측된 것: CPU READY 통지 6회(prefix 268 blocks + turn별 2 blocks), connector cold-miss 조회 4,430 tokens. 즉 **OffloadingConnector 경로가 GPU-local hit 계측을 바꾸지 않으며**, 첫 turn 이후 CPU tier에 prefix 사본이 READY 상태로 존재함이 listener로 확인됐다.

## 다른 gate

| Gate | 상태 | 비고 |
| --- | --- | --- |
| G-B | NOT_RUN | 다음 순서 (8 sessions, worker당 4 고정 — s2 worker 재기동 필요) |
| G-C | NOT_RUN | receipt patch 미적용 상태에서는 "실제 다른 worker 실행" 검증 수단이 sidecar 분포뿐 |
| G-D | NOT_RUN | scheduler↔worker RPC 미구현(in-process mailbox까지) |
| G-E~G-G | NOT_RUN | 실제 CXL 접근 — **별도 안전 승인 전 실행 금지**(manager protocol/offset origin 미확인) |
| G-H | NOT_RUN | |

## 부수 확인

- 2026-09-05 pressure run(32 세션 × 11,816 tok = GPU pool의 1.94배, `CPUOffloadingSpec`)에서 GPU prefix hit이 0으로 나온 것은 **계측 아티팩트가 아니다**. 같은 OffloadingConnector 경로에서 G-A ON이 GPU hit 82.8%를 정상 계측했으므로, pressure run의 0%는 working set 초과에 의한 실제 eviction이다.
