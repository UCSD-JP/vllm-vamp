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

각 변경은 별도 patch로 전달하고 rollback 방법을 함께 적는다. 이 commit은 위 변경을 포함하지 않는다.

## Fake backend와의 대응

`vamp_cxl` fake는 위 9개 항목을 모두 SUPPORTED로 보고하되 `is_mock=True`,
`backend="fake"`로 표시한다. mock 통과는 hardware capability 통과가 아니다.
