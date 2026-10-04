# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0
"""Tests for the S3 keys the server hands to workers, per project and environment."""

import logging
import os
import shutil
import sqlite3
import tempfile

from avocado import Test
from fastapi.testclient import TestClient

from seine.distributed.common.models import ClaimJobRequest, JobManifest
from seine.distributed.server.api import create_app
from seine.distributed.server.db import Database
from seine.distributed.server.settings import Settings

SECRETS = ("a-dev-secret", "a-prod-secret", "def-dev-secret", "GKa-dev", "GKa-prod")


def settings(**extra):
    values = {
        "s3_endpoint": "https://s3.test",
        "s3_region": "lan",
        "s3_projects": {"alpha": {
            "dev": {"access_key": "GKa-dev", "secret_key": "a-dev-secret"},
            "prod": {"access_key": "GKa-prod", "secret_key": "a-prod-secret"},
        }},
    }
    return Settings(**{**values, **extra})


class JobS3Test(Test):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="seine-test-job-s3-")
        self.db = Database(os.path.join(self.tmp_dir, "test.db"))
        self.app = create_app(db=self.db, settings=settings())
        self.client = TestClient(self.app)
        self.db.upsert_worker(
            worker_id="w1", hostname="h", native_arch="amd64", arch_scores={"amd64": 1.0},
            free_disk_gb=50.0, token="wtok-1", concurrency_slots=2,
        )
        self.db.upsert_worker(
            worker_id="w2", hostname="h2", native_arch="amd64", arch_scores={"amd64": 1.0},
            free_disk_gb=50.0, token="wtok-2",
        )
        self.db.ensure_project("alpha", dev_bucket="alpha-dev", prod_bucket="alpha-prod")
        self.db.ensure_project("orphan")
        self.db.users.create("dev")
        for name in ("alpha", "orphan"):
            self.db.projects.add_member(name, "dev", role="releaser")
        self.dev_headers = {"Authorization": f"Bearer {self.db.tokens.issue(user_id='dev')['token']}"}

    def tearDown(self):
        if hasattr(self, "handler"):
            logging.getLogger().removeHandler(self.handler)
        self.db.close()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _claim(self, worker="w1"):
        token = {"w1": "wtok-1", "w2": "wtok-2"}[worker]
        return self.client.post(
            "/api/v1/workers/claim",
            json=ClaimJobRequest(worker_id=worker).model_dump(),
            headers={"Authorization": f"Bearer {token}"},
        )

    def _build(self, project="alpha", is_release=False, build_id=None, options=None):
        build_id = build_id or f"bld-{project}-{int(is_release)}"
        self.db.create_build(
            build_id=build_id, project=project, target_arch="amd64",
            worktree_digest="tree", is_release=is_release, options=options,
        )
        return build_id

    def _shared_cache(self, is_release, options=None):
        self._build(is_release=is_release, options=options)
        return self._claim().json()["options"]["shared_cache"]

    def test_shared_cache_defaults_on_for_dev_and_off_for_release(self):
        self.assertTrue(self._shared_cache(False))

    def test_release_build_defaults_to_a_clean_build(self):
        self.assertFalse(self._shared_cache(True))

    def test_explicit_shared_cache_flag_wins_over_the_default(self):
        self.assertFalse(self._shared_cache(False, {"shared_cache": False}))

    def test_explicit_shared_cache_on_a_release_build(self):
        self.assertTrue(self._shared_cache(True, {"shared_cache": True}))

    def test_dev_build_gets_the_dev_key_and_bucket_only(self):
        self._build()
        resp = self._claim()
        self.assertEqual(resp.status_code, 200)
        s3 = resp.json()["storage"]
        self.assertEqual(s3, {
            "type": "s3",
            "endpoint": "https://s3.test", "region": "lan", "bucket": "alpha-dev",
            "access_key": "GKa-dev", "secret_key": "a-dev-secret",
        })
        self.assertNotIn("a-prod-secret", resp.text)
        self.assertNotIn("GKa-prod", resp.text)

    def test_release_build_gets_the_prod_key_and_never_the_dev_key(self):
        self._build(is_release=True)
        resp = self._claim()
        self.assertEqual(resp.status_code, 200)
        s3 = resp.json()["storage"]
        self.assertEqual((s3["bucket"], s3["access_key"], s3["secret_key"]),
                         ("alpha-prod", "GKa-prod", "a-prod-secret"))
        self.assertNotIn("a-dev-secret", resp.text)
        self.assertNotIn("GKa-dev", resp.text)

    def test_project_without_entry_uses_the_default_pair(self):
        self.app.state.settings = settings(s3_default={
            "dev": {"access_key": "GKdef", "secret_key": "def-dev-secret"},
        })
        self._build("orphan")
        self.assertEqual(self._claim().json()["storage"]["access_key"], "GKdef")

    def test_project_without_credentials_fails_the_build_with_a_reason(self):
        build_id = self._build("orphan")
        resp = self._claim()
        self.assertEqual(resp.status_code, 204)
        build = self.db.get_build(build_id)
        self.assertEqual(build["status"], "failed")
        self.assertIn("no S3 dev credentials configured for project 'orphan'", build["error_message"])
        job = self.db.conn.execute("SELECT status FROM jobs WHERE build_id = ?", (build_id,)).fetchone()
        self.assertEqual(job["status"], "failed")
        self.assertEqual(self._claim().status_code, 204)

    def test_release_without_a_prod_pair_fails_even_if_dev_exists(self):
        self.app.state.settings = settings(s3_projects={
            "alpha": {"dev": {"access_key": "GKa-dev", "secret_key": "a-dev-secret"}},
        })
        build_id = self._build(is_release=True)
        self.assertEqual(self._claim().status_code, 204)
        build = self.db.get_build(build_id)
        self.assertEqual(build["status"], "failed")
        self.assertIn("prod", build["error_message"])
        self.assertNotIn("a-dev-secret", build["error_message"])

    def test_failed_claim_drops_the_transient_secrets(self):
        build_id = self._build("orphan")
        self.app.state.transient_secrets[build_id] = {"k": "v"}
        self._claim()
        self.assertNotIn(build_id, self.app.state.transient_secrets)

    def test_only_the_owning_worker_receives_the_keys(self):
        self._build()
        self.assertEqual(self._claim("w1").status_code, 200)
        other = self._claim("w2")
        self.assertEqual(other.status_code, 204)
        for secret in SECRETS:
            self.assertNotIn(secret, other.text)

    def test_keys_stay_out_of_the_database_the_build_response_and_the_logs(self):
        build_id = self._build()
        records = []
        handler = logging.Handler()
        handler.emit = records.append
        logging.getLogger().addHandler(handler)
        self.handler = handler
        logging.getLogger().setLevel(logging.DEBUG)

        self.assertEqual(self._claim().status_code, 200)
        self._build("orphan")
        self._claim()
        build = self.client.get(f"/api/v1/builds/{build_id}", headers=self.dev_headers)
        self.assertEqual(build.status_code, 200)

        dump = "\n".join(sqlite3.connect(os.path.join(self.tmp_dir, "test.db")).iterdump())
        logged = "\n".join(r.getMessage() for r in records)
        for secret in SECRETS:
            self.assertNotIn(secret, dump)
            self.assertNotIn(secret, build.text)
            self.assertNotIn(secret, logged)

    def test_manifest_repr_hides_the_keys(self):
        self._build()
        manifest = JobManifest(**self._claim().json())
        for secret in SECRETS:
            self.assertNotIn(secret, repr(manifest))
