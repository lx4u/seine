# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import os
import shutil
import tempfile
from unittest import mock

from avocado import Test
from fastapi.testclient import TestClient

from seine.distributed.server.api import create_app
from seine.distributed.server.db import Database
from seine.distributed.server.settings import Settings
from seine.storage.base import StorageError, StorageOfflineError
from seine.storage.s3.client import S3Client
from seine.storage.s3.provider import S3StorageProvider


class TestS3GenerateDownloadUrl(Test):
    """Tests for S3StorageProvider temporary presigned download URL generation."""

    def setUp(self):
        self.mock_client = mock.MagicMock(spec=S3Client)
        self.mock_client.endpoint = "http://192.168.1.100:3900"
        self.provider = S3StorageProvider(self.mock_client, "test-bucket")

    def test_generate_download_url_full_key(self):
        self.mock_client.presign_get.return_value = "http://s3.local/signed-1"

        url = self.provider.generate_download_url("demo", "artifacts/demo/bld-1/pc-image.img")
        self.assertEqual(url, "http://s3.local/signed-1")
        self.mock_client.presign_get.assert_called_once_with(
            "test-bucket", "artifacts/demo/bld-1/pc-image.img", 3600)

    def test_generate_download_url_artifact_name(self):
        self.mock_client.presign_get.return_value = "http://s3.local/signed-2"

        url = self.provider.generate_download_url("demo", "pc-image.img")
        self.assertEqual(url, "http://s3.local/signed-2")
        self.mock_client.presign_get.assert_called_once_with(
            "test-bucket", "artifacts/demo/pc-image.img", 3600)

    def test_generate_download_url_custom_expiration(self):
        self.provider.generate_download_url("demo", "pc-image.img", expires_in=7200)
        self.mock_client.presign_get.assert_called_once_with(
            "test-bucket", "artifacts/demo/pc-image.img", 7200)

    def test_generate_download_url_offline_mode_strict(self):
        strict_provider = S3StorageProvider(self.mock_client, "test-bucket", offline_mode="strict")
        self.mock_client.presign_get.side_effect = Exception("network unreachable")

        with self.assertRaises(StorageOfflineError):
            strict_provider.generate_download_url("demo", "pc-image.img")

    def test_generate_download_url_failure_raises_storage_error(self):
        self.mock_client.presign_get.side_effect = Exception("bad credentials")

        with self.assertRaises(StorageError):
            self.provider.generate_download_url("demo", "pc-image.img")


class TestBuildArtifactDownloadUrlsAPI(Test):
    """Tests for GET /api/v1/builds/{id} download_urls generation."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="seine-test-api-artifacts-")
        self.db_path = os.path.join(self.tmp_dir, "test.db")
        self.db = Database(self.db_path)
        self.app = create_app(db=self.db)
        self.client = TestClient(self.app)

        self.mock_storage = mock.MagicMock(spec=S3StorageProvider)
        self.mock_storage.generate_download_url.side_effect = lambda proj, key, expires_in=3600: (
            f"http://192.168.1.100:3900/seine-cache/{key}?expires={expires_in}"
        )
        self.app.state.storage_provider = self.mock_storage
        self.db.ensure_project("demo")
        self.db.users.create("alice")
        self.db.projects.add_member("demo", "alice", role="developer")
        token = self.db.tokens.issue(user_id="alice", kind="pat")["token"]
        self.headers = {"Authorization": f"Bearer {token}"}

    def tearDown(self):
        self.db.close()
        if os.path.exists(self.tmp_dir):
            shutil.rmtree(self.tmp_dir, ignore_errors=True)

    @staticmethod
    def _meta(build_id, *names):
        return [
            {"name": n, "key": f"artifacts/demo/{build_id}/{n}", "sha256": "cd" * 32, "size": 5}
            for n in names
        ]

    def _complete(self, build_id, *names, urls=None):
        meta = self._meta(build_id, *names)
        self.db.builds.update_status(
            build_id, "completed",
            artifact_urls=urls if urls is not None else [m["key"] for m in meta],
            artifact_meta=meta,
        )

    def test_completed_build_returns_download_urls(self):
        build_id = "bld-test-completed"
        self.db.builds.create(id=build_id, project="demo", target_arch="amd64")
        self._complete(build_id, "pc-image.img", "pc-image.img.digest")

        resp = self.client.get(f"/api/v1/builds/{build_id}", headers=self.headers)
        self.assertEqual(resp.status_code, 200)

        data = resp.json()
        self.assertEqual(data["status"], "completed")
        self.assertIn("download_urls", data)
        download_urls = data["download_urls"]
        self.assertEqual(
            download_urls.get("pc-image.img"),
            "http://192.168.1.100:3900/seine-cache/artifacts/demo/bld-test-completed/pc-image.img?expires=3600",
        )
        self.assertEqual(
            download_urls.get("pc-image.img.digest"),
            "http://192.168.1.100:3900/seine-cache/artifacts/demo/bld-test-completed/pc-image.img.digest?expires=3600",
        )
        self.assertEqual(
            data["artifacts"],
            [{"name": n, "size": 5, "sha256": "cd" * 32}
             for n in ("pc-image.img", "pc-image.img.digest")],
        )

    def test_download_urls_only_for_names_in_the_manifest(self):
        build_id = "bld-test-partial"
        self.db.builds.create(id=build_id, project="demo", target_arch="amd64")
        self._complete(
            build_id, "disk.raw",
            urls=[f"artifacts/demo/{build_id}/disk.raw", f"artifacts/demo/{build_id}/extra.bin"],
        )

        data = self.client.get(f"/api/v1/builds/{build_id}", headers=self.headers).json()
        self.assertEqual(list(data["download_urls"]), ["disk.raw"])
        self.assertEqual([a["name"] for a in data["artifacts"]], ["disk.raw"])

    def test_build_without_a_manifest_gets_no_download_urls(self):
        build_id = "bld-test-legacy"
        self.db.builds.create(id=build_id, project="demo", target_arch="amd64")
        self.db.builds.update_status(
            build_id, "completed", artifact_urls=[f"artifacts/demo/{build_id}/disk.raw"]
        )

        data = self.client.get(f"/api/v1/builds/{build_id}", headers=self.headers).json()
        self.assertEqual(data["download_urls"], {})
        self.assertEqual(data["artifacts"], [])
        self.mock_storage.generate_download_url.assert_not_called()

    def test_normal_build_has_no_expiry_fields(self):
        build_id = "bld-test-normal"
        self.db.builds.create(id=build_id, project="demo", target_arch="amd64")
        self._complete(build_id, "pc-image.img")

        data = self.client.get(f"/api/v1/builds/{build_id}", headers=self.headers).json()
        self.assertIsNone(data["artifacts_expired_at"])
        self.assertIsNone(data["artifacts_expired_reason"])

    def test_evicted_build_reports_why_its_artifacts_are_gone(self):
        for reason in ("ttl", "pressure"):
            build_id = f"bld-test-{reason}"
            self.db.builds.create(id=build_id, project="demo", target_arch="amd64")
            self._complete(build_id, "pc-image.img")
            self.db.builds.mark_artifacts_expired(build_id, reason, now=1234.0)

            resp = self.client.get(f"/api/v1/builds/{build_id}", headers=self.headers)
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertEqual(data["status"], "completed")
            self.assertEqual(data["artifacts_expired_at"], 1234.0)
            self.assertEqual(data["artifacts_expired_reason"], reason)
            self.assertEqual(data["download_urls"], {})
            self.assertEqual([a["name"] for a in data["artifacts"]], ["pc-image.img"])
        self.mock_storage.generate_download_url.assert_not_called()

    def test_queued_build_returns_empty_download_urls(self):
        build_id = "bld-test-queued"
        self.db.builds.create(id=build_id, project="demo", target_arch="amd64")

        resp = self.client.get(f"/api/v1/builds/{build_id}", headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["download_urls"], {})

    def test_storage_error_tolerated_without_failing_api(self):
        build_id = "bld-test-err"
        self.db.builds.create(id=build_id, project="demo", target_arch="amd64")
        self._complete(build_id, "disk.raw")
        self.mock_storage.generate_download_url.side_effect = Exception("Storage unreachable")

        resp = self.client.get(f"/api/v1/builds/{build_id}", headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["download_urls"], {})

    def _completed_build(self, build_id, is_release):
        self.db.builds.create(id=build_id, project="demo", target_arch="amd64", is_release=is_release)
        self._complete(build_id, "disk.raw")

    def test_non_member_gets_no_download_urls(self):
        self._completed_build("bld-secret", is_release=False)
        self.db.users.create("eve")
        token = self.db.tokens.issue(user_id="eve", kind="pat")["token"]

        resp = self.client.get("/api/v1/builds/bld-secret", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(resp.status_code, 403)
        self.assertNotIn("download_urls", resp.json())
        self.mock_storage.generate_download_url.assert_not_called()

    def test_bucket_and_key_follow_build_kind_and_project_row(self):
        self.db.ensure_project("demo", dev_bucket="demo-dev-custom", prod_bucket="demo-prod-custom")
        self._completed_build("bld-dev", is_release=False)
        self._completed_build("bld-rel", is_release=True)
        self.app.state.storage_provider = None
        self.app.state.settings = Settings(
            s3_endpoint="https://s3.test",
            s3_projects={"demo": {
                "dev": {"access_key": "GKdev", "secret_key": "dev-secret"},
                "prod": {"access_key": "GKprod", "secret_key": "prod-secret"},
            }},
        )

        with mock.patch("seine.storage.s3.S3StorageProvider") as provider, \
                mock.patch("seine.storage.s3.client.S3Client") as client:
            provider.return_value.generate_download_url.return_value = "http://s3/x"
            for build_id in ("bld-dev", "bld-rel"):
                resp = self.client.get(f"/api/v1/builds/{build_id}", headers=self.headers)
                self.assertEqual(resp.status_code, 200)

        self.assertEqual([c.args[1] for c in provider.call_args_list], ["demo-dev-custom", "demo-prod-custom"])
        self.assertEqual([c.args[1] for c in client.call_args_list], ["GKdev", "GKprod"])

    def test_no_credentials_means_no_download_urls_and_no_ambient_fallback(self):
        self._completed_build("bld-dev", is_release=False)
        self.app.state.storage_provider = None
        self.app.state.settings = Settings()
        with mock.patch("seine.storage.for_build") as ambient:
            resp = self.client.get("/api/v1/builds/bld-dev", headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["download_urls"], {})
        ambient.assert_not_called()


class TestBuildTokenAuthorization(Test):
    """Tests for token authorization on GET /api/v1/builds/{id}."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="seine-test-auth-")
        self.db_path = os.path.join(self.tmp_dir, "test.db")
        self.db = Database(self.db_path)
        self.app = create_app(db=self.db)
        self.client = TestClient(self.app)

        self.db.ensure_project("demo")
        self.db.users.create("alice")
        self.db.users.create("eve")
        self.member_tok = self.db.tokens.issue(user_id="alice", kind="pat")["token"]
        self.outsider_tok = self.db.tokens.issue(user_id="eve", kind="pat")["token"]
        self.db.projects.add_member("demo", "alice", role="developer")

        self.build_id = "bld-auth-check"
        self.db.builds.create(id=self.build_id, project="demo", target_arch="amd64")

    def tearDown(self):
        self.db.close()
        if os.path.exists(self.tmp_dir):
            shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_verified_member_token_succeeds(self):
        headers = {"Authorization": f"Bearer {self.member_tok}"}
        resp = self.client.get(f"/api/v1/builds/{self.build_id}", headers=headers)
        self.assertEqual(resp.status_code, 200)

    def test_non_member_token_gets_403_forbidden(self):
        headers = {"Authorization": f"Bearer {self.outsider_tok}"}
        resp = self.client.get(f"/api/v1/builds/{self.build_id}", headers=headers)
        self.assertEqual(resp.status_code, 403)

    def test_invalid_token_gets_401_unauthorized(self):
        headers = {"Authorization": "Bearer bad-token"}
        resp = self.client.get(f"/api/v1/builds/{self.build_id}", headers=headers)
        self.assertEqual(resp.status_code, 401)

    def test_unauthenticated_request_gets_401_unauthorized(self):
        resp = self.client.get(f"/api/v1/builds/{self.build_id}")
        self.assertEqual(resp.status_code, 401)
