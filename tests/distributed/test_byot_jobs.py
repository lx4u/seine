#!/usr/bin/env python3
# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0
"""A build can bring its own Artifactory credential and run as its user, not as the server."""

import os
import shutil
import sys
import tempfile
from unittest import mock

import avocado
from fastapi.testclient import TestClient

path_to_self = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.insert(0, path_to_sources)

from seine.distributed.server import validation
from seine.distributed.server.api import create_app
from seine.distributed.server.db import Database
from seine.distributed.server.settings import Settings, SettingsError
from seine.distributed.server.transient import TransientSecrets
from seine.storage.artifactory import ArtifactoryError

OWN = {"artifactory": {"token": "user-tok"}}
OWN_BASIC = {"artifactory": {"user": "alice", "password": "alice-pw"}}


class ByotJobsTest(avocado.Test):

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="seine-test-byot-")
        self.db = Database(os.path.join(self.tmp_dir, "test.db"))
        self.db.upsert_worker(
            worker_id="w1", hostname="h", native_arch="amd64", arch_scores={"amd64": 1.0},
            free_disk_gb=50.0, token="wtok", concurrency_slots=2)
        self.db.ensure_project("proj", dev_bucket="proj-dev", prod_bucket="proj-prod")
        self.db.users.create("dev")
        self.db.projects.add_member("proj", "dev", role="developer")
        self.user = {"Authorization": "Bearer " + self.db.tokens.issue(user_id="dev")["token"]}
        self.worker = {"Authorization": "Bearer wtok"}
        self.check = mock.patch("seine.distributed.server.api.check_job_credentials")
        self.check_job = self.check.start()

    def tearDown(self):
        self.check.stop()
        self.db.close()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _client(self, **settings):
        values = dict(storage_type="artifactory", artifactory_endpoint="https://arti.lan",
                      artifactory_default={"dev": {"token": "server-tok"}, "prod": {"token": "server-tok"}})
        values.update(settings)
        app = create_app(db=self.db, settings=Settings(**values), storage_provider=mock.MagicMock())
        app.state.transient_secrets = TransientSecrets(60)
        return TestClient(app)

    def _submit(self, client, secrets=None):
        body = {"project": "proj", "worktree_digest": "tree"}
        if secrets is not None:
            body["transient_secrets"] = secrets
        return client.post("/api/v1/builds", json=body, headers=self.user)

    def _claim(self, client):
        return client.post("/api/v1/workers/claim", json={"worker_id": "w1"}, headers=self.worker)

    def test_profile_tells_a_client_what_storage_the_server_runs_and_no_secret(self):
        client = self._client(artifactory_job_tokens="required", artifactory_downloads="byot")
        resp = client.get("/api/v1/me", headers=self.user)
        self.assertEqual(resp.json()["storage"], {
            "type": "artifactory", "endpoint": "https://arti.lan",
            "job_tokens": "required", "downloads": "byot"})
        self.assertNotIn("server-tok", resp.text)

    def test_profile_of_an_s3_server_says_only_s3(self):
        resp = self._client(storage_type="s3").get("/api/v1/me", headers=self.user)
        self.assertEqual(resp.json()["storage"], {
            "type": "s3", "endpoint": None, "job_tokens": None, "downloads": None})

    def test_the_build_runs_with_the_users_token_not_the_servers(self):
        client = self._client()
        self.assertEqual(self._submit(client, OWN).status_code, 200)
        self.check_job.assert_called_once()
        self.assertEqual(self.check_job.call_args.args[1:], ("proj-dev", {"token": "user-tok"}))
        manifest = self._claim(client).json()
        self.assertEqual(manifest["storage"], {
            "type": "artifactory", "endpoint": "https://arti.lan", "bucket": "proj-dev",
            "token": "user-tok", "user": None, "password": None})
        self.assertEqual(manifest["transient_secrets"], {})
        self.assertNotIn("server-tok", str(manifest))

    def test_a_user_and_password_work_too(self):
        client = self._client()
        self._submit(client, OWN_BASIC)
        storage = self._claim(client).json()["storage"]
        self.assertEqual((storage["user"], storage["password"], storage["token"]), ("alice", "alice-pw", None))

    def test_without_a_token_the_build_runs_on_the_servers_credential(self):
        client = self._client()
        self.assertEqual(self._submit(client).status_code, 200)
        self.check_job.assert_not_called()
        self.assertEqual(self._claim(client).json()["storage"]["token"], "server-tok")

    def test_required_refuses_a_build_without_a_token_and_says_how_to_bring_one(self):
        client = self._client(artifactory_job_tokens="required")
        resp = self._submit(client)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("SEINE_ARTIFACTORY_TOKEN", resp.json()["detail"])
        self.assertEqual(self.db.builds.list(), [])

    def test_required_never_falls_back_when_the_secret_is_gone_by_the_claim(self):
        client = self._client(artifactory_job_tokens="required")
        build_id = self._submit(client, OWN).json()["build_id"]
        client.app.state.transient_secrets.pop(build_id)
        self.assertEqual(self._claim(client).status_code, 204)
        self.assertEqual(self.db.get_build(build_id)["status"], "failed")

    def test_a_token_that_cannot_use_the_repo_is_refused_before_the_build_exists(self):
        client = self._client()
        for code in (401, 403):
            self.check_job.side_effect = ArtifactoryError("denied", status_code=code)
            resp = self._submit(client, OWN)
            self.assertEqual(resp.status_code, 403, code)
            self.assertIn("proj-dev", resp.json()["detail"])
        self.assertEqual(self.db.builds.list(), [])

    def test_an_unreachable_artifactory_is_a_502_not_a_refusal(self):
        self.check_job.side_effect = ArtifactoryError("connection refused")
        self.assertEqual(self._submit(self._client(), OWN).status_code, 502)

    def test_an_s3_server_drops_the_credential_it_has_no_use_for(self):
        client = self._client(storage_type="s3", s3_endpoint="https://s3.test",
                              s3_default={"dev": {"access_key": "GK", "secret_key": "sk"}})
        build_id = self._submit(client, OWN).json()["build_id"]
        self.assertNotIn(build_id, client.app.state.transient_secrets)
        self.check_job.assert_not_called()

    def test_bad_shapes_are_400(self):
        client = self._client()
        for secrets in ({"artifactory": {"token": ""}}, {"artifactory": {"token": "t", "user": "u"}},
                        {"artifactory": {"user": "u"}}, {"artifactory": "tok"},
                        {"artifactory": {"token": "t" * 4097}}, {"artifactory": {"token": "t", "x": "y"}}):
            self.assertEqual(self._submit(client, secrets).status_code, 400, secrets)
        self.assertEqual(self.db.builds.list(), [])

    def test_artifactory_and_feeds_travel_together(self):
        feeds = {"feeds": {"https://h/": {"login": "l", "password": "p"}}}
        validation.check_transient_secrets({**feeds, **OWN})
        client = self._client()
        self._submit(client, {**feeds, **OWN})
        self.assertEqual(self._claim(client).json()["transient_secrets"], feeds)

    def test_settings_default_to_optional_and_reject_other_values(self):
        self.assertEqual(Settings().artifactory_job_tokens, "optional")
        with self.assertRaises(SettingsError):
            Settings(enrollment_token="x", artifactory_job_tokens="always").validate()


if __name__ == "__main__":
    avocado.main()
