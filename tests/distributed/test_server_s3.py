# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0
"""Tests for the S3 keys the server itself uses to stage worktrees and provision buckets."""

import os
import shutil
import tempfile
from unittest import mock

from avocado import Test
from fastapi.testclient import TestClient

from seine.distributed.server.admin import project_create
from seine.distributed.server.api import create_app
from seine.distributed.server.db import Database
from seine.distributed.server.settings import Settings


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


class ServerStorageTest(Test):
    """Worktree staging and bucket provisioning use the project's own keys."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="seine-test-server-s3-")
        self.db = Database(os.path.join(self.tmp_dir, "test.db"))
        self.app = create_app(db=self.db, settings=settings())
        self.client = TestClient(self.app)
        self.db.ensure_project("alpha", dev_bucket="alpha-dev", prod_bucket="alpha-prod")
        self.db.ensure_project("orphan")
        self.db.users.create("dev")
        self.db.users.create("root", is_admin=True)
        for name in ("alpha", "orphan"):
            self.db.projects.add_member(name, "dev", role="developer")
        self.headers = {"Authorization": f"Bearer {self.db.tokens.issue(user_id='dev')['token']}"}
        self.admin = {"Authorization": f"Bearer {self.db.tokens.issue(user_id='root', kind='admin')['token']}"}

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _upload(self, project):
        return self.client.post(
            f"/api/v1/projects/{project}/worktrees",
            content=b"\x28\xb5\x2f\xfd" + b"payload",
            headers={**self.headers, "Content-Type": "application/octet-stream"},
        )

    def test_staging_uses_the_projects_dev_key(self):
        with mock.patch("seine.storage.s3.client.S3Client") as client, \
                mock.patch("seine.storage.s3.S3StorageProvider") as provider:
            resp = self._upload("alpha")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(client.call_args.args[:3], ("https://s3.test", "GKa-dev", "a-dev-secret"))
        self.assertEqual(provider.call_args.args[1], "alpha-dev")
        provider.return_value.ensure_bucket.assert_called_once()

    def test_staging_without_credentials_is_refused_and_never_ambient(self):
        with mock.patch("seine.storage.for_build") as ambient, \
                mock.patch("seine.storage.s3.client.S3Client") as client:
            resp = self._upload("orphan")
        self.assertEqual(resp.status_code, 409)
        self.assertIn("no S3 dev credentials", resp.json()["detail"])
        ambient.assert_not_called()
        client.assert_not_called()

    def test_injected_provider_still_wins(self):
        injected = mock.MagicMock()
        injected.push_worktree.return_value = True
        self.app.state.storage_provider = injected
        self.assertEqual(self._upload("orphan").status_code, 200)

    def test_provisioning_uses_the_key_of_each_environment(self):
        self.app.state.settings = settings(s3_default={
            "dev": {"access_key": "GKd", "secret_key": "sd"},
            "prod": {"access_key": "GKp", "secret_key": "sp"},
        })
        with mock.patch("seine.storage.s3.client.S3Client") as client, \
                mock.patch("seine.storage.s3.S3StorageProvider") as provider:
            resp = self.client.post(
                "/api/v1/projects", headers=self.admin,
                json={"name": "fresh", "dev_bucket": "f-dev", "prod_bucket": "f-prod",
                      "provision_buckets": True},
            )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual([c.args[1] for c in client.call_args_list], ["GKd", "GKp"])
        self.assertEqual([c.args[1] for c in provider.call_args_list], ["f-dev", "f-prod"])

    def test_admin_cli_provisioning_uses_the_key_of_each_environment(self):
        with mock.patch("seine.storage.s3.client.S3Client") as client, \
                mock.patch("seine.storage.s3.S3StorageProvider") as provider:
            project_create(self.db, "alpha-x", provision_buckets=True, settings=Settings(
                s3_endpoint="https://s3.test",
                s3_default={
                    "dev": {"access_key": "GKd", "secret_key": "sd"},
                    "prod": {"access_key": "GKp", "secret_key": "sp"},
                },
            ))
        self.assertEqual([c.args[1] for c in client.call_args_list], ["GKd", "GKp"])
        self.assertEqual([c.args[1] for c in provider.call_args_list], ["seine-alpha-x-dev", "seine-alpha-x-prod"])


class HomeProjectStorageTest(Test):
    """A home project has no keys or bucket of its own until its first upload."""

    DEFAULT_KEYS = {"dev": {"access_key": "GKdefault", "secret_key": "default-secret"}}

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="seine-test-home-s3-")
        self.db = Database(os.path.join(self.tmp_dir, "test.db"))
        self.settings = Settings(
            s3_endpoint="https://s3.test", s3_region="lan", s3_default=self.DEFAULT_KEYS,
            new_user_project="auto")
        self.client = TestClient(create_app(db=self.db, settings=self.settings))
        self.db.users.create("root", is_admin=True)
        self.admin = {"Authorization": f"Bearer {self.db.tokens.issue(user_id='root', kind='admin')['token']}"}

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _upload_as(self, user_id):
        token = self.db.tokens.issue(user_id=user_id)["token"]
        return self.client.post(
            f"/api/v1/projects/home-{user_id}/worktrees",
            content=b"\x28\xb5\x2f\xfd" + b"payload",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/octet-stream"},
        )

    def test_creating_a_user_touches_no_storage(self):
        with mock.patch("seine.storage.s3.client.S3Client") as client, \
                mock.patch("seine.storage.s3.S3StorageProvider") as provider:
            resp = self.client.post("/api/v1/users", json={"id": "rita"}, headers=self.admin)
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()["default_project"], "home-rita")
        client.assert_not_called()
        provider.assert_not_called()

    def test_first_upload_creates_the_dev_bucket_with_the_default_key(self):
        self.db.provision_new_user("rita", mode="auto")
        with mock.patch("seine.storage.s3.client.S3Client") as client, \
                mock.patch("seine.storage.s3.S3StorageProvider") as provider:
            resp = self._upload_as("rita")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(client.call_args.args[:3], ("https://s3.test", "GKdefault", "default-secret"))
        self.assertEqual(provider.call_args.args[1], "seine-home-rita-dev")
        provider.return_value.ensure_bucket.assert_called_once()

    def test_a_key_that_cannot_create_the_bucket_gives_a_clear_error(self):
        from seine.storage.base import StorageError
        self.db.provision_new_user("rita", mode="auto")
        with mock.patch("seine.storage.s3.client.S3Client"), \
                mock.patch("seine.storage.s3.S3StorageProvider") as provider:
            provider.return_value.ensure_bucket.side_effect = StorageError(
                "could not create bucket 'seine-home-rita-dev': denied")
            resp = self._upload_as("rita")
        self.assertEqual(resp.status_code, 502)
        self.assertIn("could not create bucket", resp.json()["detail"])
