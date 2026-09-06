# Capability Audit (read-only, vLLM v0.19.0 + Dynamo v0.5.0 references)

작성일: 2026-09-05. 대상 commit: `2a69949` (upstream v0.19.0). 이 문서는
`vamp_cxl.kv_transfer_adapter.audit_vllm_0_19_offload()`의 근거이며, 코드를
읽어 확인한 사실만 적는다. GPU/CXL/JP 접속 없이 작성했으므로 아래 "UNVERIFIED"는
실장비 gate에서 확인해야 한다.

## 필수 항목 매트릭스 (spec §5)

| 항목 | 상태 | 근거 | 부족한 것 |
| --- | --- | --- | --- |
| exact_target_worker | UNVERIFIED | `handlers.py.ref` DecodeWorkerHandler.generate는 router가 고른 worker에서 실행; `kv_router/scheduler.rs.ref`가 worker 선택 | per-request pin 경로 미확인. 후보: worker별 single-target endpoint/namespace. G-C에서 receipt로 검증 |
| cpu_ready_notification | PARTIAL | `offloading/scheduler.py update_connector_output` → `manager.complete_store` (finished_sending 이후) | scheduler process 안의 상태만 갱신. 외부 구독/조회 hook 없음 |
| export_lifetime_protection | PARTIAL | `cpu/manager.py prepare_load`(ref_cnt++) / `complete_load`(ref_cnt--), 위반 시 assert | scheduler thread-local. 외부 RPC용 thread-safe lease API 아님 |
| destination_import | PARTIAL | `worker/cpu_gpu.py SingleDirectionOffloadingHandler(CPU→GPU)`, `offloading/scheduler.py update_state_after_alloc` | local CPU tier에서만 복원. 외부 payload를 destination CPU tensor에 넣는 hook 없음 |
| engine_compatible_hash_layout | PARTIAL | `vllm/v1/request.py block_hashes`, `kv_offload/spec.py CanonicalKVCaches` | in-process에서만 접근 가능. cross-host ModelKey/PrefixKey wrapper는 우리 것이고 미검증 |
| transfer_completion_notification | SUPPORTED (local only) | `worker/cpu_gpu.py get_finished` (CUDA end_event.query) | 실패는 보고되지 않고 assert (`offloading/worker.py get_finished`) |
| cancellation_semantics | UNSUPPORTED | `worker/worker.py OffloadingHandler`: transfer_async / get_finished / wait 뿐 | cancel primitive 없음. 제출된 전송은 완료까지 진행 |
| shared_offset_check | BLOCKED | `docs/fixed-shared-memory-api.md`: offset 기준(slice-relative vs device) 미확인 | provider 확인 전 device offset 계산 거부 (`OffsetMapper.device_offset`) |
| cross_host_visibility_primitive | BLOCKED | 같은 문서: flush/fence 미노출 | CPU flush는 GPU DMA coherence가 아님. primitive 확인 필요 |

## 기존 hook에서 관찰한 사실

- `CPUOffloadingManager.lookup`은 `block.is_ready`가 아닌 첫 block에서 멈춘다. `prepare_load`는 READY와 ref_cnt 증가를 같은 호출에서 하지만 scheduler thread 안에서만 안전하다.
- `complete_store(success=False)`는 미완료 block을 제거한다. 실패 경로 정리는 존재하지만 worker 측 `get_finished`가 `assert transfer_result.success`이므로 실제 실패는 예외로 끝난다.
- `_get_reqs_to_store`는 매 step 새 block을 저장 대상으로 잡는다. turn 종료 시점의 마지막 block은 async scheduling 때문에 미뤄질 수 있다(`request_finished`의 TODO). 따라서 turn-end ≠ CPU READY.
- `OffloadingSpecFactory`의 `spec_module_path` hook으로 새 spec class를 끼울 수 있으나, connector scheduler는 단일 `OffloadingManager`와 `(src, dst)` medium 조합의 handler만 다룬다. 새 medium(예: `CXL`)을 추가하려면 `LoadStoreSpec` 서브클래스와 handler 등록이 필요하고, delayed publication은 manager/handler 바깥의 별도 executor가 되어야 한다.
- Dynamo reference `publisher.py.ref`의 VAMP sidecar는 `SchedulerStats.prefix_cache_stats`와 `connector_prefix_cache_stats`를 파일로 덤프한다. connector query의 분모는 GPU miss 토큰이다(spec §2).

## 실제 backend에 필요한 최소 변경 (제안, 미적용)

1. **CPU READY 통지 hook**: `OffloadingConnectorScheduler.update_connector_output`가 `complete_store`를 호출한 직후 block hash 집합을 외부 callback으로 넘기는 optional hook. scheduler process 안에서 호출되므로 thread 안전성은 caller가 책임진다.
2. **export lease adapter**: scheduler process 안에서 `prepare_load`/`complete_load`를 감싸는 단일 owner queue. 외부 RPC는 이 queue에 요청을 넣고 completion event를 기다린다. manager dict를 callback thread에서 직접 만지지 않는다.
3. **CPU payload export/import handler**: `CpuGpuOffloadingHandlers`가 소유한 CPU tensor의 block 범위를 외부 transport에 노출하는 worker 측 handler. `CPULoadStoreSpec.block_ids`는 local ID이므로 cross-host 식별자로 쓰지 않는다.
4. **destination import**: 외부 payload를 destination worker CPU tensor의 새로 할당된 block에 쓰고 manager에 `prepare_store`/`complete_store`로 등록하는 경로. 기존 CPU→GPU handler가 이후 복원을 담당한다.
5. **routing receipt**: 응답 헤더 또는 첫 chunk에 실제 worker id를 실어 runner의 target 검증에 쓴다.

각 변경은 별도 patch로 전달하고 rollback 방법을 함께 적는다. 1~4는 vLLM 소스를
바꾸지 않고 `vamp_cxl/vllm_binding.py`에서 `spec_module_path` 확장점으로 구현했다
(아래 표). 5는 `patches/dynamo-vamp-receipt.patch`로 제공하며 설치본에는 적용하지
않았다. rollback: extra_config에서 `spec_module_path`/`spec_name`을 제거하면 기존
`CPUOffloadingSpec`으로 돌아가고, patch는 `patch -R -p1`로 되돌린다.

## Fake backend와의 대응

`vamp_cxl` fake는 위 9개 항목을 모두 SUPPORTED로 보고하되 `is_mock=True`,
`backend="fake"`로 표시한다. mock 통과는 hardware capability 통과가 아니다.

## 이 branch에서 구현한 것 (2026-09-05, GPU 없이 구현·검증 범위 구분)

"검증 불가"와 "구현 불가"를 구분한다. 아래는 하드웨어 없이 *구현*했고,
각 항목의 검증 수준을 명시한다. 어느 것도 provider 코드나 vLLM 소스를 바꾸지
않았다(`verify_source_lock.py` PASS).

| 항목 | 구현 위치 | GPU-free 검증 수준 | 남은 실장비 검증 |
| --- | --- | --- | --- |
| CPU READY / eviction 통지 | `vamp_cxl/vllm_binding.py` `VampCPUOffloadingManager.complete_store/prepare_store` | 실제 v0.19.0 `CPUOffloadingManager`에 대해 CPU-only 단위 테스트 (`tests/test_vllm_binding.py`) | scheduler process에 listener 등록 후 finished_sending 타이밍 관측 (G-A) |
| export lease (원자적 READY 확인+pin) | 같은 파일 `acquire_export_lease` / `release_export_lease`; 다른 thread는 mailbox(`*_async`) 사용, `lookup`/`take_events`에서 drain | 실제 manager로 partial-pin 거부, pinned block eviction 면제, foreign thread 거부, mailbox drain 검증 | scheduler thread 안에서 drain 지연이 lookup latency에 주는 영향 (G-B) |
| destination import | `reserve_import` / `commit_import` (prepare_store/complete_store 재사용) | 실제 manager로 예약→미READY→commit→READY, 실패 시 제거 검증 | worker 측 tensor 쓰기와 scheduler 측 commit의 순서 (G-D) |
| CPU payload export/import bridge | `CpuPayloadBridge` (worker 측, zero-copy memoryview) | CPU int8 tensor로 gather/import/checksum 왕복 검증 | CUDA pinned tensor에서 동일 동작, handler와의 stream ordering (G-D) |
| spec_module_path 접합 | `VampOffloadingSpec` (`spec_name`/`spec_module_path` extra_config) | import 및 클래스 구조만 확인; engine 기동 없음 | 실제 engine 기동과 handler 등록 (G-A) |
| B1 network transport (correctness prototype) | `vamp_cxl/network_transport.py` TCP chunk framing, per-chunk checksum, chunk 경계 cancel, terminal event 1개 | loopback 1 MiB 왕복, cancel/failure/queued-cancel, executor 결합 (`tests/test_network_transport.py`) | host 간 대역폭 측정은 calibration probe에서 별도; NIXL 등 지원 transport 조사는 미완 |
| 정확한 target worker receipt | `patches/dynamo-vamp-receipt.patch` (`handlers.py`에 `worker_id` 태그, `vamp_target_worker` 불일치 시 생성 거부) | pristine reference에 `patch -p1` 적용·구문 검사 (`tests/test_dynamo_patch.py`) | 실제 Dynamo 설치본 적용, frontend가 `vamp_target_worker`를 전달하는 경로 확인 (G-C) |
| HTTP streaming replay backend | `session_replay.HttpStreamingBackend` | 로컬 SSE 서버로 role/reasoning/content/usage 분류, receipt 검증, misroute 표시 (`tests/test_http_backend.py`) | 실제 Dynamo frontend 헤더/응답 형식 |
| fixed-API shared store | `vamp_cxl/cxl_shm_binding.py` `CxlSharedKVStore`: 우리 metadata record, 단일 writer, reader lease, generation, destroy→shfree 순서 | EMULATED provider(in-process bytearray)로 lifecycle/ABA/capacity/bounds 검증 (`tests/test_cxl_shm_binding.py`). emulated 통과는 G-E/G-F가 아님 | ABI 확인 후 실제 library, offset origin, lock handle ABI, 실제 CXL write (G-E) |
| ctypes 바인딩 | `CtypesProviderApi`: `CXL_SHM_LIBRARY`에서만 로드, 확인된 signature digest만 바인딩, lock 계열은 미바인딩 | 미확인 signature 호출 거부, 라이브러리 hash 불일치 거부 검증 | 운영자의 `AbiConfirmation` 작성 |
| CXL chunk copy transport | `CxlCopyTransport`: chunk 경계 cancel, fence callable 없으면 VISIBLE 미발생(fail closed) | emulated write/read/failure 검증 | visibility primitive 확인 전에는 READY 도달 불가 (BLOCKED 유지) |

여전히 BLOCKED (구현으로 해결 불가, provider/운영자 확인 필요):

- shared offset origin (slice-relative vs device). `OffsetMapper`는 확인 전 device offset 계산을 거부한다.
- cross-host visibility/fence primitive. `CxlCopyTransport`는 fence가 주입되지 않으면 VISIBLE을 내지 않는다.
- crash recovery/fencing: 죽은 writer의 RESERVED/WRITING slot 회수 절차 미구현. 임의 timeout 회수는 하지 않는다(spec §6.8).
- lock handle ABI: 값 전달 handle의 정확한 struct 크기/정렬 미확인. 확인 전에는 `CxlSharedKVStore`를 실제 library에 붙일 수 없다.

## 2026-09-06 BLOCKED 항목 갱신 (provider 코드 분석, `docs/provider-api-findings-2026-09-06.md`)

| 항목 | 상태 변경 | 근거 |
| --- | --- | --- |
| shared_offset_check | BLOCKED → **CONFIRMED slice-relative** | `__init_local`의 `mmap(..., offset=0x1000000000)`, `get_offset = ptr − dax_base`. `OffsetMapper(slice_len=64 GiB, provider_maps_slice_relative=True)`로 device offset 계산 허용 가능 |
| cross_host_visibility_primitive | BLOCKED → **CONFIRMED (CPU staging 경로)** | export `clflush_region_with_mfence/sfence`, `clwb_region_with_barrier`. GPU-DMA coherence는 여전히 별개 |
| lock handle ABI | BLOCKED → **CONFIRMED** | `cxl_lock_t = {volatile shm_ptr_t lockptr}` 8 B by value; ctypes `c_uint64` |
| foreign READY entry 읽기 | 미해결 → **해소 경로 확정** | 레코드에 lockptr(8 B) 저장 → 타 노드가 `cxl_lock_t` 재구성. `_lookup` 채택 시 lock=None 대신 레코드 lockptr 사용(구현 예정) |
| manager startup/clear | 미확인 → **CONFIRMED** | node0·rank0 init이 메타 138 MB zero + 10 GiB 0xAA; 재기동 금지 규칙; `cxl_clear_ucsd`=[64 GiB,128 GiB) |
| crash recovery | BLOCKED 유지 | 데모 범위: 실패 시 clear + cold restart |

## 2026-09-06 (후속) 실장비 확정

- lock handle ABI: **ctypes로 실증** — `cxl_shm_allocate_lock(uint64*)`, `lock_acquire/release(uint64 값)`; s1이 s2가 만든 lock을 lockptr로 재구성해 acquire 성공.
- cross_host_visibility_primitive: **실증** — `clwb_region_with_barrier`(fence) / `clflush_region_with_mfence`(refresh); refresh 생략 시 부분 stale 혼합 checksum 관측(negative test).
- foreign READY entry 읽기: **구현·에뮬 테스트 완료**(레코드 lockptr), 실장비 store 레벨 검증은 G-F에서.
- 남은 BLOCKED: crash recovery(dead writer slot) — 데모 범위 밖(clear + cold restart).
