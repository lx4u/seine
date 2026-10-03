# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the distributed REST API server."""

import hashlib
import io
import os
import shutil
import tempfile
import time
from unittest import mock

from avocado import Test
from fastapi.testclient import TestClient

from seine.distributed.common.models import (
    BuildSubmitRequest,
    ClaimJobRequest,
    HeartbeatRequest,
    JobStatusUpdateRequest,
    RegisterWorkerRequest,
    WorkerCapabilities,
)
from seine.distributed.server.api import create_app
from seine.distributed.server.cli import main as cli_main
from seine.distributed.server.db import Database
from seine.distributed.server.settings import Settings

S3_SETTINGS = Settings(
    s3_endpoint="https://s3.test",
    s3_default={
        "dev": {"access_key": "GKdev", "secret_key": "dev-secret"},
        "prod": {"access_key": "GKprod", "secret_key": "prod-secret"},
    },
)


class WorktreeRelayTest(Test):
    """Test worktree staging relay and S3 storage interaction."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="seine-test-api-")
        self.db_path = os.path.join(self.tmp_dir, "test.db")
        self.db = Database(self.db_path)
        self.app = create_app(db=self.db, enrollment_token="test-enrollment-token")
        self.client = TestClient(self.app)

        self.db.projects.create("alpha")
        self.db.users.create("alice")
        self.db.users.create("mallory")
        self.db.projects.add_member("alpha", "alice", role="developer")
        self.user_token = self.db.tokens.issue(user_id="alice", kind="pat")["token"]
        self.other_token = self.db.tokens.issue(user_id="mallory", kind="pat")["token"]
        self.headers = {"Authorization": f"Bearer {self.user_token}"}

    def tearDown(self):
        self.db.close()
        if os.path.exists(self.tmp_dir):
            shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _upload(self, content=b"\x28\xb5\x2f\xfd" + b"dummy zstd payload", project="alpha", headers=None):
        headers = dict(self.headers if headers is None else headers)
        headers["Content-Type"] = "application/octet-stream"
        return self.client.post(
            f"/api/v1/projects/{project}/worktrees",
            content=content,
            headers=headers,
        )

    def test_worktree_upload_relay_success(self):
        staged = {}

        def push(project, digest, path):
            with open(path, "rb") as f:
                staged.update(project=project, digest=digest, path=path, content=f.read())
            return True

        mock_provider = mock.MagicMock()
        mock_provider.push_worktree.side_effect = push
        self.app.state.storage_provider = mock_provider

        dummy_content = b"\x28\xb5\x2f\xfd" + b"dummy zstd payload"
        resp = self._upload(dummy_content)
        self.assertEqual(resp.status_code, 200)

        data = resp.json()
        self.assertEqual(data["digest"], hashlib.sha256(dummy_content).hexdigest())
        self.assertEqual(data["bytes"], len(dummy_content))
        self.assertEqual(data["status"], "staged")

        mock_provider.ensure_bucket.assert_called_once()
        self.assertEqual(staged["project"], "alpha")
        self.assertEqual(staged["digest"], data["digest"])
        self.assertEqual(staged["content"], dummy_content)
        self.assertFalse(os.path.exists(staged["path"]))

    def test_worktree_upload_refreshes_the_staged_bundle(self):
        provider = mock.MagicMock()
        self.app.state.storage_provider = provider
        digest = self._upload().json()["digest"]
        provider.refresh_worktree.assert_called_once_with("alpha", digest)

    def test_worktree_upload_survives_a_failed_refresh(self):
        provider = mock.MagicMock()
        provider.refresh_worktree.side_effect = RuntimeError("s3 hiccup")
        self.app.state.storage_provider = provider
        with self.assertLogs("seine.server.api", "WARNING"):
            resp = self._upload()
        self.assertEqual(resp.status_code, 200)

    def test_dev_only_project_takes_no_prod_worktree(self):
        self.db.projects.create("home-rita", dev_only=True)
        self.db.users.create("rita")
        self.db.projects.add_member("home-rita", "rita", role="releaser")
        token = self.db.tokens.issue(user_id="rita", kind="pat")["token"]
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/octet-stream"}
        payload = b"\x28\xb5\x2f\xfd" + b"x"

        resp = self.client.post(
            "/api/v1/projects/home-rita/worktrees?env=prod", content=payload, headers=headers)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("dev-only", resp.json()["detail"])

    def test_dev_only_project_still_takes_dev_worktrees(self):
        self.db.projects.create("home-rita", dev_only=True)
        self.db.users.create("rita")
        self.db.projects.add_member("home-rita", "rita", role="developer")
        token = self.db.tokens.issue(user_id="rita", kind="pat")["token"]
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/octet-stream"}
        self.app.state.storage_provider = mock.MagicMock()
        resp = self.client.post(
            "/api/v1/projects/home-rita/worktrees", content=b"\x28\xb5\x2f\xfd" + b"x",
            headers=headers)
        self.assertEqual(resp.status_code, 200)

    def test_non_member_does_not_learn_that_a_project_is_dev_only(self):
        self.db.projects.create("home-rita", dev_only=True)
        self.db.users.create("sam")
        token = self.db.tokens.issue(user_id="sam", kind="pat")["token"]
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/octet-stream"}
        resp = self.client.post(
            "/api/v1/projects/home-rita/worktrees?env=prod", content=b"\x28\xb5\x2f\xfd" + b"x",
            headers=headers)
        self.assertEqual(resp.status_code, 403)

    def _upload_env(self, env, token):
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/octet-stream"}
        return self.client.post(
            f"/api/v1/projects/alpha/worktrees?env={env}", content=b"\x28\xb5\x2f\xfd" + b"x", headers=headers,
        )

    def test_prod_worktree_upload_as_releaser_lands_in_prod_bucket(self):
        self.db.users.create("rita")
        self.db.projects.add_member("alpha", "rita", role="releaser")
        token = self.db.tokens.issue(user_id="rita", kind="pat")["token"]
        prod_bucket = self.db.get_project("alpha")["prod_bucket"]
        with mock.patch("seine.distributed.server.api.provider_for") as provider_for:
            provider_for.return_value.push_worktree.return_value = True
            resp = self._upload_env("prod", token)
        self.assertEqual(resp.status_code, 200)
        provider_for.assert_called_once_with(mock.ANY, "alpha", prod_bucket, "prod")
        provider_for.return_value.push_worktree.assert_called_once()

    def test_dev_worktree_upload_lands_in_dev_bucket(self):
        dev_bucket = self.db.get_project("alpha")["dev_bucket"]
        with mock.patch("seine.distributed.server.api.provider_for") as provider_for:
            provider_for.return_value.push_worktree.return_value = True
            resp = self._upload_env("dev", self.user_token)
        self.assertEqual(resp.status_code, 200)
        provider_for.assert_called_once_with(mock.ANY, "alpha", dev_bucket, "dev")

    def test_prod_worktree_upload_as_developer_is_403(self):
        self.app.state.storage_provider = mock.MagicMock()
        self.assertEqual(self._upload_env("prod", self.user_token).status_code, 403)
        self.app.state.storage_provider.push_worktree.assert_not_called()

    def test_worktree_upload_unknown_env_is_400(self):
        self.app.state.storage_provider = mock.MagicMock()
        self.assertEqual(self._upload_env("staging", self.user_token).status_code, 400)
        self.app.state.storage_provider.push_worktree.assert_not_called()

    def test_worktree_upload_relay_unauthorized(self):
        resp = self._upload(headers={})
        self.assertEqual(resp.status_code, 401)

        resp_bad = self._upload(headers={"Authorization": "Bearer invalid-token"})
        self.assertEqual(resp_bad.status_code, 401)

    def test_worktree_upload_relay_empty_file(self):
        resp = self._upload(b"")
        self.assertEqual(resp.status_code, 400)

    def test_worktree_upload_requires_membership(self):
        self.app.state.storage_provider = mock.MagicMock()
        resp = self._upload(headers={"Authorization": f"Bearer {self.other_token}"})
        self.assertEqual(resp.status_code, 403)
        self.app.state.storage_provider.push_worktree.assert_not_called()

    def test_worktree_upload_unknown_project_is_404(self):
        resp = self._upload(project="ghost")
        self.assertEqual(resp.status_code, 404)
        self.assertIsNone(self.db.get_project("ghost"))

    def test_worktree_upload_bad_project_name_is_400(self):
        resp = self._upload(project="Bad_Name")
        self.assertEqual(resp.status_code, 400)

    def test_worktree_upload_oversize_is_413(self):
        app = create_app(db=self.db, max_upload_bytes=1024)
        app.state.storage_provider = mock.MagicMock()
        client = TestClient(app)
        headers = {**self.headers, "Content-Type": "application/octet-stream"}
        resp = client.post("/api/v1/projects/alpha/worktrees", content=b"x" * 4096, headers=headers)
        self.assertEqual(resp.status_code, 413)
        app.state.storage_provider.push_worktree.assert_not_called()

    def test_worktree_upload_oversize_stream_without_length_is_413(self):
        app = create_app(db=self.db, max_upload_bytes=1024)
        app.state.storage_provider = mock.MagicMock()
        client = TestClient(app)
        headers = {**self.headers, "Content-Type": "application/octet-stream"}
        resp = client.post(
            "/api/v1/projects/alpha/worktrees",
            content=iter([b"x" * 600, b"x" * 600]),
            headers=headers,
        )
        self.assertEqual(resp.status_code, 413)
        app.state.storage_provider.push_worktree.assert_not_called()

    def test_worktree_upload_checks_auth_before_reading_body(self):
        consumed = []

        def body():
            consumed.append(True)
            yield b"x" * 100

        headers = {"Content-Type": "application/octet-stream"}
        resp = self.client.post("/api/v1/projects/alpha/worktrees", content=body(), headers=headers)
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(consumed, [])

    def test_worktree_upload_rejects_multipart(self):
        files = {"file": ("tree.tar.zst", io.BytesIO(b"abc"), "application/octet-stream")}
        resp = self.client.post("/api/v1/projects/alpha/worktrees", files=files, headers=self.headers)
        self.assertEqual(resp.status_code, 415)

    def test_worktree_upload_storage_failure_is_502(self):
        mock_provider = mock.MagicMock()
        mock_provider.push_worktree.side_effect = RuntimeError("s3 down")
        self.app.state.storage_provider = mock_provider
        resp = self._upload()
        self.assertEqual(resp.status_code, 502)
        self.assertIn("s3 down", resp.json()["detail"])

    def test_worktree_upload_push_refused_is_502(self):
        mock_provider = mock.MagicMock()
        mock_provider.push_worktree.return_value = False
        self.app.state.storage_provider = mock_provider
        self.assertEqual(self._upload().status_code, 502)


class WorktreeStagingCheckTest(Test):
    """Test that a build is only accepted when its worktree is staged for its environment."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="seine-test-staging-")
        self.db = Database(os.path.join(self.tmp_dir, "test.db"))
        self.client = TestClient(create_app(db=self.db))
        self.db.projects.create("alpha")
        self.db.users.create("rita")
        self.db.projects.add_member("alpha", "rita", role="releaser")
        self.headers = {"Authorization": f"Bearer {self.db.tokens.issue(user_id='rita', kind='pat')['token']}"}
        self.staged = {"dev": set(), "prod": set()}

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _submit(self, digest, is_release):
        def provider_for(settings, project, bucket, env):
            provider = mock.MagicMock()
            provider.has_worktree.side_effect = lambda p, d: d in self.staged[env]
            return provider

        req = BuildSubmitRequest(project="alpha", worktree_digest=digest, is_release=is_release)
        with mock.patch("seine.distributed.server.api.provider_for", provider_for):
            return self.client.post("/api/v1/builds", json=req.model_dump(), headers=self.headers)

    def test_release_build_in_a_dev_only_project_is_400(self):
        self.db.projects.create("home-rita", dev_only=True)
        self.db.projects.add_member("home-rita", "rita", role="releaser")
        req = BuildSubmitRequest(project="home-rita", worktree_digest="d1", is_release=True)
        resp = self.client.post("/api/v1/builds", json=req.model_dump(), headers=self.headers)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("dev-only", resp.json()["detail"])
        self.assertEqual(self.db.builds.list(), [])

    def test_submitted_spec_files_are_stored_with_the_first_as_spec_file(self):
        self.staged["dev"].add("d1")

        def provider_for(settings, project, bucket, env):
            provider = mock.MagicMock()
            provider.has_worktree.return_value = True
            return provider

        files = ["a.yaml", "--", "b.yaml"]
        req = BuildSubmitRequest(project="alpha", worktree_digest="d1", spec_file="a.yaml",
                                 spec_files=files)
        with mock.patch("seine.distributed.server.api.provider_for", provider_for):
            resp = self.client.post("/api/v1/builds", json=req.model_dump(), headers=self.headers)
        build = self.db.builds.get(resp.json()["build_id"])
        self.assertEqual(build["spec_files"], files)
        self.assertEqual(build["spec_file"], "a.yaml")

    def test_dev_build_with_dev_staged_digest_is_accepted(self):
        self.staged["dev"].add("d1")
        self.assertEqual(self._submit("d1", False).status_code, 200)

    def test_release_build_with_prod_staged_digest_is_accepted(self):
        self.staged["prod"].add("d1")
        self.assertEqual(self._submit("d1", True).status_code, 200)

    def test_release_build_with_dev_only_digest_is_400(self):
        self.staged["dev"].add("d1")
        resp = self._submit("d1", True)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["detail"], "worktree d1 is not staged for prod builds")
        self.assertEqual(self.db.builds.list(), [])

    def test_dev_build_with_prod_only_digest_is_400(self):
        self.staged["prod"].add("d1")
        resp = self._submit("d1", False)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["detail"], "worktree d1 is not staged for dev builds")

    def test_submit_refreshes_the_staged_worktree_in_its_bucket(self):
        providers = {}

        def provider_for(settings, project, bucket, env):
            providers[env] = mock.MagicMock()
            return providers[env]

        for is_release, env in ((False, "dev"), (True, "prod")):
            req = BuildSubmitRequest(project="alpha", worktree_digest="d1", is_release=is_release)
            with mock.patch("seine.distributed.server.api.provider_for", provider_for):
                self.assertEqual(self.client.post("/api/v1/builds", json=req.model_dump(), headers=self.headers).status_code, 200)
            providers[env].refresh_worktree.assert_called_once_with("alpha", "d1")

    def test_submit_survives_a_failed_refresh(self):
        provider = mock.MagicMock()
        provider.refresh_worktree.side_effect = RuntimeError("s3 hiccup")
        with mock.patch("seine.distributed.server.api.provider_for", return_value=provider):
            req = BuildSubmitRequest(project="alpha", worktree_digest="d1")
            with self.assertLogs("seine.server.api", "WARNING"):
                resp = self.client.post("/api/v1/builds", json=req.model_dump(), headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(self.db.builds.list()), 1)

    def test_missing_worktree_is_not_refreshed(self):
        self.assertEqual(self._submit("d1", False).status_code, 400)

    def test_storage_failure_is_502(self):
        provider = mock.MagicMock()
        provider.has_worktree.side_effect = RuntimeError("s3 down")
        with mock.patch("seine.distributed.server.api.provider_for", return_value=provider):
            req = BuildSubmitRequest(project="alpha", worktree_digest="d1")
            resp = self.client.post("/api/v1/builds", json=req.model_dump(), headers=self.headers)
        self.assertEqual(resp.status_code, 502)


class WorkerEnrollmentTest(Test):
    """Test worker trust, registration, and token minting."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="seine-test-enroll-")
        self.db_path = os.path.join(self.tmp_dir, "test.db")
        self.db = Database(self.db_path)
        self.app = create_app(db=self.db, enrollment_token="secret-enroll-token")
        self.client = TestClient(self.app)

    def tearDown(self):
        self.db.close()
        if os.path.exists(self.tmp_dir):
            shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_enrollment_valid_token(self):
        req = RegisterWorkerRequest(
            worker_id="worker-node-1",
            hostname="node1.local",
            capabilities=WorkerCapabilities(
                native_arch="amd64",
                arch_scores={"arm64": 0.7},
                concurrency_slots=4,
                free_disk_gb=150.0,
            ),
        )
        headers = {"Authorization": "Bearer secret-enroll-token"}

        resp = self.client.post(
            "/api/v1/workers/register",
            json=req.model_dump(),
            headers=headers,
        )
        self.assertEqual(resp.status_code, 200)

        data = resp.json()
        self.assertEqual(data["worker_id"], "worker-node-1")
        self.assertEqual(data["status"], "registered")
        self.assertIn("worker_token", data)
        self.assertTrue(len(data["worker_token"]) >= 24)

        worker = self.db.workers.get("worker-node-1")
        self.assertIsNotNone(worker)
        self.assertEqual(worker["hostname"], "node1.local")
        self.assertEqual(worker["native_arch"], "amd64")
        self.assertEqual(worker["concurrency_slots"], 4)
        self.assertEqual(worker["free_disk_gb"], 150.0)
        self.assertNotIn("token_hash", worker)
        by_token = self.db.get_worker_by_token(data["worker_token"])
        self.assertEqual(by_token["id"], "worker-node-1")

    def test_enrollment_invalid_token(self):
        req = RegisterWorkerRequest(
            worker_id="worker-node-2",
            hostname="node2.local",
            capabilities=WorkerCapabilities(native_arch="amd64"),
        )
        resp_wrong = self.client.post(
            "/api/v1/workers/register",
            json=req.model_dump(),
            headers={"Authorization": "Bearer wrong-token"},
        )
        self.assertEqual(resp_wrong.status_code, 401)

        resp_missing = self.client.post(
            "/api/v1/workers/register",
            json=req.model_dump(),
        )
        self.assertEqual(resp_missing.status_code, 401)


class WorkerClaimAndHeartbeatTest(Test):
    """Test worker claim loop, HTTP 204 behavior, and heartbeats."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="seine-test-claim-")
        self.db_path = os.path.join(self.tmp_dir, "test.db")
        self.db = Database(self.db_path)
        self.app = create_app(db=self.db, enrollment_token="secret-token", settings=S3_SETTINGS)
        self.client = TestClient(self.app)

        self.worker_id = "worker-arm-1"
        self.worker_token = "wtoken-abc12345"
        self.db.upsert_worker(
            worker_id=self.worker_id,
            hostname="arm-box",
            native_arch="arm64",
            arch_scores={"arm64": 1.0, "amd64": 0.3},
            free_disk_gb=100.0,
            token=self.worker_token,
            concurrency_slots=2,
        )

    def tearDown(self):
        self.db.close()
        if os.path.exists(self.tmp_dir):
            shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_claim_empty_queue_returns_clean_204(self):
        headers = {"Authorization": f"Bearer {self.worker_token}"}
        req = ClaimJobRequest(worker_id=self.worker_id)

        resp = self.client.post("/api/v1/workers/claim", json=req.model_dump(), headers=headers)
        self.assertEqual(resp.status_code, 204)
        self.assertEqual(len(resp.content), 0)

    def test_claim_matching_job_success(self):
        self.db.create_build(
            build_id="bld-arm-test",
            project="testproj",
            target_arch="arm64",
            worktree_digest="tree123",
        )

        headers = {"Authorization": f"Bearer {self.worker_token}"}
        req = ClaimJobRequest(worker_id=self.worker_id)

        resp = self.client.post("/api/v1/workers/claim", json=req.model_dump(), headers=headers)
        self.assertEqual(resp.status_code, 200)

        data = resp.json()
        self.assertEqual(data["build_id"], "bld-arm-test")
        self.assertEqual(data["target_arch"], "arm64")
        self.assertEqual(data["kind"], "image")
        self.assertEqual(data["worktree_digest"], "tree123")

        build = self.db.builds.get("bld-arm-test")
        self.assertEqual(build["status"], "running")

    def test_the_claimed_job_carries_every_spec_file(self):
        files = ["main.yaml", "--", "other.yaml"]
        self.db.create_build(
            build_id="bld-multi", project="testproj", target_arch="arm64",
            worktree_digest="tree123", spec_file="main.yaml", spec_files=files)
        headers = {"Authorization": f"Bearer {self.worker_token}"}
        req = ClaimJobRequest(worker_id=self.worker_id)
        resp = self.client.post("/api/v1/workers/claim", json=req.model_dump(), headers=headers)
        self.assertEqual(resp.json()["spec_files"], files)

    def test_claim_unsupported_arch_returns_204(self):
        self.db.create_build(
            build_id="bld-mips-test",
            project="testproj",
            target_arch="mips64",
            worktree_digest="tree-mips",
        )

        headers = {"Authorization": f"Bearer {self.worker_token}"}
        req = ClaimJobRequest(worker_id=self.worker_id)

        resp = self.client.post("/api/v1/workers/claim", json=req.model_dump(), headers=headers)
        self.assertEqual(resp.status_code, 204)
        self.assertEqual(len(resp.content), 0)

    def test_claim_invalid_token_returns_403(self):
        req = ClaimJobRequest(worker_id=self.worker_id)
        resp = self.client.post(
            "/api/v1/workers/claim",
            json=req.model_dump(),
            headers={"Authorization": "Bearer bad-token"},
        )
        self.assertEqual(resp.status_code, 403)

    def test_worker_heartbeat(self):
        headers = {"Authorization": f"Bearer {self.worker_token}"}
        req = HeartbeatRequest(worker_id=self.worker_id, free_disk_gb=77.5)

        resp = self.client.post(
            "/api/v1/workers/heartbeat",
            json=req.model_dump(),
            headers=headers,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"status": "ok", "cancel": []})

        worker = self.db.workers.get(self.worker_id)
        self.assertEqual(worker["free_disk_gb"], 77.5)

        resp_bad = self.client.post(
            "/api/v1/workers/heartbeat",
            json=req.model_dump(),
            headers={"Authorization": "Bearer invalid"},
        )
        self.assertEqual(resp_bad.status_code, 403)

    def test_heartbeat_brings_reaped_worker_back_online(self):
        self.db.scheduler.reap_stale(now=time.time() + 3600)
        self.assertEqual(self.db.workers.get(self.worker_id)["status"], "offline")

        resp = self.client.post(
            "/api/v1/workers/heartbeat",
            json=HeartbeatRequest(worker_id=self.worker_id, free_disk_gb=10.0).model_dump(),
            headers={"Authorization": f"Bearer {self.worker_token}"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.db.workers.get(self.worker_id)["status"], "online")

    def test_heartbeat_returns_cancel_requested_jobs(self):
        self.db.create_build(
            build_id="bld-cancel-me",
            project="testproj",
            target_arch="arm64",
            worktree_digest="tree",
        )
        job = self.db.scheduler.claim_job(self.worker_id)
        self.db.scheduler.request_cancel("bld-cancel-me")

        resp = self.client.post(
            "/api/v1/workers/heartbeat",
            json=HeartbeatRequest(worker_id=self.worker_id, free_disk_gb=10.0).model_dump(),
            headers={"Authorization": f"Bearer {self.worker_token}"},
        )
        self.assertEqual(resp.json()["cancel"], [job["job_id"]])

    def _claim_status_request(self, build_id="bld-status-test", **fields):
        self.db.create_build(
            build_id=build_id,
            project="statusproj",
            target_arch="arm64",
            worktree_digest="tree-status",
        )
        job = self.db.scheduler.claim_job(self.worker_id)
        body = dict(
            worker_id=self.worker_id,
            build_id=build_id,
            job_id=job["job_id"],
            status="completed",
        )
        body.update(fields)
        return job, JobStatusUpdateRequest(**body)

    def _post_status(self, job_id, req, token=None):
        return self.client.post(
            f"/api/v1/workers/jobs/{job_id}/status",
            json=req.model_dump(),
            headers={"Authorization": f"Bearer {token or self.worker_token}"},
        )

    def _heartbeat(self, **fields):
        body = {"worker_id": self.worker_id, "free_disk_gb": 10.0, **fields}
        return self.client.post(
            "/api/v1/workers/heartbeat",
            json=body,
            headers={"Authorization": f"Bearer {self.worker_token}"},
        )

    def _claim_for_reconcile(self):
        self.db.create_build(
            build_id="bld-recon", project="testproj", target_arch="arm64", worktree_digest="tree"
        )
        job = self.db.scheduler.claim_job(self.worker_id)
        started = self.db.builds.get_job(job["job_id"])["started_at"]
        self.db.scheduler.clock = lambda: started + 1000
        return job["job_id"]

    def test_heartbeat_without_running_jobs_does_not_reconcile(self):
        job_id = self._claim_for_reconcile()
        resp = self._heartbeat()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.db.builds.get_job(job_id)["status"], "claimed")

    def test_heartbeat_with_empty_running_jobs_requeues_lost_job(self):
        job_id = self._claim_for_reconcile()
        resp = self._heartbeat(running_jobs=[])
        self.assertEqual(resp.json(), {"status": "ok", "cancel": []})
        self.assertEqual(self.db.builds.get_job(job_id)["status"], "queued")

    def test_heartbeat_listing_the_job_keeps_it(self):
        job_id = self._claim_for_reconcile()
        self.assertEqual(self._heartbeat(running_jobs=[job_id]).status_code, 200)
        self.assertEqual(self.db.builds.get_job(job_id)["status"], "claimed")

    def test_heartbeat_rejects_oversized_running_jobs(self):
        job_id = self._claim_for_reconcile()
        too_many = self._heartbeat(running_jobs=[f"job-{i}" for i in range(101)])
        too_long = self._heartbeat(running_jobs=["j" * 200])
        self.assertEqual((too_many.status_code, too_long.status_code), (400, 400))
        self.assertEqual(self._heartbeat(running_jobs=[1]).status_code, 422)
        self.assertEqual(self.db.builds.get_job(job_id)["status"], "claimed")

    def test_requeued_job_cannot_be_completed_by_the_old_worker(self):
        job, req = self._claim_status_request()
        started = self.db.builds.get_job(job["job_id"])["started_at"]
        self.db.scheduler.clock = lambda: started + 1000
        self._heartbeat(running_jobs=[])

        resp = self._post_status(job["job_id"], req)
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(self.db.builds.get_job(job["job_id"])["status"], "queued")

    def test_owner_can_complete_a_claimed_job_late(self):
        job, req = self._claim_status_request()
        started = self.db.builds.get_job(job["job_id"])["started_at"]
        self.db.scheduler.clock = lambda: started + 1000
        self._heartbeat(running_jobs=[job["job_id"]])

        resp = self._post_status(job["job_id"], req)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.db.builds.get_job(job["job_id"])["status"], "completed")

    def test_job_status_from_other_worker_is_403(self):
        job, req = self._claim_status_request()
        self.db.upsert_worker(
            worker_id="worker-other",
            hostname="other",
            native_arch="arm64",
            arch_scores={"arm64": 1.0},
            free_disk_gb=10.0,
            token="wtoken-other",
        )
        req.worker_id = "worker-other"
        resp = self._post_status(job["job_id"], req, token="wtoken-other")
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(self.db.builds.get_job(job["job_id"])["status"], "claimed")

    def test_job_status_rejects_foreign_artifact_prefix(self):
        job, req = self._claim_status_request(
            artifact_urls=["artifacts/statusproj/bld-other/image.raw"]
        )
        resp = self._post_status(job["job_id"], req)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.db.builds.get("bld-status-test")["artifact_urls"], [])

    def test_job_status_rejects_unknown_status(self):
        job, req = self._claim_status_request(status="queued")
        self.assertEqual(self._post_status(job["job_id"], req).status_code, 400)

    def test_job_status_ignores_body_build_id(self):
        job, req = self._claim_status_request(
            build_id="bld-real", artifact_urls=["artifacts/statusproj/bld-real/image.raw"]
        )
        req.build_id = "bld-spoofed"
        resp = self._post_status(job["job_id"], req)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            self.db.builds.get("bld-real")["artifact_urls"],
            ["artifacts/statusproj/bld-real/image.raw"],
        )

    def _artifact(self, name="image.raw", **fields):
        entry = {
            "name": name,
            "key": f"artifacts/statusproj/bld-status-test/{name}",
            "sha256": "ab" * 32,
            "size": 12,
        }
        entry.update(fields)
        return entry

    def test_job_status_stores_the_artifact_manifest(self):
        entries = [self._artifact("image.raw"), self._artifact("image.raw.digest", size=0)]
        job, req = self._claim_status_request(
            artifact_urls=[e["key"] for e in entries], artifacts=entries
        )
        self.assertEqual(self._post_status(job["job_id"], req).status_code, 200)
        self.assertEqual(self.db.builds.get("bld-status-test")["artifact_meta"], entries)

    def test_job_status_rejects_a_bad_manifest_entry(self):
        key = self._artifact()["key"]
        bad = [
            self._artifact(sha256="AB" * 32),
            self._artifact(sha256="ab" * 31),
            self._artifact(sha256="zz" * 32),
            self._artifact(sha256=None),
            self._artifact(size=-1),
            self._artifact(size="12"),
            self._artifact(size=1.5),
            self._artifact(size=True),
            self._artifact(name="", key=key[: -len("image.raw")]),
            self._artifact(name="../x", key=key.replace("image.raw", "../x")),
            self._artifact(name="a/b", key=key.replace("image.raw", "a/b")),
            self._artifact(name="a\\b", key=key.replace("image.raw", "a\\b")),
            self._artifact(name="x\0y", key=key.replace("image.raw", "x\0y")),
            self._artifact(name="n" * 256, key=key.replace("image.raw", "n" * 256)),
            self._artifact(name=".."),
            self._artifact(key="artifacts/statusproj/bld-other/image.raw"),
            self._artifact(key="artifacts/statusproj/bld-status-test/other.raw"),
            {"name": "image.raw", "sha256": "ab" * 32, "size": 1},
        ]
        job, req = self._claim_status_request()
        for entry in bad:
            req.artifacts = [entry]
            with self.subTest(entry=entry):
                self.assertEqual(self._post_status(job["job_id"], req).status_code, 400)
        build = self.db.builds.get("bld-status-test")
        self.assertEqual(build["artifact_meta"], [])
        self.assertEqual(self.db.builds.get_job(job["job_id"])["status"], "claimed")

    def test_worker_job_status_update(self):
        self.db.create_build(
            build_id="bld-status-test",
            project="statusproj",
            target_arch="arm64",
            worktree_digest="tree-status",
        )
        job = self.db.scheduler.claim_job(self.worker_id)
        self.assertIsNotNone(job)

        headers = {"Authorization": f"Bearer {self.worker_token}"}
        req = JobStatusUpdateRequest(
            worker_id=self.worker_id,
            build_id="bld-status-test",
            job_id=job["job_id"],
            status="completed",
            artifact_urls=["artifacts/statusproj/bld-status-test/image.raw"],
        )

        resp = self.client.post(
            f"/api/v1/workers/jobs/{job['job_id']}/status",
            json=req.model_dump(),
            headers=headers,
        )
        self.assertEqual(resp.status_code, 200)

        job_db = self.db.builds.get_job(job["job_id"])
        self.assertEqual(job_db["status"], "completed")

        build_db = self.db.builds.get("bld-status-test")
        self.assertEqual(build_db["status"], "completed")
        self.assertIn("artifacts/statusproj/bld-status-test/image.raw", build_db["artifact_urls"])

    def _error_of(self, status, message):
        job, req = self._claim_status_request(status=status, error_message=message)
        self.assertEqual(self._post_status(job["job_id"], req).status_code, 200)
        return self.db.builds.get("bld-status-test")["error_message"]

    def test_failed_job_stores_the_error_message(self):
        self.assertEqual(self._error_of("failed", "build exited with code 2"),
                         "build exited with code 2")

    def test_error_message_is_truncated_and_cleaned(self):
        stored = self._error_of("failed", "bad\x1b[31m\nline" + "x" * 2000)
        self.assertEqual(len(stored), 1000)
        self.assertTrue(stored.startswith("bad[31m line"))

    def test_error_message_is_ignored_for_other_statuses(self):
        self.assertIsNone(self._error_of("completed", "ignored"))

    def test_empty_error_message_is_not_stored(self):
        self.assertIsNone(self._error_of("failed", "  "))


class BuildSubmissionRBACTest(Test):
    """Test build submission and RBAC rejection of unauthorized release builds."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="seine-test-rbac-")
        self.db_path = os.path.join(self.tmp_dir, "test.db")
        self.db = Database(self.db_path)
        self.app = create_app(db=self.db, storage_provider=mock.MagicMock())
        self.client = TestClient(self.app)

        self.db.ensure_project("firmware")
        for name in ("developer_bob", "releaser_carol", "admin_dave", "outsider_eve"):
            self.db.users.create(name)
        self.db.users.create("sysadmin", is_admin=True)
        self.dev_tok = self.db.tokens.issue(user_id="developer_bob")["token"]
        self.rel_tok = self.db.tokens.issue(user_id="releaser_carol")["token"]
        self.adm_tok = self.db.tokens.issue(user_id="admin_dave")["token"]
        self.ext_tok = self.db.tokens.issue(user_id="outsider_eve")["token"]

        self.db.projects.add_member("firmware", "developer_bob", role="developer")
        self.db.projects.add_member("firmware", "releaser_carol", role="releaser")
        self.db.projects.add_member("firmware", "admin_dave", role="admin")
        self.db.ensure_project("secret")
        self.db.projects.add_member("secret", "releaser_carol", role="releaser")

    def tearDown(self):
        self.db.close()
        if os.path.exists(self.tmp_dir):
            shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_development_build_submission_allowed(self):
        req = BuildSubmitRequest(
            project="firmware",
            worktree_digest="tree-dev-1",
            target_arch="amd64",
            is_release=False,
        )
        resp = self.client.post(
            "/api/v1/builds",
            json=req.model_dump(),
            headers={"Authorization": f"Bearer {self.dev_tok}"},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "queued")
        self.assertEqual(data["project"], "firmware")

        build = self.db.builds.get(data["build_id"])
        self.assertFalse(build["is_release"])
        self.assertEqual(build["user_id"], "developer_bob")

    def _submit_options(self, options):
        req = BuildSubmitRequest(
            project="firmware", worktree_digest="tree-x", target_arch="amd64", options=options
        )
        return self.client.post(
            "/api/v1/builds", json=req.model_dump(),
            headers={"Authorization": f"Bearer {self.dev_tok}"},
        )

    def test_known_build_options_are_accepted(self):
        options = {"packages_only": True, "s3_cache": True, "require_native": True,
                   "min_arch_score": 0.5}
        resp = self._submit_options(options)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.db.builds.get(resp.json()["build_id"])["options"], options)

    def test_unknown_build_options_are_listed_in_a_400(self):
        resp = self._submit_options({"s3_cache": True, "zzz": 1, "aaa": 2})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("aaa, zzz", resp.json()["detail"])
        self.assertEqual(self.db.builds.list(), [])

    def test_secret_looking_options_are_refused(self):
        for key in ("token", "api_secret", "Password", "ssh_key", "credentials", "s3_cache_token"):
            with self.subTest(key=key):
                resp = self._submit_options({key: "x"})
                self.assertEqual(resp.status_code, 400)
                self.assertIn(key, resp.json()["detail"])
                self.assertIn("secrets", resp.json()["detail"])

    def _submit(self, token=None, project="firmware"):
        req = BuildSubmitRequest(project=project, worktree_digest="tree-x", target_arch="amd64")
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return self.client.post("/api/v1/builds", json=req.model_dump(), headers=headers)

    def test_anonymous_submit_get_and_list_are_401(self):
        build_id = self._submit(self.dev_tok).json()["build_id"]
        self.assertEqual(self._submit().status_code, 401)
        self.assertEqual(self.client.get(f"/api/v1/builds/{build_id}").status_code, 401)
        self.assertEqual(self.client.get("/api/v1/builds").status_code, 401)

    def test_non_member_cannot_submit_or_get(self):
        build_id = self._submit(self.dev_tok).json()["build_id"]
        self.assertEqual(self._submit(self.ext_tok).status_code, 403)
        resp = self.client.get(
            f"/api/v1/builds/{build_id}", headers={"Authorization": f"Bearer {self.ext_tok}"}
        )
        self.assertEqual(resp.status_code, 403)

    def test_submit_unknown_project_is_404(self):
        self.assertEqual(self._submit(self.dev_tok, project="ghost").status_code, 404)

    def test_list_builds_only_shows_member_projects(self):
        mine = self._submit(self.dev_tok).json()["build_id"]
        theirs = self._submit(self.rel_tok, project="secret").json()["build_id"]

        def ids(token):
            resp = self.client.get("/api/v1/builds", headers={"Authorization": f"Bearer {token}"})
            self.assertEqual(resp.status_code, 200)
            return {b["id"] for b in resp.json()}

        self.assertEqual(ids(self.dev_tok), {mine})
        self.assertEqual(ids(self.ext_tok), set())
        self.assertEqual(ids(self.db.tokens.issue(user_id="sysadmin")["token"]), {mine, theirs})

    def test_cancel_permissions(self):
        build_id = self._submit(self.dev_tok).json()["build_id"]
        path = f"/api/v1/builds/{build_id}/cancel"

        def post(token):
            return self.client.post(path, headers={"Authorization": f"Bearer {token}"})

        self.assertEqual(self.client.post(path).status_code, 401)
        self.assertEqual(post(self.ext_tok).status_code, 403)
        self.assertEqual(post(self.rel_tok).status_code, 403)
        self.assertEqual(post(self.dev_tok).status_code, 200)
        self.assertEqual(self.db.builds.get(build_id)["status"], "cancelled")
        self.assertEqual(post(self.dev_tok).status_code, 409)

        other = self._submit(self.dev_tok).json()["build_id"]
        resp = self.client.post(
            f"/api/v1/builds/{other}/cancel", headers={"Authorization": f"Bearer {self.adm_tok}"}
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            self.client.post(
                "/api/v1/builds/bld-none/cancel", headers={"Authorization": f"Bearer {self.adm_tok}"}
            ).status_code,
            404,
        )

    def test_release_build_rejected_unauthenticated(self):
        req = BuildSubmitRequest(
            project="firmware",
            worktree_digest="tree-rel-1",
            target_arch="amd64",
            is_release=True,
        )
        resp = self.client.post("/api/v1/builds", json=req.model_dump())
        self.assertEqual(resp.status_code, 401)

    def test_release_build_rejected_developer_role(self):
        req = BuildSubmitRequest(
            project="firmware",
            worktree_digest="tree-rel-1",
            target_arch="amd64",
            is_release=True,
        )
        resp = self.client.post(
            "/api/v1/builds",
            json=req.model_dump(),
            headers={"Authorization": f"Bearer {self.dev_tok}"},
        )
        self.assertEqual(resp.status_code, 403)

    def test_release_build_rejected_non_member(self):
        req = BuildSubmitRequest(
            project="firmware",
            worktree_digest="tree-rel-1",
            target_arch="amd64",
            is_release=True,
        )
        resp = self.client.post(
            "/api/v1/builds",
            json=req.model_dump(),
            headers={"Authorization": f"Bearer {self.ext_tok}"},
        )
        self.assertEqual(resp.status_code, 403)

    def test_release_build_allowed_releaser_role(self):
        req = BuildSubmitRequest(
            project="firmware",
            worktree_digest="tree-rel-carol",
            target_arch="amd64",
            is_release=True,
        )
        resp = self.client.post(
            "/api/v1/builds",
            json=req.model_dump(),
            headers={"Authorization": f"Bearer {self.rel_tok}"},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()

        build = self.db.builds.get(data["build_id"])
        self.assertTrue(build["is_release"])

    def test_release_build_allowed_admin_role(self):
        req = BuildSubmitRequest(
            project="firmware",
            worktree_digest="tree-rel-dave",
            target_arch="amd64",
            is_release=True,
        )
        resp = self.client.post(
            "/api/v1/builds",
            json=req.model_dump(),
            headers={"Authorization": f"Bearer {self.adm_tok}"},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()

        build = self.db.builds.get(data["build_id"])
        self.assertTrue(build["is_release"])

    def test_build_status_inspection(self):
        req = BuildSubmitRequest(
            project="firmware",
            worktree_digest="tree-inspect",
            target_arch="amd64",
        )
        headers = {"Authorization": f"Bearer {self.dev_tok}"}
        submit_resp = self.client.post("/api/v1/builds", json=req.model_dump(), headers=headers)
        build_id = submit_resp.json()["build_id"]

        resp = self.client.get(f"/api/v1/builds/{build_id}", headers=headers)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["id"], build_id)
        self.assertEqual(data["status"], "queued")
        self.assertIn("created_at", data)
        self.assertIn("artifact_urls", data)

        resp_404 = self.client.get("/api/v1/builds/bld-nonexistent", headers=headers)
        self.assertEqual(resp_404.status_code, 404)


class TransientSecretsLifecycleTest(Test):
    """Test transient secrets in-memory storage and purge lifecycle."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="seine-test-secrets-")
        self.db_path = os.path.join(self.tmp_dir, "test.db")
        self.db = Database(self.db_path)
        self.app = create_app(db=self.db, settings=S3_SETTINGS, storage_provider=mock.MagicMock())
        self.client = TestClient(self.app)

        self.worker_id = "worker-1"
        self.worker_token = "tok-w1"
        self.db.upsert_worker(
            worker_id=self.worker_id,
            hostname="host1",
            native_arch="amd64",
            arch_scores={"amd64": 1.0},
            free_disk_gb=50.0,
            token=self.worker_token,
        )
        self.db.ensure_project("testproj")
        self.db.users.create("dev")
        self.db.projects.add_member("testproj", "dev", role="developer")
        dev_token = self.db.tokens.issue(user_id="dev")["token"]
        self.user_headers = {"Authorization": f"Bearer {dev_token}"}

    def tearDown(self):
        self.db.close()
        if os.path.exists(self.tmp_dir):
            shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_transient_secrets_memory_lifecycle(self):
        feed_secrets = {"feeds": {"https://repo.example/debian": {
            "login": "secret-feed-xyz", "password": "supersecret123"}}}
        req = BuildSubmitRequest(
            project="testproj",
            worktree_digest="tree1",
            target_arch="amd64",
            transient_secrets=feed_secrets,
        )
        resp = self.client.post("/api/v1/builds", json=req.model_dump(), headers=self.user_headers)
        self.assertEqual(resp.status_code, 200)
        build_id = resp.json()["build_id"]

        self.assertIn(build_id, self.app.state.transient_secrets)
        self.assertEqual(self.app.state.transient_secrets[build_id], feed_secrets)

        cur = self.db.conn.execute("SELECT * FROM builds WHERE id = ?", (build_id,))
        row = dict(cur.fetchone())
        self.assertNotIn("secret-feed-xyz", str(row))
        self.assertNotIn("supersecret123", str(row))

        claim_headers = {"Authorization": f"Bearer {self.worker_token}"}
        claim_req = ClaimJobRequest(worker_id=self.worker_id)
        claim_resp = self.client.post(
            "/api/v1/workers/claim",
            json=claim_req.model_dump(),
            headers=claim_headers,
        )
        self.assertEqual(claim_resp.status_code, 200)
        manifest = claim_resp.json()
        self.assertEqual(manifest["transient_secrets"], feed_secrets)

        update_req = JobStatusUpdateRequest(
            worker_id=self.worker_id,
            build_id=build_id,
            job_id=manifest["job_id"],
            status="completed",
        )
        status_resp = self.client.post(
            f"/api/v1/workers/jobs/{manifest['job_id']}/status",
            json=update_req.model_dump(),
            headers=claim_headers,
        )
        self.assertEqual(status_resp.status_code, 200)

        self.assertNotIn(build_id, self.app.state.transient_secrets)

    def test_transient_secrets_purged_on_job_failure(self):
        feed_secrets = {"feeds": {"https://repo.example/debian": {
            "login": "bob", "password": "vault-temp-key"}}}
        req = BuildSubmitRequest(
            project="testproj",
            worktree_digest="tree2",
            target_arch="amd64",
            transient_secrets=feed_secrets,
        )
        resp = self.client.post("/api/v1/builds", json=req.model_dump(), headers=self.user_headers)
        build_id = resp.json()["build_id"]

        self.assertIn(build_id, self.app.state.transient_secrets)

        claim_headers = {"Authorization": f"Bearer {self.worker_token}"}
        claim_req = ClaimJobRequest(worker_id=self.worker_id)
        claim_resp = self.client.post(
            "/api/v1/workers/claim",
            json=claim_req.model_dump(),
            headers=claim_headers,
        )
        manifest = claim_resp.json()

        update_req = JobStatusUpdateRequest(
            worker_id=self.worker_id,
            build_id=build_id,
            job_id=manifest["job_id"],
            status="failed",
        )
        status_resp = self.client.post(
            f"/api/v1/workers/jobs/{manifest['job_id']}/status",
            json=update_req.model_dump(),
            headers=claim_headers,
        )
        self.assertEqual(status_resp.status_code, 200)

        self.assertNotIn(build_id, self.app.state.transient_secrets)


class NoEnrollmentTokenTest(Test):
    """An app without an enrollment token must not enroll anybody."""

    def setUp(self):
        self.db = Database(":memory:")

    def tearDown(self):
        self.db.close()

    def test_register_rejected_when_unset(self):
        client = TestClient(create_app(db=self.db))
        body = RegisterWorkerRequest(
            worker_id="w1", hostname="h",
            capabilities=WorkerCapabilities(native_arch="amd64", arch_scores={"amd64": 1.0}, free_disk_gb=1.0),
        ).model_dump()
        for auth in ("Bearer ", "Bearer seine-dev-enrollment-token", "Bearer None"):
            resp = client.post("/api/v1/workers/register", json=body, headers={"Authorization": auth})
            self.assertEqual(resp.status_code, 401, auth)

    def test_no_module_level_app(self):
        from seine.distributed.server import api
        self.assertFalse(hasattr(api, "app"))
        self.assertFalse(hasattr(api, "get_default_db"))


class ServerCLITest(Test):
    """Test server CLI argument parsing and settings propagation."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="seine-test-cli-")
        env = {k: v for k, v in os.environ.items() if not k.startswith("SEINE_")}
        self.patcher = mock.patch.dict(os.environ, env, clear=True)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _run(self, *extra):
        args = ["seine-server", "run", "--db-path", os.path.join(self.tmp_dir, "cli.db"), *extra]
        with mock.patch("sys.argv", args), mock.patch("uvicorn.run") as run:
            cli_main()
        return run

    def test_cli_run_options(self):
        run = self._run(
            "--host", "127.0.0.1", "--port", "9999", "--enrollment-token", "cli-secret-token",
        )
        run.assert_called_once()
        app = run.call_args[0][0]
        self.assertEqual(app.state.enrollment_token, "cli-secret-token")
        self.assertEqual(run.call_args[1]["host"], "127.0.0.1")
        self.assertEqual(run.call_args[1]["port"], 9999)
        self.assertIsNone(run.call_args[1]["ssl_certfile"])

    def test_cli_refuses_to_start_without_token(self):
        with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            with self.assertRaises(SystemExit) as ctx:
                self._run()
        self.assertNotEqual(ctx.exception.code, 0)
        self.assertIn("enrollment token", err.getvalue())

    def test_cli_refuses_half_a_tls_pair(self):
        with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            with self.assertRaises(SystemExit) as ctx:
                self._run("--enrollment-token", "t", "--tls-cert", "cert.pem")
        self.assertNotEqual(ctx.exception.code, 0)
        self.assertIn("--tls-key", err.getvalue())

    def test_cli_passes_tls_to_uvicorn(self):
        run = self._run("--enrollment-token", "t", "--tls-cert", "c.pem", "--tls-key", "k.pem")
        self.assertEqual(run.call_args[1]["ssl_certfile"], "c.pem")
        self.assertEqual(run.call_args[1]["ssl_keyfile"], "k.pem")

    def test_cli_warns_on_public_bind_without_tls(self):
        with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            self._run("--enrollment-token", "t", "--host", "0.0.0.0")
        self.assertIn("without TLS", err.getvalue())

    def test_cli_default_bind_is_loopback_and_quiet(self):
        with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            run = self._run("--enrollment-token", "t")
        self.assertEqual(run.call_args[1]["host"], "127.0.0.1")
        self.assertEqual(err.getvalue(), "")

    def test_cli_passes_job_lost_grace_to_the_scheduler(self):
        run = self._run("--enrollment-token", "t", "--job-lost-grace", "12")
        self.assertEqual(run.call_args[0][0].state.db.scheduler.job_lost_grace, 12.0)

    def test_cli_refuses_a_zero_job_lost_grace(self):
        with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            with self.assertRaises(SystemExit):
                self._run("--enrollment-token", "t", "--job-lost-grace", "0")
        self.assertIn("job_lost_grace", err.getvalue())

    def test_cli_reads_token_from_environment(self):
        os.environ["SEINE_ENROLLMENT_TOKEN"] = "env-token"
        run = self._run()
        self.assertEqual(run.call_args[0][0].state.enrollment_token, "env-token")
