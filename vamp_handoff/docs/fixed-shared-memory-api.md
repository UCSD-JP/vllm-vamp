# Fixed External Shared-Memory Contract

The provider code and API/ABI are fixed. They are NOT owned or modified here.
No provider source, header, binary, manager, or build files are vendored.

This is our integration note, not a replacement header or a new API proposal.
Names and behavior below were read from the provided interface on 2026-09-05;
verify the deployed ABI against that external interface before loading it.

## Existing Operations

| Family | Existing operations | Adapter implications |
| --- | --- | --- |
| Lifecycle | cxl_shm_init/connect/finalize/is_initialized | Initialization is privileged operational coordination, not a unit-test side effect |
| Key reference | cxl_shm_put/get/get_locked/destroy | Maps keys to allocated objects; not automatically an async KV payload transport |
| Allocation | shmalloc/shfree; chunk and payload alloc/free | Use matching allocation/free family; obey provider ownership semantics |
| Locks | allocate/free/acquire/release lock | Inter-host ordering is not a substitute for visibility completion |
| Address conversion | cxl_shm_get_offset/get_ptr | Persist offsets, not process-local addresses |

The provided key-reference put takes a local pointer to a shmalloc-allocated
object, NOT an arbitrary uint64 offset argument. Get returns a local pointer
through an output parameter. The offset type is a 64-bit unsigned value.
Lock handles contain a shared offset but are passed by value to acquire/release;
allocation writes a handle through an output pointer. Do not guess ctypes types.

The destroy contract warns not to shfree the object first and requires freeing
object-associated locks first. Do not apply a generic free-then-destroy sequence.
The get_locked unlock/lifetime contract requires explicit clarification; do not
assume it provides the application's complete reader lease protocol.

## Our Responsibility

Our adapter owns application-level KV metadata, ModelKey/PrefixKey, generation
checks, bounded accounting, job completion, and import/export integration. It may
use the fixed allocation/lock API. This does not make the provider allocator or
manager a component we can patch.

Resolve offset origin, allowed range, visibility/fence primitives, destroy
ownership, and crash recovery before real memory access. Do not add a guessed
flush function to the provider API or assume a CPU flush implies GPU DMA safety.
If the fixed API cannot express a safe operation, mark that capability blocked.

## External Paths and Safety

On the existing testbed the library/header were provided under the operator's
work directory. At deployment accept external paths (for example CXL_SHM_LIBRARY
and CXL_SHM_INCLUDE_DIR); do not download, copy, or rebuild the provider code.
Offline development must run without these paths and without loading any library.

The reported UCSD device range is [64 GiB, 128 GiB). Manager coordination is on
node 0 (s2). Actual mapping origin and startup/clear procedures still require
operator confirmation. No automatic initialization, full-pool clear, or writes
to other tenants' regions are permitted.
