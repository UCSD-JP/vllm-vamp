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

## Restore 셀 (`restore`, G-A 보강 — VampOffloadingSpec로 CPU→GPU 복원 경로 실발동)

G-A에서 restore가 0이었던 이유: vLLM OffloadingConnector는 **eager write-through**(`_get_reqs_to_store`가 매 step 새 block을
CPU tier로 복사)라 store는 GPU 압력과 무관하게 발동하지만, restore는 **GPU miss 시에만** CPU tier를 조회한다. G-A는
working set 4.3K ≪ pool 97.5K라 GPU가 항상 hit → 조회 자체가 없었다. 그래서 working set을 pool 위로 올렸다.

설정: s1 단일 worker(VampOffloadingSpec, CPU tier 64GiB), **12 세션 × 3 turn, prompt 11,816 tok, concurrency 4**
→ working set **143,828 tok(turn 0 prompt_tokens 실합; 두 세션은 12,820) = GPU pool의 1.47배** (≈8,990 blocks > 6,097 GPU blocks).

| turn | ok | p50 | p95 | max |
| --- | --- | --- | --- | --- |
| 0 | 12/12 | 13.92 s | 16.04 s | 19.68 s |
| 1 | 12/12 | **1.06 s** | 1.14 s | 1.14 s |
| 2 | 12/12 | **1.08 s** | 1.13 s | 1.13 s |

sidecar (cell 창 197 records):
- GPU prefix: hits **0** / queries 431,484 — round-robin turn 순서에서 각 세션 prefix가 재사용 전에 11개 다른 세션에 밀려 전부 eviction. 정합.
- **connector: hits 287,040 / queries 431,484 = 66.5%** — 3 turn 중 turn 1·2가 CPU tier에서 복원(기대 66.7%). **restore 경로 실발동 확정.**
- peak_kv_usage 0.506 (= 동시 4 × 11.8K / 97.5K; vLLM `kv_cache_usage`는 running 할당만 세고 cached-free 블록은 free로 집계), peak_waiting 4.

probe: READY **36회**(12 세션 × 3 turn), block 수 분포 {739: 10, 801: 1, 802: 1, 2: 22, 1: 2} — turn 0은 prefix 전체(11,816/16 = 739; 두 세션은 12,820 tok → 801/802), turn 1–2는 새 suffix 1–2 blocks. CPU tier eviction 0(9,039 blocks × 2.5 MiB ≈ 22 GB < 64 GiB).

지연 관측: cold round p50 13.9 s, reuse(restore) round p50 1.06 s; 참고로 G-A의 GPU-hit round는 0.48 s. 이는 **concurrency 4에서의 응답 지연 차이**(큐 대기, suffix prefill 포함)이며 통제된 성능 비교나 service time이 아니다 — "restore가 N배 빠르다"나 "복사 비용 X초"로 환원하지 않는다. READY 통지 36회는 store 이벤트 수이며 restore 횟수가 아니다(restore는 connector hit 합계로만 관측). calibration은 §10의 no-queue probe로 별도 측정한다.

## G-B: 8 sessions, 각 worker 4개 고정 (headroom)

토폴로지: s1 + s2 각 1 worker(VampOffloadingSpec, CPU tier 64GiB), frontend KV router. 8 세션 × 6 turn, prompt ≈4,325 tok,
**concurrency 1(순차)** — router 결정을 요청과 1:1 매핑하기 위함. working set 34,600 tok ≪ 2×97,552 (headroom).
"worker당 4개 **고정**"을 강제할 수단은 이 스택에 없으므로(receipt patch 미적용, per-request pinning 없음) 분배는 router에 맡기고 사후 매핑으로 관측했다.

### 1차 (`gb`) — **INVALID: cross-cell cache 오염**

48/48 ok, 매핑 유효(48 = 48). 그러나 s1 sidecar에 **connector hit 21,520 / 22,033 (97.7%)**, turn 0에 0.48–0.52 s GPU-hit 3건 —
첫 방문 세션이 tier에 있을 수 없다. 원인: restore 셀과 같은 prefix 생성기(`s{sid}-item{i}`, 370단어는 1000단어의 접두어)라
s1의 GPU(최근 8세션 분)와 CPU tier(전부)에 restore 셀 잔존 캐시가 있었다. spec §9 "cross-cell cache 오염 → cell invalid" 적용.
raw는 `gb_*`로 보존(숨기지 않음). 조치: `pressure_run.py --salt <cell>`(prefix에 cell 태그 혼입) + cell 전 양 worker 재기동으로 tier 초기화 → `gb2`.

### 캐시와 무관하게 유효한 발견 — router affinity 부재

`solab/gb_map.py`(순차 요청 ↔ frontend `Selected worker` 1:1):

```
session 0: W0 W0 W1 W1 W0 W0  MOVED      session 4: W1 W1 W0 W0 W1 W1  MOVED
session 1: W1 W1 W0 W1 W0 W1  MOVED      session 5: W1 W0 W0 W0 W0 W1  MOVED
session 2: W0 W1 W1 W1 W1 W0  MOVED      session 6: W1 W0 W1 W0 W0 W1  MOVED
session 3: W1 W1 W1 W1 W0 W0  MOVED      session 7: W0 W1 W1 W1 W0 W1  MOVED
sessions that changed worker: 8/8 · per-worker distinct sessions: W0=8, W1=8 · router cached blocks by turn: all 0
```

**이 배치에서는 Dynamo 0.5.0 KV router의 overlap 신호와 안정적인 affinity를 관측하지 못했다**(router-side `cached blocks`가 항상 0 →
logit이 두 worker에 동일 → load/tie-break로 교대한 것으로 보인다; KV event가 router에 도달하지 않는 원인은 미조사). 따라서 이 배치의
stock router로는 "worker당 4개 고정"이 성립하지 않는다. receipt patch는 **실행 worker를 확인·거부하는 기능**이고 지정 worker로 보내는
기능이 아니므로, 고정에는 별도 targeting 수단(검증된 단일-worker endpoint 또는 experiment router)이 필요하다. 앞선 routing smoke의 대칭 hit(49.6/49.7%)을 affinity로
읽은 것은 오독이었다 — headroom에서 양 worker가 모든 prefix를 결국 캐시한 결과.

### 2차 (`gb2`, `--salt gb2` + 양 worker 재기동으로 tier 초기화) — 유효

48/48 ok, prompt ≈5,072 tok(salt로 길어짐), working set 40,576 ≪ 195,104. warm-up이 stale-instance 500을 다시 흡수(기록, 제외).

매핑(`gb_map.py`): **7/8 세션 이동**(session 6만 우연히 sticky), W0(…8820)=s1이 7세션·27요청, W1(…8824)=s2가 8세션·21요청, router `cached blocks` 전 turn 0.

| turn | p50 | 분포 |
| --- | --- | --- |
| 0 | 1.75 s | 8건 전부 cold (1.73–1.81) |
| 1 | 1.74 s | 4건 0.48–0.50 / 4건 1.74–1.77 |
| 2 | 0.49 s | 7건 0.48–0.50 / 1건 1.76 |
| 3 | 0.50 s | 6건 0.48–0.50 / 2건 1.75–1.78 |
| 4–5 | 0.49 s | 16건 전부 0.48–0.50 |

**turn>0의 느린 요청 7건 전부가 "그 세션이 그 worker를 처음 방문"한 요청**이고(gb_map 교차), 그 외는 전부 GPU hit.
cold 총 15건 = 서로 다른 (세션, worker) 쌍 15개 (W0 7 + W1 8) — 정확히 일치.

worker별 sidecar (cell 창):

| worker | 요청 | cold(첫 방문) | GPU prefix hit | 기대 (요청−cold)/요청 | connector | READY 통지 | ready blocks |
| --- | --- | --- | --- | --- | --- | --- | --- |
| s1 (W0) | 27 | 7 | **100,960 / 136,991 = 73.7%** | 20/27 = 74.1% | 0 / 36,031 | 27 | 2,249 (≈7×317 + suffix) |
| s2 (W1) | 21 | 8 | **65,616 / 106,543 = 61.6%** | 13/21 = 61.9% | 0 / 40,927 | 21 | 2,556 (≈8×317 + suffix) |

connector queries 합 76,958 ≈ 15 cold × 5,072 tok(cold 시 빈 CPU tier 조회) → hit 0 정상(첫 방문이라 그 worker의 CPU tier에 없음).
restore 미발생 정상(headroom, GPU eviction 없음). peak_kv_usage 0.052, waiting 0, 오류 0.

### G-B 판정: 통과 조건 **PASS**, 전제 조건 **BLOCKED**

- 통과 조건 "headroom에서 GPU/connector 카운터 해석 가능; 오류 없음": **PASS** — 모든 카운터가 (세션, worker) 첫 방문 모델로
  검산 일치, 오류 0.
- 전제 "각 worker 4개 **고정**": **BLOCKED** — stock KV router가 affinity를 만들지 않아(7/8 이동) 고정을 강제·검증할 수단이 없다.
  해제 조건: `patches/dynamo-vamp-receipt.patch` 적용(target receipt) + per-request pinning 수단(`vamp_target_worker` 전달 경로 확인).
  G-C("실제 다른 worker 실행" 검증)도 같은 해제 조건에 걸린다.

## G-C: 강제 A→B 이동, 목적지 local-cold 재계산 (2026-09-06)

토폴로지(고정 endpoint): frontend A `--namespace vampA --http-port 8080` ↔ worker s1(`DYN_NAMESPACE=vampA`), frontend B `--namespace vampB --http-port 8081` ↔ worker s2(`DYN_NAMESPACE=vampB`).
etcd `instances/vampA/backend/generate`=1, `instances/vampB/...`=1 검증. 양 worker 재기동으로 tier 초기화, `--salt gc`. receipt patch·pinning 없이 **토폴로지로 목적지 고정**.
1 세션, turn 0–2 → A, turn 3–4 → B. prompt 12,819 tok, 순차(concurrency 1). `solab/gc_cell.py`, `solab/frontend_ns.sh`, `worker_vamp.sh`(NS, PYTHONHASHSEED=0).

| turn | endpoint | latency | 해석 |
| --- | --- | --- | --- |
| 0 | A | 4.189 s | cold (재계산 + eager store) |
| 1–2 | A | 0.539 / 0.533 s | GPU hit |
| **3** | **B** | **4.175 s** | **local-cold: GPU miss + CPU tier miss → 재계산** |
| 4 | B | 0.535 s | GPU hit (B 자체 캐시) |

| worker | 요청 | GPU prefix hit | connector | READY 통지 |
| --- | --- | --- | --- | --- |
| s1 (A) | 3 | 25,600 / 38,457 (66.6% = 2/3) | 0 / 12,857 (turn 0 cold 조회) | 3 (801 + 1 + 1 blocks) |
| s2 (B) | 2 | 12,800 / 25,638 (49.9% = 1/2) | **0 / 12,838 (turn 3 cold 조회)** | 2 (801 + 1) |

**판정: PASS** — 목적지 local-cold(B connector 0 hit), 실제 다른 worker 실행(네임스페이스 인스턴스 1 + s2 sidecar가 2요청분), recompute 확인(4.175 s ≈ A의 cold 4.189 s). 5/5 marker 일치.
concurrency 1이므로 4.18 s는 12.8K tok cold prefill의 **큐 대기 없는 응답 지연**이다(service time 근사치로 참고 가능; 여전히 응답 지연).

## G-D: B1 한 세션 실제 KV network migration (2026-09-06)

토폴로지는 G-C와 동일(vampA=s1:8080, vampB=s2:8081). 양 worker를 **in-engine agent**(`solab/vamp_agent.py`, `spec_module_path`)로 재기동:
A는 첫 ≥64-block READY run을 **scheduler thread에서 동기 pin**(export candidate), 제어 채널(7001/7002)과 `PayloadReceiver`(7101/7102)를 EngineCore 안에서 서비스.
`--salt gd`, tier 초기화. 1 세션 turn 0–2 → A, **gap 중 hook**(`solab/gd_hook.py`) → turn 3–4 → B. 순차.

hook 순서: A status(candidate 801 blocks) → A `hashes` → B `import_prepare`(reserve 포스트) → **B nudge #1**(idle 엔진 step 유발 → mailbox drain) → B reserved(801, evicted 0)
→ A `export`(bridge.gather → TCP → B receiver) → B payload 수신·검증 → `bridge.import_payload` → commit 포스트 → **B nudge #2** → committed. 총 26.8 s.

| 단계 (agent 로그) | 값 |
| --- | --- |
| A `candidate_pinned` | 801 blocks, lease 1, hashes head `20e88d2f…, a770f5f8…, aab8398f…` |
| A `export_done` | nbytes **2,099,773,440** (= 801 × 2,621,440), gather 1.271 s, TCP send **21.66 s** (~97 MB/s), sha256 `8a9722…`, DONE |
| B `import_reserved` | 801 to_store, 0 evicted |
| B `import_payload_received` | 2,099,773,440 B, **complete=true** (receiver가 chunk별 sha256[:16] + 전체 sha256 검증), error null |
| B `import_written` | 0.413 s (CPU tensor에 zero-copy 대상 슬롯 기록) |
| B `import_committed` | 801 blocks; 이때 발생한 READY 이벤트의 hashes head가 **A와 동일** → A의 BlockHash가 B의 키로 그대로 유효 (`PYTHONHASHSEED=0`) |

| turn | endpoint | latency | 해석 |
| --- | --- | --- | --- |
| 0 | A | 4.174 s | cold |
| 1–2 | A | 0.537 / 0.540 s | GPU hit |
| **3** | **B** | **0.641 s** | **GPU miss → connector lookup → 가져온 CPU tier hit → restore** |
| 4 | B | 0.533 s | GPU hit |

| worker | GPU prefix hit | connector | 비고 |
| --- | --- | --- | --- |
| s1 (A) | 25,600 / 38,457 | 0 / 12,857 | G-C와 동일 |
| s2 (B) | 12,800 / 25,669 | **12,800 / 12,869 (99.5%)** | turn 3에서 800 blocks × 16 tok 복원; 801번째 block은 A turn-0 고유 suffix라 미사용(정합) |

**판정: PASS** — source/destination checksum 일치(receiver 검증), prefix/layout 일치(801 × block_bytes 정확, to_store 801), import+restore 완료 후 B가 정답 marker 출력(2/2).
G-C 대비 B 첫 턴 4.175 s → 0.641 s는 **같은 조건(순차, 12,819 tok)에서의 관측 지연 차이**이며, 전송 자체(21.7 s)는 gap 중에 일어났다.

관측 주의:
- TCP 전송 21.7 s(~97 MB/s)는 **B1 correctness prototype**의 값이며 성능 baseline이 아니다(spec §4). 이 값으로는 gap 없는 즉시 이동(MOVE_IMMEDIATE)에서 재계산(4.2 s)보다 느리다 — 이동이 이득이 되려면 요청 없는 gap에 전송이 끝나야 한다는 점을 실측이 그대로 보여준다.
- nudge 요청 2건(각 2 tokens)이 B에 추가로 들어갔다(cell 밖으로 기록; B READY 1-block 이벤트 2개). idle EngineCore는 step을 돌리지 않으므로 mailbox 명령(reserve/commit)을 drain하려면 현재로선 필요하다.
- B agent가 import된 run을 자기 candidate로 pin했다(lease 2) — "첫 큰 READY run pin" 규칙의 부작용, 무해하나 기록.
- Dynamo patch 미적용, vLLM 소스 무수정 유지.

## G-H: calibration 프로브 (2026-09-06, endpoint A = s1, 순차 concurrency 1)

spec §10 "no-queue probe": 요청을 한 건씩 보내 큐 대기를 배제했다. 값은 **응답 지연**(prefill + `/no_think` decode ≤24 tok 포함)이며 service time 근사치다.
cell마다 `--salt`가 prefix item에 섞여 실제 prompt_tokens가 조금씩 다르다(표에 실측값 기재). restore 셀은 working set > GPU pool(97,552)로 만들어 turn 1이 **전부 CPU tier restore**임을 connector 계정으로 확인했다(GPU hit 0, connector ≈50% = turn 1 전량).

| prompt tok (실측) | cold: 재계산 + eager store | GPU hit | CPU→GPU restore | 표본 |
| --- | --- | --- | --- | --- |
| 4,794–5,494 | **1.90 s** (med, n=26: 1.68–1.92) / 1.86 (hit 셀 t0) | **0.49 s** | **0.59 s** (med, n=26: 0.54–0.59) | `calib_350_restore3`(26×2), `calib_350_hit`(1×3) |
| 8,962–10,262 | **3.08 s** (med, n=14: 3.05–3.33) / 3.46 (10,262 tok) | **0.52 s** | **0.59 s** (med, n=14: 0.59–0.65) | `calib_650_restore2`(14×2), `calib_650_hit` |
| 12,817–12,825 | **4.34 s** (med, n=10: 4.26–4.41) / 4.39 | **0.54 s** | **0.64 s** (n=10: 0.637–0.645) | `calib_1000_restore`(10×2), `calib_1000_hit` |
| 12,819 (G-D) | — | — | 0.641 s (B, import된 tier) | `gd` |

network A→B (G-D 단일 표본, 12,819 tok = 801 blocks = 2,099,773,440 B): `bridge.gather` 1.27 s + TCP 21.66 s (~97 MB/s, B1 correctness prototype) = ~23 s. 4.8K/9K 표본 없음.
CPU→CXL, CXL→GPU: **없음**(G-E/F BLOCKED).

관측 정리(주장 아님): restore는 GPU hit 대비 +0.05–0.10 s 수준(0.8–2.1 GB), cold는 prompt 길이에 거의 선형(≈0.34 s/1K tok). 이 값들은 이 모델·A6000·s1 host DRAM 경로에서만 유효하며 §10대로 **측정 범위 안에서만** 보간한다.
무효 표본: `calib_350_restore`(working set 54.9K < pool → turn 1이 GPU hit), `calib_650_restore`(혼합), `calib_350_restore2`(95.9K, 경계 → 혼합), 첫 `calib_1000_*`(salt로 prompt 16,824 tok > max_model_len → 500). raw 보존.

**판정: 실제 경로별 측정값 확보(recompute / GPU hit / CPU restore 3크기, network 1점)** — frozen config 반영은 `configs/calibration_solab_2026-09-06.json`(라벨 `real`), no-oracle mock 통과는 정책 비교 단계에서.

## G-E: CXL 작은 payload cross-host 안전 검증 (2026-09-06) — **PASS**

전제 확정(`docs/provider-api-findings-2026-09-06.md`): offset은 slice-relative(dax_base = 장치 64 GiB), 우리는 allocator가 준 객체만 사용, fence/refresh는 export 함수, lock 핸들 8 B.

### 기동 절차(실측으로 수정됨)
1. **노드 ID는 라이브러리 변형에 컴파일**되어 있다: N0/N1의 유일한 코드 차이 = `__init_local`의 `movw $0x0|$0x1 → my_id.nid`. provider의 `libcxl_shm.so` symlink는 **양 노드 모두 N1** → s2에서 그대로 띄우면 manager가 nid 1로 attach(첫 시도: `id:[1,0,…]`, "empty arena" 없음, lock thread 없음). provider 파일은 그대로 두고 `~/vamp/cxl/lib/libcxl_shm.so`(s2→N0, s1→N1)를 `LD_LIBRARY_PATH` 앞에 둔다(`solab/cxl_manager_start.sh`).
2. arena가 다른 node 0에 의해 초기화된 채 남아 있었으므로 예외 절차 `solab/cxl_fresh_start.sh`: 우리 manager 종료 → `cxl_clear_ucsd`(**offset 68,719,476,736 / size 68,719,476,736 출력 확인 = [64,128) GiB, 35 s**) → wrapper로 `start_server.sh`.
3. 결과 `id:[0, 0, 221454]`, `initializing... clflush`, `Heap: init done`, `meta arena [34304,262144) payload arena [262144,33554432) pages`, `hash initialized 2097152`, lock thread(`lock_manager_thread_func`, rank 1) → **threads 2**, `DONE and Waiting forever`(pause 상주). 완료 판정은 로그 라인으로(고정 대기 아님).
4. **arena 주의**: 페이지 단위 payload arena [1 GiB, 128 GiB) — allocator가 128 GiB 매핑 전체를 arena로 본다. 물리 슬라이스는 64 GiB이므로 **adapter가 모든 payload 할당의 offset+len < 64 GiB를 검사·거부**해야 하며 실사용 용량은 ≈63 GiB(1 GiB–64 GiB). `StoreCapacity`는 이 값으로 잡는다.

### ping 결과 (`solab/cxl_ping.c`, provider 헤더·라이브러리로 빌드, writer s2 nid 0 / reader s1 nid 1)
writer: `shmalloc` 레코드 + `shm_payload_alloc` payload + `cxl_shm_allocate_lock`; 패턴 채움 → `clwb_region_with_barrier`(fence) → lock → 레코드{magic, gen, nbytes, checksum, lockptr, payload_off, state=READY} → `clflush_region_with_mfence`(레코드) → unlock → `cxl_shm_put`.
reader: `cxl_shm_connect` → `cxl_shm_get` → 레코드 refresh → `cxl_lock_t{lockptr}` 재구성 → lock → refresh → 레코드 읽기 → unlock → payload refresh(`clflush_region_with_mfence`) → checksum.

| nbytes | payload off | fence(clwb) | connect(s1) | lock wait | refresh(clflush) | checksum |
| --- | --- | --- | --- | --- | --- | --- |
| 1 MiB | 0x40000000 (1 GiB) | 0.1 ms | 0.092 s(첫 attach) | 0.0 ms | 0.1 ms | **일치** |
| 64 MiB | 0x40100000 | 4.9 ms | 0.000 s | 0.0 ms | 7.2 ms | **일치** |
| 1 GiB | 0x44100000 | 79 ms | 0.000 s | 0.1 ms | 85 ms | **일치** |

- 다른 노드가 만든 lock을 lockptr로 재구성해 acquire 성공 → `FOREIGN_ENTRY_LOCK_UNAVAILABLE` 해소 경로 실증.
- 할당은 payload arena 시작(1 GiB)부터 상향 — 모두 64 GiB 안(검사 통과).
- 통과 조건 대비: startup 절차 ✅, offset 경계 ✅(allocator 객체만, 64 GiB 검사), lock/visibility ✅(cross-host lock + fence/refresh 후 checksum 일치), generation ✅(레코드 gen 전달·확인; 다중 generation 교체 시나리오는 G-F에서).
### ctypes 바인딩 실장비 검증 (`solab/cxl_pyping.py`, `vamp_cxl.cxl_shm_binding.CtypesProviderApi` + `solab/cxl_abi_solab.py` confirmation)

서명 19개(connect/finalize/is_initialized/shmalloc/shfree/shm_payload_alloc/free/put/get/destroy/get_offset/get_ptr/allocate_lock(uint64*)/free_lock/lock_acquire/lock_release(uint64 값)/clwb_region_with_barrier/clflush_region_with_mfence/sfence)를 헤더·`nm -D`·C ping으로 확인 후 confirmation에 등록, 노드별 라이브러리 sha256 고정.

| 단계 | 결과 |
| --- | --- |
| s2 `write` 64 MiB seed 11 | WRITE_OK, payload_off 2,215,641,088, lockptr 0xA000008403000, write 36 ms, fence 5.0 ms |
| s1 `read` (refresh) | **READ_OK** gen 1, lock 대기 0, refresh 6.2 ms |
| s2 `rewrite` 같은 payload seed 12 → gen 2 | REWRITE_OK |
| s1 `read --no-refresh` | **READ_MISMATCH**: local `4f36899b…` ≠ remote `669fc7b3…`이며 구버전 `a54a738e…`도 아님 → **로컬 캐시 라인 일부만 stale인 혼합 상태** = 비-coherent 경로에서 refresh 필수임을 실증 |
| s1 `read` (refresh) | **READ_OK** gen 2 |
| s1 Python reader ← C writer `VAMP_PING` 1 GiB | **READ_OK** — C/Python 레코드 레이아웃·lock 재구성 상호운용 |

(read+checksum 5 s/64 MiB, 58 s/1 GiB는 Python fnv1a 루프 비용이며 CXL 지연이 아니다; 실제 KV 이동은 memmove/numpy 경로를 쓴다.)

바인딩 변경: `EntryRecord`에 `lockptr` 추가 → foreign 항목을 `_lookup`이 lock과 함께 채택(`FOREIGN_ENTRY_LOCK_UNAVAILABLE` 해소, 에뮬레이션 테스트 `test_foreign_ready_entry_is_readable_via_record_lockptr`), payload는 `shm_payload_alloc`(payload arena), `_read_entry`/`_write_entry`가 refresh/fence 수행, 64 GiB 경계는 `OffsetMapper.check_range`.

## G-F: B2 한 세션 실제 CXL KV migration (2026-09-06) — **PASS** (`gf2`)

토폴로지 G-C/G-D와 동일(vampA=s1:8080, vampB=s2:8081), 양 worker `vamp_agent`(CXL 경로 포함), `CXL_SHM_LIBRARY`=노드별 symlink(s2→N0, s1→N1), manager(node 0, pid 221454) 상주. `--salt gf2`, tier·candidate 초기화(재기동). 1 세션 turn 0–2 → A, **gap 중 `solab/gf_hook.py`** → turn 3–4 → B. prompt 13,822 tok = 864 blocks.

hook(7.3 s): A `cxl_export` → B `cxl_import_prepare` → nudge → B `cxl_import_status`(refresh→sha256→CXL→CPU zero-copy→commit 포스트) → nudge → committed.

| 단계 | 값 |
| --- | --- |
| A `cxl_export_done` | 864 blocks = **2,264,924,160 B**, payload_off 4,382,523,392(≈4.08 GiB, 64 GiB 안 — adapter 검사 통과), gather 1.50 s, **CXL write(memmove) 0.234 s (~9.7 GB/s), fence(clwb) 0.167 s**, sha256 1.57 s, 레코드 lockptr 0xC000008403000 |
| B `cxl_import_reserved` | 864 to_store, 0 evicted (nudge #1로 drain) |
| B `cxl_import_written` | **refresh(clflush) 0.170 s, sha256 2.545 s = A 값과 일치, CXL view에서 CPU tier로 복사 0.804 s**(ctypes view → `bridge.import_payload`; 복사 1회) |
| B `cxl_import_committed` | 864 blocks; READY 이벤트 hash head `bc620938…` = A와 동일 |

| turn | endpoint | latency |
| --- | --- | --- |
| 0 | A | 4.535 s (cold) |
| 1–2 | A | 0.540 / 0.538 s |
| **3** | **B** | **0.654 s** — connector **13,792 / 13,883 (99.3%)** = CXL에서 가져온 CPU tier restore |
| 4 | B | 0.543 s (GPU hit) |

A sidecar: connector 0/13,882(turn 0 cold), GPU 66.5%. 5/5 marker 정답.

**판정: PASS** — local-cold 목적지(B)에서 shared READY prefix가 복원되어 정답 출력. checksum(A sha256 = B sha256 after refresh) 일치, 레이아웃(864 × 2,621,440) 일치, generation 1 확인.
**1차(`gf`)는 절차상 INVALID**: 기능은 동일하게 성공(B 0.68 s, connector 99.6%)했으나 hook이 중간 단계 "reserved"만 기다려 timeout(rc 4)했고 runner가 B 턴을 진행함. 수정(`bae291922`): hook은 target 이상 단계 허용, `gc_cell.py`는 hook rc≠0 시 `cell_invalid` 기록 후 중단. raw `gf_*` 보존.

관측(주장 아님): **정책이 써야 하는 준비 비용은 hook 전체 벽시계 7.3 s**(gather 1.5 + CXL write 0.23 + fence 0.17 + sha256 1.6 + 제어/nudge + B refresh 0.17 + sha256 2.5 + 복사 0.80)이다. "1.4 s"는 그중 write/fence/refresh/복사 4단계의 합일 뿐이며 이동 비용 전체가 아니다. TCP prototype hook은 26.8 s. nudge 2건은 여전히 필요(idle EngineCore). 벽시계 값이며 통제된 성능 비교가 아니다.

## 실패 주입·정리 게이트 (2026-09-06) — **PASS** (`fail_wrongkey2`, `fail_checksum2`)

절차: `gate_cell.sh` → `gc_cell.py --hook "gf_hook.py --inject …"`; hook rc≠0 → `cell_invalid` 기록·B 턴 생략·rc 1 → `CELL_INVALID`(실패 전파). 정리 순서(controller-serialized): B cleanup(import) → A cleanup(export: `lock_free → destroy → payload_free → shfree`, lease release 게시) → A nudge → 양쪽 status 검증 = `CLEANUP_GATE`.

| 셀 | 주입 | 관측 | 정리 결과 |
| --- | --- | --- | --- |
| `fail_wrongkey2` | B prepare에 없는 key | A export 801 blocks 2,099,773,440 B → B "not found" | A `cxl_freed` payload+hashes, leases 0/0, **gate ok**, hook 4.1 s |
| `fail_checksum2` | reservation 후 기대 digest 오염 | reserved 864 → refresh 0.176 s → sha256 2.547 s 불일치 → abort 게시 | abort 드레인(nudge) → B idle → A `cxl_freed` 2,264,924,160 B, leases 0/0, **gate ok**, hook 5.7 s |
| `fail_transfer` (B1, 네트워크) | reservation 후 sender가 chunk 3에서 error marker | A export `FAILED`(injected sender error) → B `import_payload_received complete=false` → stage failed, abort 게시 | nudge 드레인 → B cleanup: 수신 버퍼 2,099,773,440 B 폐기·stage idle → A lease 반납 → **strict gate ok**(failed_checks 없음), hook 7.1 s |
| `b1_gate` (B1 정상 경로) | 없음 | TCP 2,264,924,160 B: gather 1.44 s, send 23.4 s, B committed 864 | B cleanup(committed→idle, staging 버퍼 폐기) → A lease 반납 → **strict gate ok**; transfer 29.9 s / cleanup 0.22 s 분리 기록; B 첫 턴 0.654 s, 5/5 정답 |

**Strict gate(2026-09-06 리뷰 반영, `solab/hook_common.py`)**: 양쪽 cleanup RPC `ok` + A/B lease 0 + candidate 없음 + A `cxl_export_held` 없음 + B network/CXL import stage idle + B 수신 payload 버퍼·큐 비움. cleanup은 commit/abort가 드레인되기 전이면 거부하고, `destroy` 실패 시 payload를 해제하지 않고 포인터를 보존한 채 `ok=false`(셀 실패). hook 출력은 `transfer_s`(hook 시작→목적지 committed/failed)와 `cleanup_s`를 분리.

**사고 1건(원인 확정·수정)**: 첫 `fail_wrongkey`에서 A cleanup이 다른 RPC 스레드에서 실행되어 provider `my_id`(TLS)가 -1 → lock timeout 480 s → **EngineCore abort**. 격리 재현 후 agent의 모든 provider 호출을 전용 스레드 1개로 직렬화(`bf589151e`, `provider-api-findings` §4). 첫 `fail_checksum`은 hook이 상태 한 번에 reserved→failed 진행을 try 밖에서 관측해 정리 없이 종료 → 수정(`dd347096f`) 후 재실행. 이슈·질문 정리 = `review-request-2026-09-06-cleanup-gate.md`.

## B0/B1/B2 통제 비교 — 1 세션, 동일 입력 (2026-09-06, `compare_arms.sh cmp`)

설계(Codex 승인): 동일 `--salt cmp`(모든 arm에서 prompt_tokens **12,821** 동일), A 3턴 → gap(hook) → B 2턴, **arm마다 cold reset**(`arm_reset.sh`: worker+EngineCore 재기동, agent+bridge·etcd 1/1·양 endpoint 실요청 확인; manager·etcd·nats·frontend 유지), 묶음 시작 전 **fresh start 1회**(양 worker 정지 → `cxl_clear_ucsd` 32 s → manager nid 0 재기동, `fresh_start_20260906T070438Z.log`), 양 endpoint 동일 warm-up. 바뀐 것은 전송 메커니즘과 CXL key만. B0의 A auto-pin은 B 턴 후 post-hook(`gf_hook --cleanup-only`)으로 반납.

| arm | gap 내용 | transfer_s / cleanup_s | **B 첫 턴(turn 3)** | B turn 4 | B turn 3 sidecar | gate |
| --- | --- | --- | --- | --- | --- | --- |
| **B0** recompute | 없음(gap 0) | – / 0.116 (post-hook) | **4.278 s** | 0.544 s | GPU hit 0, connector hit 0 / 12,821 (local-cold) | ok |
| **B1** network KV | gd_hook: gather 1.82 s + TCP send 20.8 s(2,099,773,440 B) + commit | 26.014 / 0.202 | **0.638 s** | 0.549 s | connector hit **12,800 / 12,821**, GPU hit 0 | ok |
| **B2** CXL KV | gf_hook: gather 1.49 s, CXL write 0.234 s, fence 0.155 s, sha256; B refresh 0.158 s, sha256 2.114 s, CXL→CPU 복사 0.692 s | 7.018 / 0.119 | **0.645 s** | 0.534 s | connector hit **12,800 / 12,821**, GPU hit 0 | ok |

A 쪽은 세 arm 모두 turn 0 = 4.21–4.26 s(cold), turn 1–2 = 0.53–0.54 s(GPU hit) — cold reset이 arm 간 동일 시작 상태를 만들었다는 대조. B turn 4는 세 arm 모두 GPU hit 12,800(turn 3에서 채워진 GPU prefix) 0.53–0.55 s. 5/5 marker 정답 ×3, 세 arm 모두 strict `CLEANUP_GATE` ok, `ALL_ARMS_OK`.

**읽는 법(주장 아님, 관측 응답 지연)**: 목적지 B의 첫 턴은 local-cold 재계산(B0) 4.28 s 대비 CPU tier restore(B1·B2) 0.64 s로, **B1과 B2는 B 첫 턴에서 구별되지 않는다**(둘 다 CPU tier에 같은 블록이 있음). 차이는 **gap 안의 준비 비용**: 네트워크 26.0 s vs CXL 7.0 s(같은 2.10 GB, 같은 gather·sha256 포함; TCP prototype은 8 MiB chunk 단일 스트림이라 최적화된 네트워크 경로의 대표치가 아님). 이 준비 비용이 B 첫 요청 도착 전에 끝나야 이득이 실현되므로 gap 길이가 정책 변수 — gap 비교(도착 전/후)는 다음 단계. n=1, 통제된 성능 비교가 아니라 경로 기능·비용 자릿수 확인.

## Gap 실험 `gap1` — 3 arm × 4 gap, WAIT 정책 (2026-09-06 07:21–07:46Z)

프로토콜(Codex 명세): `t0` = A 마지막 응답 완료. hook(export→import→cleanup)은 t0에 백그라운드 시작. 다음 턴의 controller **도착 = t0 + gap(전 arm 동일)**. B0는 도착 즉시 B로 전송(local-cold 재계산), B1/B2는 hook이 **cleanup까지 성공 완료**한 뒤 전송(WAIT; 재계산 병행 없음). **주 지표 = 도착→응답 완료**(기다린 비용 포함). 셀마다 cold reset, 동일 prompt 12,821 tok, `RUN=gap1` 파일명 분리. 12/12 CELL_OK, 12/12 strict gate ok, 5/5 marker ×12.

| gap | B0 도착→완료 | B1(network) hook / wait / **도착→완료** | B2(CXL) hook / wait / **도착→완료** |
| --- | --- | --- | --- |
| 0 s | **4.214** | 27.10 / 27.19 / **27.838** | 7.86 / 7.94 / **8.582** |
| 5 s | **4.297** | 26.98 / 22.06 / **22.701** | 7.46 / 2.55 / **3.189** |
| 10 s | **4.308** | 27.26 / 17.34 / **17.975** | 7.58 / 0 / **0.646** |
| 35 s | **4.286** | 27.05 / 0 / **0.642** | 7.92 / 0 / **0.663** |

(hook = hook 자체 `total_s`, cleanup 0.11–0.20 s 포함. runner의 `hook_wall_s`는 도착 후 측정돼 `max(hook, gap)`이 되므로 쓰지 않음. A turn 0 cold 4.22–4.27 s 12셀 동일, B turn 4 GPU hit 0.53–0.55 s 12셀 동일, B 첫 턴 HTTP 지연은 B1·B2 모두 0.64–0.66 s.)

**읽는 법(n=1, 관측값)**: 이득 조건 `남은 준비 대기 + restore < 재계산`. **직접 확인한 것**: B2(CXL)는 gap 0 s에서 손해(8.58 > 4.21), gap 5 s에서 이득(3.19 < 4.30); B1(TCP prototype)은 gap 10 s에서 손해(17.98 > 4.31), gap 35 s에서 이득(0.64). **비용식으로 추정한 경계**(gap ≈ hook − (재계산 − restore), 실측 아님): CXL 약 4 s(7.6 − 3.65), TCP 약 23.5 s(27.1 − 3.65) — Codex 사전 예상 3.6 / 22.7 s와 같은 자릿수.

**B1 링크 확인(2026-09-06)**: TCP arm의 경로는 s1 EngineCore → s2 PayloadReceiver(7102)로, s1↔s2 private 링크 `ens7f1`(Intel igb, **1000 Mb/s, MTU 1500**)을 탄다. 2,099,773,440 B / 20.8 s ≈ 101 MB/s = 1 GbE 이론치(~117 MB/s)의 86% → **B1의 준비 비용은 링크 대역폭 지배**이며 sha256을 빼도 크게 줄지 않는다. 양 노드에 `ens1f0np0/np1`(고속 포트로 추정, operstate down, 미연결)이 있어 B1은 데이터센터 네트워크의 대표치가 아니라 "이 testbed의 1 GbE 경로"로만 읽어야 한다.

**계측 보정(Codex 감사, 2026-09-06)**: gap1 runner는 지표를 예정 도착(t0+gap)이 아니라 실제 wakeup부터 재서 4–35 ms가 빠졌다(`보정 지연 = arrival_to_done + arrival_after_t0_s − gap_s`; 예: gap 35 s의 B1/B2 = 0.642→0.677, 0.663→0.698). 위 표는 미보정 원값이며 결론에 영향 없음. 이후 runner(`gc_cell.py`, `2c80abbaa` 이후 수정)는 monotonic clock·예정 도착 기준·`wakeup_late_s` 별도 기록·hook 종료는 10 ms 폴링 관측으로 변경(gap2 종료 후 배포). gap1/gap2 대조는 같은 예정 도착 기준으로 보정해 개별 값·범위만 제시(p99·통계적 동등성 주장 금지). gap ≥ hook이면 두 경로는 구별되지 않는다(0.64 vs 0.66). **주장 아님**: 정책 우위나 일반적 CXL-대-네트워크 성능 주장이 아니라 경로 비용 자릿수와 WAIT 정책의 손익 경계 1점 관측. TCP prototype은 8 MiB chunk 단일 스트림. 다음 = 반복 측정(≥3), 이후 "늦은 import와 재계산 병행" 정책.

**정리 순서 수정(Codex, `195e9c2e0`)**: `hook_common.cleanup_and_verify`가 B cleanup 거절 시 A cleanup을 호출하지 않고(A lock/key/payload 보존) gate 실패로 종료. 이번 12셀·이전 성공 셀은 모두 B cleanup ok였으므로 결과 무효 아님.

## D0–D2: GPU ↔ CXL 직접 경로 feasibility (2026-09-06, `solab/cxl_dma_probe.py`) — **PASS**

우선순위 변경(Codex/jp): DRAM 경유 비용으로 정책을 정교화하기 전에 직접 경로 가능 여부를 확인. gap2는 6셀(gap 0·5 × 3 arm) 완료 상태로 보존·중단(값은 gap1과 근접: B2 8.51/3.30, B1 27.80/23.64, B0 4.2–4.3). 양 worker 정지, manager 유지, **독립 프로세스**(provider 호출은 메인 스레드 1개), payload는 provider `shm_payload_alloc`으로 우리 슬라이스 안에서만 할당. writer = s1(node 1, GPU A), reader = s2(node 0, GPU B). 복사 시간과 sha256(correctness)은 분리 측정.

| 단계 | 결과 |
| --- | --- |
| D0 등록 | host 대조군 64 MiB: `cudaHostRegister(flag 0)` ok. **CXL payload(devdax mmap) 256 MiB·2 GiB: flag 0(Default)로 ok**(6 ms). IoMemory/Mapped는 시도 불필요. driver 550.54.14 / CUDA 12.8 runtime(torch) / RTX A6000 / kernel 6.6.0-rc6 조합 |
| D1 A GPU→CXL | 256 MiB 7.47 GB/s; **2 GiB 0.295 s = 7.28 GB/s**(2회 동일), 이후 clwb fence 0.159 s, 로컬 sha256 일치 |
| D1 CXL→B GPU | 256 MiB 10.69 GB/s; **2 GiB 0.201 s = 10.7 GB/s**(2회 동일), 사전 clflush refresh 0.160 s |
| D2 cross-host | A GPU → 공유 CXL → B GPU: lock+레코드+fence/refresh 규약 하에 **GPU 바이트 sha256 일치, CPU view 일치**(256 MiB 1회, 2 GiB 2회) |
| 대조 | 같은 GPU ↔ 일반 pinned DRAM 2 GiB: D2H 26.1 / H2D 25.1 GB/s. GPU 링크 = PCIe **Gen4 x16**(b8:00.0 max 16 GT/s). CXL 장치 = a8:00.0 class 0x050210, vendor 0x1c5c(**SK hynix**) |

**읽는 법**: 직접 경로는 이 구성에서 동작한다(등록·복사·cross-host 가시성·바이트 일치). 10.7 / 7.3 GB/s는 GPU PCIe 한계(25 GB/s 실측)가 아니라 **CXL Type-3 장치 경로의 실효 처리량**이며, 단일 스레드 CPU 읽기(약 3 GB/s)보다 3.5배 높다. 이는 **DRAM-staged 경로의 CXL 구간(write 0.23+fence 0.16+refresh 0.16+CXL→DRAM 0.69 s)과 DRAM→GPU restore를 GPU DMA 0.30+0.20 s로 대체할 수 있다**는 뜻이지만, **D3(실제 KV 블록을 vLLM GPU KV 텐서에서 직접 CXL로 내보내고 B의 GPU KV 블록으로 들여와 재사용·정답 확인)은 미완**이다. 현재 agent는 CPU tier에서 gather하므로 자동으로 직접 경로가 되지 않는다. 기존 gap1/gap2·B0/B1/B2 결과는 **DRAM-staged 기준선**으로 보존.

## 다른 gate

| Gate | 상태 | 비고 |
| --- | --- | --- |
| D0–D2 직접 경로 | **PASS** | 등록 ok(flag 0), 2 GiB GPU→CXL 7.3 GB/s / CXL→GPU 10.7 GB/s, cross-host 바이트 일치. D3(실 KV) 미완 |
| Gap `gap1` | **완료(n=1, 12셀)** | 도착→완료: B0 4.2–4.3 s 상수; B2 8.58/3.19/0.65/0.66; B1 27.84/22.70/17.98/0.64 (gap 0/5/10/35). 위 참조 |
| B0/B1/B2 | **완료(n=1)** | 동일 입력·cold reset·strict gate. B 첫 턴 4.28 / 0.64 / 0.65 s, 준비 비용 – / 26.0 / 7.0 s. 위 참조 |
| 실패 주입·정리 | **PASS** | wrong_key / checksum 둘 다 `CLEANUP_GATE ok`, 실패 전파 확인. 위 참조 |
| G-B | **headroom 계측 진단 완료 / 고정 배치 미검증** | 통과 조건(카운터 해석·무오류) 충족, "worker당 4 고정" 전제 미충족. 1차 INVALID(cache 오염) → 2차 유효 |
| G-C | **PASS** | 고정 endpoint(네임스페이스 분리)로 목적지 보장. 위 참조 |
| G-D | **PASS** | in-engine agent + 제어 채널 + nudge로 export→TCP→import→restore end-to-end. 위 참조 |
| G-E | **PASS** | node 0 manager 기동(N0 라이브러리), 1 MiB/64 MiB/1 GiB cross-host lock+fence/refresh+checksum 일치 |
| G-F | **PASS** | 1세션 A→CXL→B: 2.26 GB, B 첫 턴 0.654 s, connector 99.3%, sha256 일치 |
| G-G | **gap 중 전송 가능성 확인** (network·CXL 변형 모두) | 양 엔진 idle 중 hook이 수행; 단 mailbox drain에 nudge 요청 2건 필요 → "요청 없이 완전 자율 실행"은 아님. 자율 publication(pin 시점 결정·타이머)은 정책 단계 |
| G-H | **측정 완료(부분)** | recompute/GPU hit/CPU restore ×3 크기 + network 1점. CXL 경로 없음 |

## 부수 확인

- 2026-09-05 pressure run(32 세션 × 11,816 tok = GPU pool의 1.94배, `CPUOffloadingSpec`)에서 GPU prefix hit이 0으로 나온 것은 **계측 아티팩트가 아니다**. 같은 OffloadingConnector 경로에서 G-A ON이 GPU hit 82.8%를 정상 계측했으므로, pressure run의 0%는 working set 초과에 의한 실제 eviction이다.
