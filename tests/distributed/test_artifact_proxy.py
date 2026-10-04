#!/usr/bin/env python3
# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0
"""The server streams Artifactory artifacts to clients that hold no storage credentials."""

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

from seine.distributed.server.api import create_app
from seine.distributed.server.db import Database
from seine.distributed.server.settings import Settings, SettingsError
from seine.storage.artifactory import ArtifactoryStorageProvider
from seine.storage.artifactory.client import ArtifactoryNotFoundError

PAYLOAD = b"disk-image-bytes"


def upstream(chunks=(PAYLOAD[:5], PAYLOAD[5:])):
    resp = mock.MagicMock()
    resp.iter_content.return_value = iter(chunks)
    return resp


class ArtifactProxyTest(avocado.Test):

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="seine-test-proxy-")
        self.db = Database(os.path.join(self.tmp_dir, "test.db"))
        self.provider = mock.MagicMock(spec=ArtifactoryStorageProvider)
        self.provider.generate_download_url.side_effect = (
            lambda project, key, expires_in=3600: f"https://arti.lan/seine-demo-dev/{key}")
        self.provider.open_artifact.return_value = upstream()
        self.db.ensure_project("demo")
        for user, role in (("alice", "developer"), ("mallory", None)):
            self.db.users.create(user)
            if role:
                self.db.projects.add_member("demo", user, role=role)
        self.headers = {user: {"Authorization": "Bearer " + self.db.tokens.issue(
            user_id=user, kind="pat")["token"]} for user in ("alice", "mallory")}
        self.db.builds.create(id="bld-1", project="demo", target_arch="amd64")
        self._complete("bld-1")

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _client(self, **settings):
        values = dict(storage_type="artifactory", artifactory_endpoint="https://arti.lan")
        values.update(settings)
        app = create_app(settings=Settings(**values), db=self.db, storage_provider=self.provider)
        return TestClient(app)

    def _complete(self, build_id):
        self.db.builds.update_status(
            build_id, "completed", artifact_urls=["k"],
            artifact_meta=[{"name": "disk image.raw", "size": len(PAYLOAD), "sha256": "ab" * 32,
                            "key": f"artifacts/demo/{build_id}/disk image.raw"}])

    def _get(self, client, user="alice", name="disk%20image.raw"):
        return client.get(f"/api/v1/builds/bld-1/artifacts/{name}", headers=self.headers.get(user))

    def test_proxy_is_the_default_and_hands_out_a_server_relative_url(self):
        resp = self._client().get("/api/v1/builds/bld-1", headers=self.headers["alice"])
        self.assertEqual(resp.json()["download_urls"],
                         {"disk image.raw": "/api/v1/builds/bld-1/artifacts/disk%20image.raw"})

    def test_direct_hands_out_the_storage_url(self):
        resp = self._client(artifactory_downloads="direct").get(
            "/api/v1/builds/bld-1", headers=self.headers["alice"])
        self.assertEqual(resp.json()["download_urls"], {
            "disk image.raw": "https://arti.lan/seine-demo-dev/artifacts/demo/bld-1/disk image.raw"})

    def test_byot_sends_the_storage_url_to_a_client_that_holds_its_own_credential(self):
        client = self._client(artifactory_downloads="byot")
        own = client.get("/api/v1/builds/bld-1", headers={**self.headers["alice"], "X-Seine-Own-Credential": "1"})
        self.assertEqual(own.json()["download_urls"], {
            "disk image.raw": "https://arti.lan/seine-demo-dev/artifacts/demo/bld-1/disk image.raw"})

    def test_byot_serves_any_other_client_through_the_server(self):
        resp = self._client(artifactory_downloads="byot").get("/api/v1/builds/bld-1", headers=self.headers["alice"])
        self.assertEqual(resp.json()["download_urls"],
                         {"disk image.raw": "/api/v1/builds/bld-1/artifacts/disk%20image.raw"})

    def test_the_marker_changes_nothing_in_proxy_mode(self):
        resp = self._client().get("/api/v1/builds/bld-1", headers={**self.headers["alice"], "X-Seine-Own-Credential": "1"})
        self.assertEqual(resp.json()["download_urls"],
                         {"disk image.raw": "/api/v1/builds/bld-1/artifacts/disk%20image.raw"})

    def test_s3_keeps_presigned_urls_whatever_the_setting(self):
        resp = self._client(storage_type="s3").get("/api/v1/builds/bld-1", headers=self.headers["alice"])
        self.assertTrue(resp.json()["download_urls"]["disk image.raw"].startswith("https://arti.lan/"))

    def test_streams_the_artifact_with_its_checksum(self):
        resp = self._get(self._client())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content, PAYLOAD)
        self.assertEqual(resp.headers["content-length"], str(len(PAYLOAD)))
        self.assertEqual(resp.headers["x-checksum-sha256"], "ab" * 32)
        self.provider.open_artifact.assert_called_once_with("demo", "artifacts/demo/bld-1/disk image.raw")

    def test_upstream_connection_is_closed_after_streaming(self):
        up = upstream()
        self.provider.open_artifact.return_value = up
        self._get(self._client())
        up.close.assert_called_once()

    def test_needs_a_token(self):
        self.assertEqual(self._get(self._client(), user=None).status_code, 401)

    def test_needs_membership_of_the_project(self):
        self.assertEqual(self._get(self._client(), user="mallory").status_code, 403)
        self.provider.open_artifact.assert_not_called()

    def test_unknown_build_or_artifact_is_404(self):
        client = self._client()
        self.assertEqual(self._get(client, name="other.raw").status_code, 404)
        resp = client.get("/api/v1/builds/bld-x/artifacts/a", headers=self.headers["alice"])
        self.assertEqual(resp.status_code, 404)

    def test_build_that_did_not_complete_has_no_artifacts(self):
        self.db.builds.create(id="bld-2", project="demo", target_arch="amd64")
        resp = self._client().get("/api/v1/builds/bld-2/artifacts/a", headers=self.headers["alice"])
        self.assertEqual(resp.status_code, 404)

    def test_expired_artifacts_are_gone(self):
        self.db.builds.mark_artifacts_expired("bld-1", "ttl")
        self.assertEqual(self._get(self._client()).status_code, 410)

    def test_object_missing_in_storage_is_404_and_other_errors_502(self):
        client = self._client()
        self.provider.open_artifact.side_effect = ArtifactoryNotFoundError("gone", status_code=404)
        self.assertEqual(self._get(client).status_code, 404)
        self.provider.open_artifact.side_effect = RuntimeError("boom")
        self.assertEqual(self._get(client).status_code, 502)

    def test_backend_without_a_proxy_answers_501(self):
        del self.provider.open_artifact
        self.assertEqual(self._get(self._client()).status_code, 501)

    def test_setting_defaults_to_proxy_and_rejects_unknown_values(self):
        self.assertEqual(Settings().artifactory_downloads, "proxy")
        with self.assertRaises(SettingsError):
            Settings(enrollment_token="x", artifactory_downloads="presign").validate()


if __name__ == "__main__":
    avocado.main()
