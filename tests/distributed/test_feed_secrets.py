# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0
"""Tests for the feed credentials a build keeps on the server, in memory and for a while."""

import logging
import os
import shutil
import sqlite3
import tempfile
from unittest import mock

from avocado import Test
from fastapi.testclient import TestClient

from seine.distributed.common.models import BuildSubmitRequest, JobManifest
from seine.distributed.server import validation
from seine.distributed.server.api import create_app
from seine.distributed.server.db import Database
from seine.distributed.server.reaper import Reaper
from seine.distributed.server.settings import Settings, SettingsError
from seine.distributed.server.transient import TransientSecrets

URI = "https://repo.example/debian"
PASSWORD = "hunter2-feed-password"
FEEDS = {"feeds": {URI: {"login": "alice-login", "password": PASSWORD}}}
LEAKS = ("alice-login", PASSWORD)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class ShapeTest(Test):
    def check(self, secrets):
        validation.check_transient_secrets(secrets)

    def test_good_and_empty_shapes_pass(self):
        self.check(FEEDS)
        self.check({})

    def test_bad_shapes_are_refused(self):
        pair = {"login": "l", "password": "p"}
        bad = [
            {"feeds": {URI: pair}, "extra": {}},
            {"apt_token": "x"},
            {"feeds": []},
            {"feeds": {}},
            {"feeds": {URI: {"login": "l"}}},
            {"feeds": {URI: dict(pair, extra="x")}},
            {"feeds": {URI: {"login": "", "password": "p"}}},
            {"feeds": {URI: {"login": "l", "password": 1}}},
            {"feeds": {URI: {"login": "l", "password": "p" * 4097}}},
            {"feeds": {"": pair}},
            {"feeds": {"u" * 4097: pair}},
            {"feeds": {URI: "secret"}},
            {"feeds": {f"https://h{i}/": pair for i in range(65)}},
        ]
        for secrets in bad:
            with self.assertRaises(ValueError, msg=str(secrets)[:60]):
                self.check(secrets)

    def test_limits_are_inclusive(self):
        self.check({"feeds": {f"https://h{i}/": {"login": "l" * 4096, "password": "p" * 4096}
                              for i in range(64)}})


class StoreTest(Test):
    def setUp(self):
        self.clock = Clock()
        self.store = TransientSecrets(ttl=60, clock=self.clock)

    def test_secrets_expire_and_are_never_handed_out_afterwards(self):
        self.store["bld-1"] = FEEDS
        self.clock.now += 59
        self.assertEqual(self.store.get("bld-1"), FEEDS)
        self.clock.now += 1
        self.assertIsNone(self.store.get("bld-1"))
        self.assertNotIn("bld-1", self.store)
        self.assertEqual(len(self.store), 0)

    def test_purge_expired_keeps_the_fresh_ones(self):
        self.store["old"] = FEEDS
        self.clock.now += 30
        self.store["new"] = FEEDS
        self.clock.now += 31
        self.assertEqual(self.store.purge_expired(), 1)
        self.assertEqual(list(self.store.build_ids()), ["new"])

    def test_pop_of_an_expired_entry_yields_nothing(self):
        self.store["bld-1"] = FEEDS
        self.clock.now += 61
        self.assertIsNone(self.store.pop("bld-1"))

    def test_repr_shows_no_secret(self):
        self.store["bld-1"] = FEEDS
        for leak in LEAKS:
            self.assertNotIn(leak, repr(self.store))


class SettingsTest(Test):
    def setUp(self):
        self.db = Database(":memory:")

    def tearDown(self):
        self.db.close()

    def test_default_is_six_hours(self):
        self.assertEqual(Settings.load(env={}).secret_ttl, 6 * 3600.0)

    def test_environment_sets_it(self):
        self.assertEqual(Settings.load(env={"SEINE_SECRET_TTL": "90"}).secret_ttl, 90.0)

    def test_it_must_be_positive(self):
        with self.assertRaises(SettingsError):
            Settings(enrollment_token="t", secret_ttl=0).validate()

    def test_the_app_stores_secrets_for_that_long(self):
        app = create_app(db=self.db, settings=Settings(secret_ttl=42))
        self.assertEqual(app.state.transient_secrets.ttl, 42)


class ServerTest(Test):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="seine-test-feed-secrets-")
        self.db_path = os.path.join(self.tmp_dir, "test.db")
        self.db = Database(self.db_path)
        self.clock = Clock()
        self.app = create_app(db=self.db, settings=Settings(
            s3_endpoint="https://s3.test",
            s3_default={"dev": {"access_key": "GKdev", "secret_key": "dev-secret"}},
        ), storage_provider=mock.MagicMock())
        self.app.state.transient_secrets = TransientSecrets(60, clock=self.clock)
        self.client = TestClient(self.app)
        self.db.upsert_worker(
            worker_id="w1", hostname="h", native_arch="amd64", arch_scores={"amd64": 1.0},
            free_disk_gb=50.0, token="wtok", concurrency_slots=2,
        )
        self.db.ensure_project("proj", dev_bucket="proj-dev", prod_bucket="proj-prod")
        self.db.users.create("dev")
        self.db.projects.add_member("proj", "dev", role="developer")
        token = self.db.tokens.issue(user_id="dev")["token"]
        self.user = {"Authorization": f"Bearer {token}"}
        self.worker = {"Authorization": "Bearer wtok"}

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def submit(self, secrets=None):
        body = {"project": "proj", "worktree_digest": "tree"}
        if secrets is not None:
            body["transient_secrets"] = secrets
        return self.client.post("/api/v1/builds", json=body, headers=self.user)

    def claim(self):
        return self.client.post("/api/v1/workers/claim", json={"worker_id": "w1"}, headers=self.worker)

    def job_status(self, manifest, status):
        return self.client.post(
            f"/api/v1/workers/jobs/{manifest['job_id']}/status",
            json={"worker_id": "w1", "build_id": manifest["build_id"],
                  "job_id": manifest["job_id"], "status": status},
            headers=self.worker,
        )

    def stored(self, build_id):
        return build_id in self.app.state.transient_secrets

    def test_bad_shapes_get_400_and_create_no_build(self):
        for secrets in ({"other": {}}, {"feeds": {URI: {"login": "l"}}}, {"feeds": {}, "x": 1}):
            resp = self.submit(secrets)
            self.assertEqual(resp.status_code, 400, secrets)
        self.assertEqual(self.db.builds.list(), [])

    def test_a_good_submission_is_kept_in_memory(self):
        build_id = self.submit(FEEDS).json()["build_id"]
        self.assertTrue(self.stored(build_id))

    def test_a_build_without_secrets_stores_nothing(self):
        build_id = self.submit().json()["build_id"]
        self.assertFalse(self.stored(build_id))

    def test_the_claim_hands_them_out_until_they_expire(self):
        self.submit(FEEDS)
        self.clock.now += 61
        manifest = self.claim().json()
        self.assertEqual(manifest["transient_secrets"], {})

    def test_the_claim_hands_out_fresh_secrets(self):
        self.submit(FEEDS)
        self.assertEqual(self.claim().json()["transient_secrets"], FEEDS)

    def test_every_job_of_a_build_gets_them_until_the_build_ends(self):
        build_id = self.submit(FEEDS).json()["build_id"]
        self.db.builds.create_job(id="job-extra", build_id=build_id, kind="package", package_name="p")
        first = self.claim().json()
        self.job_status(first, "completed")
        self.assertTrue(self.stored(build_id))
        second = self.claim().json()
        self.assertEqual(second["transient_secrets"], FEEDS)
        self.job_status(second, "completed")
        self.assertFalse(self.stored(build_id))

    def test_completion_failure_and_cancellation_drop_them(self):
        for status in ("completed", "failed", "cancelled"):
            build_id = self.submit(FEEDS).json()["build_id"]
            self.job_status(self.claim().json(), status)
            self.assertFalse(self.stored(build_id), status)

    def test_cancelling_a_queued_build_drops_them(self):
        build_id = self.submit(FEEDS).json()["build_id"]
        resp = self.client.post(f"/api/v1/builds/{build_id}/cancel", headers=self.user)
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(self.stored(build_id))

    def test_a_failed_claim_drops_them(self):
        self.app.state.settings = Settings(s3_endpoint="https://s3.test")
        build_id = self.submit(FEEDS).json()["build_id"]
        self.assertEqual(self.claim().status_code, 204)
        self.assertFalse(self.stored(build_id))

    def test_the_reaper_drops_expired_secrets_and_those_of_finished_builds(self):
        expiring = self.submit(FEEDS).json()["build_id"]
        self.clock.now += 61
        fresh = self.submit(FEEDS).json()["build_id"]
        finished = self.submit(FEEDS).json()["build_id"]
        self.db.builds.update_status(finished, "failed")
        Reaper(self.db, 120, 30, self.app.state.transient_secrets).drop_stale_secrets()
        self.assertFalse(self.stored(expiring))
        self.assertFalse(self.stored(finished))
        self.assertTrue(self.stored(fresh))

    def test_nothing_reaches_the_database_a_response_or_a_log(self):
        with self.assertLogs(level=logging.DEBUG) as logs:
            logging.getLogger("seine.test").debug("start")
            resp = self.submit(FEEDS)
            build_id = resp.json()["build_id"]
            self.client.post("/api/v1/builds", json={"project": "proj", "transient_secrets": FEEDS},
                             headers=self.user)
            texts = [resp.text, self.client.get(f"/api/v1/builds/{build_id}", headers=self.user).text,
                     self.client.get("/api/v1/builds", headers=self.user).text]
            manifest = self.claim()
            self.assertEqual(manifest.status_code, 200)
            self.job_status(manifest.json(), "completed")
        dump = "\n".join(sqlite3.connect(self.db_path).iterdump())
        for name in os.listdir(self.tmp_dir):
            with open(os.path.join(self.tmp_dir, name), "rb") as f:
                dump += f.read().decode("latin-1")
        for leak in LEAKS:
            for text in (*texts, dump, "\n".join(logs.output)):
                self.assertNotIn(leak, text)

    def test_a_rejected_request_does_not_echo_the_secrets(self):
        resp = self.client.post("/api/v1/builds", json={"project": "proj", "transient_secrets": FEEDS},
                                headers=self.user)
        self.assertEqual(resp.status_code, 422)
        for leak in LEAKS:
            self.assertNotIn(leak, resp.text)

    def test_reprs_show_no_secret(self):
        request = BuildSubmitRequest(project="p", worktree_digest="t", transient_secrets=FEEDS)
        manifest = JobManifest(job_id="j", build_id="b", project="p", transient_secrets=FEEDS)
        for obj in (request, manifest):
            for leak in LEAKS:
                self.assertNotIn(leak, repr(obj))
                self.assertNotIn(leak, str(obj))
