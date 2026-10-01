# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0
"""Tests for the reaper thread and concurrent database access."""

import os
import shutil
import tempfile
import threading
import time
from unittest import mock

from avocado import Test
from fastapi.testclient import TestClient

from seine.distributed.server.api import create_app
from seine.distributed.server.db import Database


def _register(db, worker_id):
    db.upsert_worker(
        worker_id=worker_id, hostname=worker_id, native_arch="amd64",
        arch_scores={"amd64": 1.0}, free_disk_gb=50.0, token=f"tok-{worker_id}",
    )


class ReaperTest(Test):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="seine-test-reaper-")
        self.db = Database(os.path.join(self.tmp_dir, "t.db"))

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _stale_job(self):
        _register(self.db, "w1")
        self.db.create_build("bld-1", "alpha", "amd64", "digest")
        job = self.db.claim_next_job("w1")
        self.db.conn.execute("UPDATE workers SET last_seen = ? WHERE id = 'w1'", (time.time() - 3600,))
        self.db.conn.commit()
        return job["job_id"]

    def _wait_for(self, predicate, timeout=10.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return False

    def test_thread_requeues_job_of_stale_worker(self):
        job_id = self._stale_job()
        app = create_app(db=self.db, enrollment_token="t", stale_after=60.0, reap_interval=0.05)
        with TestClient(app):
            self.assertTrue(self._wait_for(
                lambda: self.db.builds.get_job(job_id)["status"] == "queued"
            ))
        self.assertEqual(self.db.get_worker("w1")["status"], "offline")

    def test_thread_stops_on_shutdown(self):
        app = create_app(db=self.db, enrollment_token="t", reap_interval=0.05)
        with TestClient(app):
            self.assertTrue(any(t.name == "seine-reaper" for t in threading.enumerate()))
        self.assertFalse(any(t.name == "seine-reaper" for t in threading.enumerate()))

    def test_no_thread_without_lifespan(self):
        create_app(db=self.db, enrollment_token="t", reap_interval=0.05)
        self.assertFalse(any(t.name == "seine-reaper" for t in threading.enumerate()))


class ParallelApiTest(Test):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="seine-test-parallel-")

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_api_requests_in_parallel(self):
        self.db = db = Database(os.path.join(self.tmp_dir, "a.db"))
        db.projects.create("alpha")
        db.users.create("alice")
        db.projects.add_member("alpha", "alice", role="developer")
        token = db.tokens.issue(user_id="alice", kind="pat")["token"]
        client = TestClient(create_app(db=db, enrollment_token="t", storage_provider=mock.MagicMock()))
        codes = []

        def submit():
            for _ in range(5):
                r = client.post(
                    "/api/v1/builds",
                    json={"project": "alpha", "target_arch": "amd64", "worktree_digest": "d"},
                    headers={"Authorization": f"Bearer {token}"},
                )
                codes.append(r.status_code)

        threads = [threading.Thread(target=submit) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(set(codes), {200})
        self.assertEqual(len(db.builds.list()), 30)
