# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0
"""Tests for concurrent job claiming across threads."""

import os
import shutil
import tempfile
import threading

from avocado import Test

from seine.distributed.server.db import Database


class ConcurrentClaimTest(Test):
    THREADS = 8
    BUILDS = 5

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="seine-test-claims-")
        self.db = Database(os.path.join(self.tmp_dir, "c.db"))

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_threads_submit_and_claim_without_double_claims(self):
        errors, claimed = [], []
        lock = threading.Lock()
        for n in range(self.THREADS):
            self.db.upsert_worker(
                worker_id=f"w{n}", hostname=f"w{n}", native_arch="amd64",
                arch_scores={"amd64": 1.0}, free_disk_gb=50.0, token=f"tok-{n}",
            )

        def work(n):
            try:
                for i in range(self.BUILDS):
                    self.db.create_build(f"bld-{n}-{i}", "alpha", "amd64", "digest")
                while (job := self.db.claim_next_job(f"w{n}")) is not None:
                    with lock:
                        claimed.append(job["job_id"])
            except Exception as e:
                errors.append(repr(e))

        threads = [threading.Thread(target=work, args=(n,)) for n in range(self.THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        # Each worker has one slot, so each holds exactly one job.
        self.assertEqual(len(claimed), self.THREADS)
        self.assertEqual(len(set(claimed)), self.THREADS, "a job was claimed twice")
