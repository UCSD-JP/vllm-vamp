# Provider CXL shared-memory API — 코드 분석 결과 (2026-09-06)

대상: solab-s2 `~/work/{include/cxl_shm/api.h, include/cxl_shm/cacheline.h, lib/libcxl_shm_ucsd_N{0,1}.so, bin/start_server.sh, bin/start_cxl_manager, bin/cxl_clear_ucsd, my_test/main.c}`.
방법: 헤더·스크립트·예제 읽기 + `nm -D`/`strings`/`objdump -d` 디스어셈블(라이브러리는 `-p -g`로 빌드되어 함수명 보존). provider 코드는 변경하지 않았고 실행도 하지 않았다(manager 기동은 별도).
목적: fixed-shared-memory-api.md에서 "확인 필요"로 남겼던 offset 기준·flush/lock 호출·초기화 범위를 우리 adapter가 정확히 호출할 수 있는 수준으로 확정한다.

## 1. 주소 변환 — offset은 slice-relative (0 = 장치 64 GiB)

`__init_local`(cxl_shm_init/connect 공통)이 `/dev/dax1.0`를 연 뒤:

```
319e: movabs $0x1000000000,%r9     ; mmap offset = 64 GiB
31a8: mov    %eax,%r8d             ; fd
31ab: mov    $0x1,%ecx             ; MAP_SHARED
31b0: mov    $0x3,%edx             ; PROT_READ|PROT_WRITE
31b5: movabs $0x2000000000,%rax    ; length = 128 GiB
31bf: mov    %rax,%rsi
31c2: mov    $0x0,%edi             ; addr = NULL
31c7: call   mmap64
```

- 매핑 결과가 `dax_base`(export 전역). **`dax_base` = 장치 오프셋 64 GiB = UCSD 슬라이스 시작.**
- `cxl_shm_get_offset(ptr) = ptr − dax_base` (assert 문자열 `off_is_valid(((shm_ptr_t)((uintptr_t)(addr) - (uintptr_t)dax_base)))`로도 확인), `cxl_shm_get_ptr(off) = dax_base + off` (`off_is_valid` 검사 후).
- 따라서 **API가 다루는 offset은 슬라이스 기준**(`get_offset` 결과 0 = 장치 64 GiB)이며 device offset = 64 GiB + off. **단, 우리는 offset을 직접 만들어 쓰지 않는다**: `cxl_shm_put(key, addr)`의 `addr`는 `shmalloc`/`shm_payload_alloc`이 돌려준 로컬 포인터여야 하고, 슬라이스 앞부분은 라이브러리의 allocator·lock·key-value 메타데이터가 쓰고 있으므로 임의 offset 쓰기는 실행 중인 라이브러리를 깨뜨린다. 우리 adapter는 **allocator가 반환한 객체 범위 안에서만** 읽고 쓴다.
- `off_is_valid(off)`는 `off ≤ 0x1fffffffff`(128 GiB) — 매핑 길이 기준이라 우리 유효 범위(64 GiB)보다 느슨하다. 라이브러리 검사에 의존하지 않고 우리 adapter가 **(offset, offset+len)이 64 GiB 안이고 allocator가 반환한 객체 범위 안인지**를 검사한다(`OffsetMapper(slice_len_bytes=64 GiB, provider_maps_slice_relative=True)` + 객체 경계 검사). SIGBUS 여부는 안전 근거로 삼지 않는다. payload 용량도 64 GiB가 아니라 **allocator의 사용 가능 범위**(manager가 출력하는 arena payload 범위)로 잡는다.
- 라이브러리 안에 다른 64 GiB 상수(0x1000000000)는 이 mmap 한 곳뿐이다.

## 2. 초기화 범위 — node 0·rank 0만 arena를 재생성한다

`cxl_shm_init` 흐름(디스어셈블):
```
is_initialized()  (TLS: 이 프로세스가 이미 init했는지) → 이미면 attach 경로
__init_local()    (bootstrap attach, /dev/dax1.0 mmap, my_id{nid,rid,pid} 배정)
if my_nid()==0 && my_rid()==0:
    memset(dax_base, 0x00, 0x8402280)        ; ≈138.3 MB 메타데이터 영역 zero
    clflush_region_with_mfence(...)
    memset(dax_base, 0xAA, 0x280000000)      ; 10 GiB를 0xAA로 채움 (arena "empty" 패턴)
    cxl_shm_allocator_init() → _heap_init, _init_chunks ; cxl_shm_locksys_init() ; hash_init()
    ("empty arena: meta [%lu,%lu) payload [%lu,%lu)" 출력)
else:
    __wait_for_init()  (node 0 메타데이터 준비를 기다림; clflush로 재읽기)
```
- **`start_server.sh` = `rm /dev/shm/cxl_shm_bootstrap_ucsd` + `start_cxl_manager`.** 삭제되는 것은 **호스트 로컬 POSIX shm**(bootstrap용, `/cxl_shm_bootstrap_ucsd` shm_open)이며 CXL이 아니다. 그러나 이어서 뜨는 manager가 node 0·rank 0으로 `cxl_shm_init`을 실행하므로 **실행마다 메타데이터 138 MB가 0으로, 슬라이스 앞 10 GiB가 0xAA로 재초기화된다.** 실험 중 재기동 = 저장된 KV/메타 전부 소실. 범위는 `dax_base` 기준이므로 우리 슬라이스 안이다.
- `start_cxl_manager`의 main = `cxl_shm_init()` → `"DONE and Waiting forever"` → `pause()`. **상주 프로세스**이고 테스트 워크로드(`__run`, 링크만 됨)는 실행하지 않는다.
- lock thread(`cxl_shm_locksys_launch_lockthread`)는 `cxl_shm_locksys_init`(node 0 init 경로)과 `cxl_shm_connect` 안의 `my_nid()==0` 분기에서만 실행된다. **s1(node 1)에서는 manager도 lock thread도 뜨지 않는다** — handoff 규칙("s1에서 manager 실행 금지")과 일치.
- `cxl_shm_connect`(비-manager 프로세스, 같은 호스트의 추가 프로세스 포함) = `__init_local` + `__wait_for_init`; rank는 `cxl_shm_bootstrap_next_rank`로 호스트 내 배정.
- **`cxl_clear_ucsd`**(별도 도구): `open("/dev/dax1.0")` → `mmap(len=map_size, offset=map_offset)` → `memset(0)` → `clflush_region_with_mfence` → `munmap`. main에서 `movabs $0x1000000000,%rax`를 두 지역변수에 모두 저장 → **map_offset = map_size = 64 GiB → 정확히 [64 GiB, 128 GiB) = 우리 슬라이스 전체**를 지운다(`CXL_SHM_DAX_SIZE` 컴파일 상수). manager와 무관하게 슬라이스를 완전 초기화할 때 쓰는 도구. 다른 tenant 구간은 건드리지 않는다.

## 3. flush / visibility primitive — 라이브러리가 export한다

`cacheline.h`:
```c
void clflush_region_with_mfence(volatile void *addr, size_t size);
void clflush_region_with_sfence(void *addr, size_t size);
void clwb_region_with_barrier(void *addr, size_t size);
```
세 함수 모두 `libcxl_shm_ucsd_N*.so`에 export(`nm -D`). 예제(`my_test/main.c`)는 `clflush_region_with_mfence(ptr, size)`를 쓴다. **함수·호출 규약 확인과 cross-host 가시성 검증은 별개다** — 실제 lock+flush를 써서 s2 write → s1 read → checksum 일치를 확인해야 한다(`solab/cxl_ping.c`).
우리 `CxlCopyTransport`에 주입할 callable:
- **fence (write 후, COMPLETED 전)**: `clwb_region_with_barrier(dest, n)` 또는 `clflush_region_with_sfence(dest, n)` — 쓴 라인을 CXL로 내림.
- **refresh (read 전)**: `clflush_region_with_mfence(src, n)` — 로컬 stale 라인을 무효화해 다음 read가 CXL에서 가져오게 함.
- metadata 레코드도 같은 규칙: writer = 레코드 write → flush → lock release; reader = lock acquire → flush(invalidate) → read.
GPU DMA 경로(GPU↔CXL 직접)는 이 CPU flush로 coherence가 보장되지 않는다 — 우리 데모는 CPU staging(CXL↔host DRAM↔GPU)으로 시작하므로 위 규칙이 적용된다.

## 4. lock — 8바이트 값 전달 핸들, offset이라 노드 간 공유 가능

```c
typedef struct { volatile shm_ptr_t lockptr; } cxl_lock_t;   // 8 bytes
int  cxl_shm_allocate_lock(cxl_lock_t *lock);   // out-param
void cxl_shm_free_lock(cxl_lock_t lock);        // by value
int  cxl_shm_lock_acquire(cxl_lock_t lock);     // by value
void cxl_shm_lock_release(cxl_lock_t lock);     // by value
```
- ABI: 단일 `uint64` 멤버 구조체의 값 전달 = x86-64 SysV에서 정수 레지스터 하나 → ctypes `c_uint64`로 바인딩 가능. **capability-audit의 "lock handle ABI 미확인" 해소.**
- `lockptr`은 lock 워드의 offset이므로 우리 metadata 레코드에 8바이트로 저장하면 다른 노드가 `cxl_lock_t{lockptr}`로 재구성해 acquire 가능 → **cross-host READY 항목 읽기 거부(`FOREIGN_ENTRY_LOCK_UNAVAILABLE`)의 해소 경로**: `_lookup`이 채택한 foreign entry의 lock을 레코드의 lockptr로 복원.
- lock 매직 `"CXL_LOCK"`(0x43584c5f4c4f434b)이 lock 워드 검증에 쓰임. 중재는 node 0의 lock thread(`locksys`)가 담당하므로 **manager가 죽으면 acquire가 진행되지 않는다**(실측 필요).
- `cxl_shm_destroy(key)` 전에 `shfree` 금지, 객체 안에 lock을 넣었다면 `cxl_shm_free_lock` 먼저(헤더 주석). `cxl_shm_get_locked`는 hash_lookup + off_is_valid만 호출(추가 lock 획득 없음) — 우리는 쓰지 않는다.

## 5. 객체 API 동작 모델

- `shmalloc(size)` → dax 매핑 안의 로컬 VA(offset은 `get_offset`). `shm_chunk_alloc`(페이지 정렬)/`shm_payload_alloc`(대용량 payload 영역)도 export.
- `cxl_shm_put(key, addr)`: key→offset 등록(hash). `cxl_shm_get(key, &addr)`: 현재 호스트 VA 반환. 예제는 노드 0이 `put("START")`, 노드 1이 `get("START")` 폴링으로 동기화.
- 예제는 `put` 전후에 CXL 데이터를 flush하지 않고 `kvset`(로컬 malloc)만 flush한다 — 즉 **예제는 coherence 규약을 보여주지 않는다.** 우리 규약(§3)을 그대로 적용해야 한다.

## 6. 노드 ID·라이브러리 선택

- `libcxl_shm_ucsd_N0.so`와 `_N1.so`는 코드·`.data` 동일(8,204 바이트 차이는 build-id/디버그/빌드 경로 문자열). `my_id`(.data, 초기 0)는 **런타임 bootstrap이 채운다** → 노드 ID는 컴파일 상수가 아니다. 예제의 `-DCXL_SHM_NODE`는 테스트 프로그램 분기용일 뿐.
- 양 노드 모두 `libcxl_shm.so → libcxl_shm_ucsd_N1.so` symlink. 바이너리들은 `DT_NEEDED libcxl_shm.so`, RUNPATH 없음 → `LD_LIBRARY_PATH=$HOME/work/lib` 필요.
- 우리 ctypes 바인딩(`CXL_SHM_LIBRARY`)은 노드별로 파일을 지정하고, init/connect 후 `my_nid()`(export)를 호출해 s2=0, s1=1을 **확인한 뒤** 진행한다.

## 7. 아직 실측이 필요한 것 (코드로는 확정 불가)

1. **arena 실제 레이아웃**(meta/payload 범위, 즉 `shm_payload_alloc`으로 쓸 수 있는 용량): manager 기동 시 stderr에 `empty arena: meta [a,b) payload [c,d)`로 출력됨. `_init_chunks` 상수 256 KiB/4 MiB/32 MiB(chunk 크기), 10 GiB 0xAA fill이 arena 힌트지만 확정은 출력으로.
2. lock acquire/release가 실제로 상대 노드 read를 언제 허용하는지(lock thread 지연) — 작은 payload로 A write→B read→checksum 시 측정.
3. `__wait_for_init`가 manager 없이 얼마나 기다리는지(타임아웃 여부).

## 8. 운영 절차(확정)

- manager: **새 CXL 실험 묶음을 시작할 때 한 번**, s2에서만 (`solab/cxl_manager_start.sh`): ① 기존 manager/CXL 사용자 프로세스가 없는지 확인(있고 정상이면 재실행 금지) ② `PATH=$HOME/work/bin:$PATH LD_LIBRARY_PATH=$HOME/work/lib ./start_server.sh`(스크립트가 `start_cxl_manager`를 bare 이름으로 호출하므로 PATH 필요) ③ 로그 보존 ④ 고정 대기가 아니라 로그의 `initialized. id:[..]`·`empty arena:`·`DONE and Waiting forever`로 완료 판정 ⑤ s1은 `connect()`로 붙여 node ID와 작은 payload 공유를 확인. 이후 manager 유지. **실험 중 재기동 금지**(= 슬라이스 재초기화).
- `cxl_clear_ucsd`(64 GiB memset)는 일반 시작 절차에 넣지 않는다 — 명시적 전체 초기화가 필요할 때만.
- 우리 프로세스: `LD_LIBRARY_PATH=$HOME/work/lib`, `cxl_shm_connect()`(s2 추가 프로세스, s1), `my_nid()` 확인, 모든 offset은 slice-relative, 64 GiB 상한을 우리가 검사.

## 9. fixed-shared-memory-api.md / capability-audit 갱신 요약

| 항목 | 이전 | 확정 |
| --- | --- | --- |
| offset origin | BLOCKED (slice-relative vs device) | **slice-relative** (dax_base = 장치 64 GiB) |
| visibility primitive | BLOCKED (미노출) | **export됨**: clflush/clwb region 함수 3종 |
| lock handle ABI | BLOCKED | **8 B 값 전달**(`{volatile uint64 lockptr}`), lockptr 공유 가능 |
| start_server 범위 | 미확인 | 로컬 bootstrap 삭제 + node0 rank0 init(메타 138 MB zero, 10 GiB 0xAA) |
| clear 도구 | 미확인 | `cxl_clear_ucsd` = [64 GiB, 128 GiB) memset |
| crash recovery(dead writer slot) | BLOCKED | 여전히 미구현(데모 범위 밖: 실패 시 clear 후 cold restart) |
