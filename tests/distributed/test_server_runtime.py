# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0
"""Tests for the reaper thread and concurrent database access."""

import io
import logging
import os
import shutil
import tempfile
import threading
import time
from unittest import mock

from avocado import Test
from fastapi.testclient import TestClient

from seine.distributed.server.api import create_app
from seine.distributed.server.cli import configure_logging
from seine.distributed.server.db import Database
from seine.distributed.server.housekeeping import HousekeepingBusy, UnknownProject
from seine.distributed.server.reaper import Reaper


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


class HousekeepingTriggerTest(Test):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="seine-test-hk-")
        self.db = Database(os.path.join(self.tmp_dir, "t.db"))
        self.calls = []
        self.behaviour = lambda project: None
        self.reapers = []
        self.patcher = mock.patch("seine.distributed.server.reaper.run_housekeeping", self._fake)
        self.patcher.start()

    def tearDown(self):
        for reaper in self.reapers:
            reaper.stop()
        self.patcher.stop()
        self.db.close()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _fake(self, db, settings, project=None, **kwargs):
        self.calls.append(project)
        self.behaviour(project)
        return []

    def _start(self, interval=3600.0, retention=True, reap_interval=3600.0):
        settings = mock.Mock()
        settings.retention = mock.Mock(interval=interval) if retention else None
        reaper = Reaper(self.db, 60.0, reap_interval, settings=settings)
        reaper.start()
        self.reapers.append(reaper)
        return reaper

    def _wait_for(self, predicate, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        return False

    def test_nothing_without_retention(self):
        reaper = self._start(interval=0.05, retention=False)
        reaper.request_housekeeping("alpha")
        time.sleep(0.2)
        self.assertEqual(self.calls, [])
        self.assertFalse(any(t.name == "seine-housekeeping" for t in threading.enumerate()))

    def test_periodic_sweep_runs_every_interval(self):
        self._start(interval=0.1)
        self.assertEqual(self.calls, [])
        self.assertTrue(self._wait_for(lambda: self.calls.count(None) >= 2))

    def test_request_sweeps_that_project_only_and_coalesces(self):
        release = threading.Event()
        self.behaviour = lambda project: release.wait(5)
        reaper = self._start()
        reaper.request_housekeeping("alpha")
        self.assertTrue(self._wait_for(lambda: self.calls == ["alpha"]))
        reaper.request_housekeeping("beta")
        reaper.request_housekeeping("beta")
        release.set()
        self.assertTrue(self._wait_for(lambda: self.calls == ["alpha", "beta"]))
        time.sleep(0.1)
        self.assertEqual(self.calls, ["alpha", "beta"])

    def test_errors_do_not_kill_the_thread(self):
        outcomes = [HousekeepingBusy("busy"), RuntimeError("boom")]

        def behaviour(project):
            if outcomes:
                raise outcomes.pop(0)

        self.behaviour = behaviour
        reaper = self._start()
        reaper.request_housekeeping("alpha")
        self.assertTrue(self._wait_for(lambda: len(self.calls) == 1))
        # the busy project stays pending and is retried on the next wake
        reaper.request_housekeeping("beta")
        self.assertTrue(self._wait_for(lambda: len(self.calls) == 3))
        self.assertEqual(self.calls, ["alpha", "alpha", "beta"])
        reaper.request_housekeeping("gamma")
        self.assertTrue(self._wait_for(lambda: self.calls[-1] == "gamma"))

    def test_a_project_deleted_before_its_sweep_is_dropped_quietly(self):
        self.behaviour = mock.Mock(side_effect=UnknownProject("unknown project: alpha"))
        reaper = self._start()
        with self.assertLogs("seine.server.reaper", "INFO") as logs:
            reaper.request_housekeeping("alpha")
            self.assertTrue(self._wait_for(lambda: self.calls == ["alpha"]))
            self.assertTrue(self._wait_for(lambda: logs.records))
        self.assertEqual([r.levelname for r in logs.records], ["INFO"])
        self.assertEqual(self.calls, ["alpha"])
        self.assertEqual(reaper._pending, set())

    def test_workers_are_reaped_while_housekeeping_is_slow(self):
        release = threading.Event()
        self.behaviour = lambda project: release.wait(5)
        _register(self.db, "w1")
        self.db.create_build("bld-1", "alpha", "amd64", "digest")
        job = self.db.claim_next_job("w1")
        self.db.conn.execute("UPDATE workers SET last_seen = ? WHERE id = 'w1'", (time.time() - 3600,))
        self.db.conn.commit()
        reaper = self._start(reap_interval=0.05)
        reaper.request_housekeeping("alpha")
        self.assertTrue(self._wait_for(lambda: self.calls == ["alpha"]))
        self.assertTrue(self._wait_for(
            lambda: self.db.builds.get_job(job["job_id"])["status"] == "queued"
        ))
        release.set()

    def test_stop_returns(self):
        reaper = self._start()
        reaper.stop()
        self.assertFalse(reaper.is_alive())
        self.assertFalse(reaper._housekeeper.is_alive())


    def test_stop_gives_up_on_a_sweep_that_will_not_end(self):
        release = threading.Event()
        self.behaviour = lambda project: release.wait(10)
        reaper = self._start()
        reaper.request_housekeeping("alpha")
        self.assertTrue(self._wait_for(lambda: self.calls == ["alpha"]))
        with mock.patch("seine.distributed.server.reaper._STOP_TIMEOUT", 0.1):
            with self.assertLogs("seine.server.reaper", "WARNING"):
                reaper.stop()
        self.assertTrue(reaper._housekeeper.is_alive())
        release.set()
        reaper._housekeeper.join(5)
        self.assertFalse(reaper._housekeeper.is_alive())


class HousekeepingHookTest(Test):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="seine-test-hk-hook-")
        self.db = Database(os.path.join(self.tmp_dir, "t.db"))
        _register(self.db, "w1")
        self.app = create_app(db=self.db, enrollment_token="t")
        self.app.state.reaper = mock.Mock()
        self.client = TestClient(self.app)

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _status(self, job, status):
        return self.client.post(
            f"/api/v1/workers/jobs/{job['job_id']}/status",
            json={"worker_id": "w1", "build_id": job["build_id"],
                  "job_id": job["job_id"], "status": status},
            headers={"Authorization": "Bearer tok-w1"},
        )

    def test_finished_build_requests_a_sweep_of_its_project(self):
        self.db.create_build("bld-1", "alpha", "amd64", "digest")
        job = self.db.claim_next_job("w1")
        self.assertEqual(self._status(job, "running").status_code, 200)
        self.app.state.reaper.request_housekeeping.assert_not_called()
        self.assertEqual(self._status(job, "completed").status_code, 200)
        self.app.state.reaper.request_housekeeping.assert_called_once_with("alpha")


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


class ConfigureLoggingTest(Test):
    def setUp(self):
        self.seine, self.root = logging.getLogger("seine"), logging.getLogger()
        self.saved = (self.seine.handlers, self.seine.level, self.root.handlers)
        self.seine.handlers, self.root.handlers = [], []

    def tearDown(self):
        self.seine.handlers, self.seine.level, self.root.handlers = self.saved

    def test_housekeeping_info_reaches_stderr(self):
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            configure_logging()
            configure_logging()
            logging.getLogger("seine.server.housekeeping").info("evicted bld-1")
        self.assertEqual(len(self.seine.handlers), 1)
        self.assertEqual(err.getvalue(), "INFO seine.server.housekeeping: evicted bld-1\n")

    def test_level_is_applied(self):
        configure_logging("warning")
        self.assertEqual(self.seine.level, logging.WARNING)

    def test_existing_root_handler_is_left_alone(self):
        self.root.handlers = [logging.NullHandler()]
        configure_logging()
        self.assertEqual(self.seine.handlers, [])
        self.assertEqual(self.seine.level, logging.INFO)
