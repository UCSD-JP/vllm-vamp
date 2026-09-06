// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the vLLM project
// Minimal cross-host visibility check on the provider API, built with the provider's
// own header/library (no provider code modified).
//   writer (node 0, s2):  cxl_ping write <nbytes> <seed>
//   reader (node 1, s1):  cxl_ping read  <nbytes> <seed>
// Protocol: one shared record {magic, generation, nbytes, seed, checksum, lockptr, payload_off}
// published under key VAMP_PING. Writer: shmalloc record+lock, shm_payload_alloc payload,
// fill pattern(seed), fence(payload), lock: write record, fence(record), unlock, put(key).
// Reader: connect, get(key), rebuild lock from lockptr, lock: refresh(record), read record,
// unlock, refresh(payload), checksum payload, compare. Both print my_nid().
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <unistd.h>
#include <time.h>
#include "api.h"
#include "cacheline.h"

// my_nid() is not exported; the exported global my_id holds {nid, rid, pid} as uint16 (my_nid reads its first halfword)
extern __thread uint16_t my_id[3];   // thread-local in the library (.tdata)
#define my_nid() (my_id[0])

#define KEY "VAMP_PING"
#define MAGIC 0x56414d5031ULL
typedef struct __attribute__((aligned(64))) {
    volatile uint64_t magic, generation, nbytes, seed, checksum, lockptr, payload_off, state; // state 1=READY
} record_t;

static uint64_t fnv1a(const volatile unsigned char *p, size_t n) {
    uint64_t h = 1469598103934665603ULL;
    for (size_t i = 0; i < n; i++) { h ^= p[i]; h *= 1099511628211ULL; }
    return h;
}
static double now_s(void) { struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t); return t.tv_sec + t.tv_nsec * 1e-9; }

int main(int argc, char **argv) {
    if (argc < 4) { fprintf(stderr, "usage: %s write|read <nbytes> <seed>\n", argv[0]); return 2; }
    const char *mode = argv[1]; size_t n = strtoull(argv[2], 0, 0); uint64_t seed = strtoull(argv[3], 0, 0);
    double t0 = now_s();
    if (cxl_shm_connect() < 0) { fprintf(stderr, "connect failed\n"); return 1; }
    printf("connect_s=%.3f nid=%u\n", now_s() - t0, my_nid());

    if (!strcmp(mode, "write")) {
        record_t *rec = shmalloc(sizeof *rec);
        unsigned char *payload = shm_payload_alloc(n);
        if (!rec || !payload) { fprintf(stderr, "alloc failed rec=%p payload=%p\n", (void*)rec, (void*)payload); return 1; }
        cxl_lock_t lock; if (cxl_shm_allocate_lock(&lock) != 0) { fprintf(stderr, "allocate_lock failed\n"); return 1; }
        uint64_t x = seed; for (size_t i = 0; i < n; i++) { x = x * 6364136223846793005ULL + 1442695040888963407ULL; payload[i] = (unsigned char)(x >> 56); }
        uint64_t ck = fnv1a(payload, n);
        double t1 = now_s(); clwb_region_with_barrier(payload, n); double t_fence = now_s() - t1;
        cxl_shm_lock_acquire(lock);
        rec->magic = MAGIC; rec->generation = 1; rec->nbytes = n; rec->seed = seed; rec->checksum = ck;
        rec->lockptr = lock.lockptr; rec->payload_off = cxl_shm_get_offset(payload); rec->state = 1;
        clflush_region_with_mfence(rec, sizeof *rec);
        cxl_shm_lock_release(lock);
        if (cxl_shm_put(KEY, rec) != 0) { fprintf(stderr, "put failed\n"); return 1; }
        printf("WRITE_OK nbytes=%zu checksum=%016llx rec_off=%llu payload_off=%llu lockptr=%llu fence_s=%.4f\n",
               n, (unsigned long long)ck, (unsigned long long)cxl_shm_get_offset(rec),
               (unsigned long long)rec->payload_off, (unsigned long long)lock.lockptr, t_fence);
        return 0;
    }
    if (!strcmp(mode, "read")) {
        void *addr = NULL; int tries = 0;
        while (cxl_shm_get(KEY, &addr) != 0) { if (++tries > 600) { fprintf(stderr, "key not found after 60 s\n"); return 1; } usleep(100000); }
        record_t *rec = addr;
        clflush_region_with_mfence(rec, sizeof *rec);            // refresh header before reading lockptr
        cxl_lock_t lock; lock.lockptr = rec->lockptr;
        double t1 = now_s(); cxl_shm_lock_acquire(lock); double t_lock = now_s() - t1;
        clflush_region_with_mfence(rec, sizeof *rec);            // refresh under lock
        uint64_t magic = rec->magic, gen = rec->generation, nb = rec->nbytes, ck = rec->checksum, poff = rec->payload_off, st = rec->state;
        cxl_shm_lock_release(lock);
        if (magic != MAGIC || st != 1 || nb != n) { printf("READ_BAD magic=%llx state=%llu nbytes=%llu\n", (unsigned long long)magic, (unsigned long long)st, (unsigned long long)nb); return 1; }
        unsigned char *payload = cxl_shm_get_ptr(poff);
        double t2 = now_s(); clflush_region_with_mfence(payload, nb); double t_refresh = now_s() - t2;
        uint64_t mine = fnv1a(payload, nb);
        printf("%s nbytes=%llu gen=%llu remote_checksum=%016llx local_checksum=%016llx lock_wait_s=%.4f refresh_s=%.4f seed_ok=%d\n",
               mine == ck ? "READ_OK" : "READ_MISMATCH", (unsigned long long)nb, (unsigned long long)gen,
               (unsigned long long)ck, (unsigned long long)mine, t_lock, t_refresh, rec->seed == seed);
        return mine == ck ? 0 : 1;
    }
    fprintf(stderr, "unknown mode\n"); return 2;
}
