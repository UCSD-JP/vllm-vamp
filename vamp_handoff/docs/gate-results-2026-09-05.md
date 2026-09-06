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

## 다른 gate

| Gate | 상태 | 비고 |
| --- | --- | --- |
| G-B | **headroom 계측 진단 완료 / 고정 배치 미검증** | 통과 조건(카운터 해석·무오류) 충족, "worker당 4 고정" 전제 미충족. 1차 INVALID(cache 오염) → 2차 유효 |
| G-C | **PASS** | 고정 endpoint(네임스페이스 분리)로 목적지 보장. 위 참조 |
| G-D | **PASS** | in-engine agent + 제어 채널 + nudge로 export→TCP→import→restore end-to-end. 위 참조 |
| G-E~G-F | NOT_RUN / **BLOCKED** | 실제 CXL 접근 — provider 확인(manager startup/clear protocol, offset origin, fence/refresh primitive, lock ABI) 전 실행 금지 |
| G-G | **PARTIAL** | 요청 없는 gap 중 publication은 **network 목적지로 실증**(G-D hook이 양 엔진 idle 상태에서 수행); DRAM→CXL 변형은 G-E/F 승인 후 |
| G-H | NOT_RUN | |

## 부수 확인

- 2026-09-05 pressure run(32 세션 × 11,816 tok = GPU pool의 1.94배, `CPUOffloadingSpec`)에서 GPU prefix hit이 0으로 나온 것은 **계측 아티팩트가 아니다**. 같은 OffloadingConnector 경로에서 G-A ON이 GPU hit 82.8%를 정상 계측했으므로, pressure run의 0%는 working set 초과에 의한 실제 eviction이다.
