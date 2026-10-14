# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for OSTree tip database operations and API endpoints."""

import os
import shutil
import tempfile

from avocado import Test
from fastapi.testclient import TestClient

from seine.distributed.common.models import RegisterWorkerRequest, WorkerCapabilities
from seine.distributed.server.api import create_app
from seine.distributed.server.db import Database


COMMIT_A = "a" * 64
COMMIT_B = "b" * 64
COMMIT_C = "c" * 64


class OstreeTipDbTest(Test):
    """Test OstreeTipRepo database operations."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="seine-test-ostree-db-")
        self.db = Database(os.path.join(self.tmp_dir, "test.db"))
        self.db.projects.create("alpha")

    def tearDown(self):
        self.db.close()
        if os.path.exists(self.tmp_dir):
            shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_get_nonexistent(self):
        self.assertIsNone(self.db.ostree_tips.get("alpha", "main", "os/debian/x86_64"))
        self.assertIsNone(self.db.ostree_tips.get_tip("alpha", "main", "os/debian/x86_64"))

    def test_first_commit_tip_insert(self):
        ok = self.db.ostree_tips.update_tip(
            "alpha", "main", "os/debian/x86_64", COMMIT_A, parent=None
        )
        self.assertTrue(ok)
        self.assertEqual(
            self.db.ostree_tips.get_tip("alpha", "main", "os/debian/x86_64"), COMMIT_A
        )

        record = self.db.ostree_tips.get("alpha", "main", "os/debian/x86_64")
        self.assertIsNotNone(record)
        self.assertEqual(record["project"], "alpha")
        self.assertEqual(record["branch"], "main")
        self.assertEqual(record["ref"], "os/debian/x86_64")
        self.assertEqual(record["commit"], COMMIT_A)
        self.assertIsNone(record["parent"])

    def test_cas_success_on_matching_parent(self):
        self.db.ostree_tips.update_tip("alpha", "main", "os/debian/x86_64", COMMIT_A, parent=None)
        ok = self.db.ostree_tips.update_tip(
            "alpha", "main", "os/debian/x86_64", COMMIT_B, parent=COMMIT_A
        )
        self.assertTrue(ok)
        self.assertEqual(
            self.db.ostree_tips.get_tip("alpha", "main", "os/debian/x86_64"), COMMIT_B
        )

        record = self.db.ostree_tips.get("alpha", "main", "os/debian/x86_64")
        self.assertEqual(record["parent"], COMMIT_A)

    def test_cas_conflict_on_mismatched_parent(self):
        self.db.ostree_tips.update_tip("alpha", "main", "os/debian/x86_64", COMMIT_A, parent=None)
        ok = self.db.ostree_tips.update_tip(
            "alpha", "main", "os/debian/x86_64", COMMIT_C, parent=COMMIT_B
        )
        self.assertFalse(ok)
        self.assertEqual(
            self.db.ostree_tips.get_tip("alpha", "main", "os/debian/x86_64"), COMMIT_A
        )

    def test_list_by_project_and_branch(self):
        self.db.ostree_tips.update_tip("alpha", "main", "os/debian/x86_64", COMMIT_A)
        self.db.ostree_tips.update_tip("alpha", "main", "os/debian/arm64", COMMIT_B)
        self.db.ostree_tips.update_tip("alpha", "staging", "os/debian/x86_64", COMMIT_C)

        all_tips = self.db.ostree_tips.list_by_project("alpha")
        self.assertEqual(len(all_tips), 3)

        main_tips = self.db.ostree_tips.list_by_project("alpha", branch="main")
        self.assertEqual(len(main_tips), 2)
        refs = {t["ref"] for t in main_tips}
        self.assertEqual(refs, {"os/debian/x86_64", "os/debian/arm64"})

    def test_delete_tip(self):
        self.db.ostree_tips.update_tip("alpha", "main", "os/debian/x86_64", COMMIT_A)
        deleted = self.db.ostree_tips.delete("alpha", "main", "os/debian/x86_64")
        self.assertTrue(deleted)
        self.assertIsNone(self.db.ostree_tips.get("alpha", "main", "os/debian/x86_64"))

    def test_project_cascade_delete(self):
        self.db.ostree_tips.update_tip("alpha", "main", "os/debian/x86_64", COMMIT_A)
        self.db.projects.delete("alpha")
        self.assertEqual(self.db.ostree_tips.list_by_project("alpha"), [])


class OstreeTipApiTest(Test):
    """Test OSTree tip API endpoints and authorization."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="seine-test-ostree-api-")
        self.db = Database(os.path.join(self.tmp_dir, "test.db"))
        self.app = create_app(db=self.db, enrollment_token="enroll-token")
        self.client = TestClient(self.app)

        self.db.projects.create("alpha")
        self.db.users.create("alice", is_admin=True)
        self.db.users.create("bob")
        self.db.users.create("charlie")
        self.db.users.create("david")

        self.db.projects.add_member("alpha", "bob", role="releaser")
        self.db.projects.add_member("alpha", "charlie", role="developer")

        self.token_admin = self.db.tokens.issue(user_id="alice", kind="pat")["token"]
        self.token_releaser = self.db.tokens.issue(user_id="bob", kind="pat")["token"]
        self.token_developer = self.db.tokens.issue(user_id="charlie", kind="pat")["token"]
        self.token_outsider = self.db.tokens.issue(user_id="david", kind="pat")["token"]

        reg_req = RegisterWorkerRequest(
            worker_id="worker-1",
            hostname="worker1.local",
            capabilities=WorkerCapabilities(
                native_arch="amd64",
                arch_scores={"amd64": 1.0},
                free_disk_gb=50.0,
            ),
        )
        reg_resp = self.client.post(
            "/api/v1/workers/register",
            json=reg_req.model_dump(),
            headers={"Authorization": "Bearer enroll-token"},
        )
        self.assertEqual(reg_resp.status_code, 200)
        self.token_worker = reg_resp.json()["worker_token"]

    def tearDown(self):
        self.db.close()
        if os.path.exists(self.tmp_dir):
            shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_get_tip_not_found(self):
        headers = {"Authorization": f"Bearer {self.token_developer}"}
        resp = self.client.get(
            "/api/v1/projects/alpha/ostree/tip",
            params={"branch": "main", "ref": "os/debian/x86_64"},
            headers=headers,
        )
        self.assertEqual(resp.status_code, 404)

    def test_worker_can_update_and_read_tip(self):
        worker_headers = {"Authorization": f"Bearer {self.token_worker}"}
        payload = {
            "branch": "main",
            "ref": "os/debian/x86_64",
            "commit": COMMIT_A,
            "parent": None,
        }
        resp = self.client.post(
            "/api/v1/projects/alpha/ostree/tip", json=payload, headers=worker_headers
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["commit"], COMMIT_A)
        self.assertIsNone(data["parent"])

        dev_headers = {"Authorization": f"Bearer {self.token_developer}"}
        get_resp = self.client.get(
            "/api/v1/projects/alpha/ostree/tip",
            params={"branch": "main", "ref": "os/debian/x86_64"},
            headers=dev_headers,
        )
        self.assertEqual(get_resp.status_code, 200)
        self.assertEqual(get_resp.json()["commit"], COMMIT_A)

    def test_releaser_and_admin_can_update_tip(self):
        releaser_headers = {"Authorization": f"Bearer {self.token_releaser}"}
        payload1 = {
            "branch": "main",
            "ref": "os/debian/x86_64",
            "commit": COMMIT_A,
            "parent": None,
        }
        resp1 = self.client.post(
            "/api/v1/projects/alpha/ostree/tip", json=payload1, headers=releaser_headers
        )
        self.assertEqual(resp1.status_code, 200)

        admin_headers = {"Authorization": f"Bearer {self.token_admin}"}
        payload2 = {
            "branch": "main",
            "ref": "os/debian/x86_64",
            "commit": COMMIT_B,
            "parent": COMMIT_A,
        }
        resp2 = self.client.post(
            "/api/v1/projects/alpha/ostree/tip", json=payload2, headers=admin_headers
        )
        self.assertEqual(resp2.status_code, 200)
        self.assertEqual(resp2.json()["commit"], COMMIT_B)

    def test_developer_and_outsider_cannot_update_tip(self):
        dev_headers = {"Authorization": f"Bearer {self.token_developer}"}
        payload = {
            "branch": "main",
            "ref": "os/debian/x86_64",
            "commit": COMMIT_A,
            "parent": None,
        }
        resp = self.client.post(
            "/api/v1/projects/alpha/ostree/tip", json=payload, headers=dev_headers
        )
        self.assertEqual(resp.status_code, 403)

        out_headers = {"Authorization": f"Bearer {self.token_outsider}"}
        resp_out = self.client.post(
            "/api/v1/projects/alpha/ostree/tip", json=payload, headers=out_headers
        )
        self.assertEqual(resp_out.status_code, 403)

        resp_anon = self.client.post("/api/v1/projects/alpha/ostree/tip", json=payload)
        self.assertEqual(resp_anon.status_code, 401)

    def test_cas_conflict_returns_409(self):
        worker_headers = {"Authorization": f"Bearer {self.token_worker}"}
        payload1 = {
            "branch": "main",
            "ref": "os/debian/x86_64",
            "commit": COMMIT_A,
            "parent": None,
        }
        resp1 = self.client.post(
            "/api/v1/projects/alpha/ostree/tip", json=payload1, headers=worker_headers
        )
        self.assertEqual(resp1.status_code, 200)

        # Mismatched parent
        payload_conflict = {
            "branch": "main",
            "ref": "os/debian/x86_64",
            "commit": COMMIT_C,
            "parent": COMMIT_B,
        }
        resp2 = self.client.post(
            "/api/v1/projects/alpha/ostree/tip", json=payload_conflict, headers=worker_headers
        )
        self.assertEqual(resp2.status_code, 409)

    def test_validation_errors(self):
        worker_headers = {"Authorization": f"Bearer {self.token_worker}"}
        # Bad ref with path traversal
        bad_ref = {
            "branch": "main",
            "ref": "../bad/ref",
            "commit": COMMIT_A,
            "parent": None,
        }
        resp1 = self.client.post(
            "/api/v1/projects/alpha/ostree/tip", json=bad_ref, headers=worker_headers
        )
        self.assertEqual(resp1.status_code, 400)

        # Bad commit hash
        bad_commit = {
            "branch": "main",
            "ref": "os/debian/x86_64",
            "commit": "not-valid-hex",
            "parent": None,
        }
        resp2 = self.client.post(
            "/api/v1/projects/alpha/ostree/tip", json=bad_commit, headers=worker_headers
        )
        self.assertEqual(resp2.status_code, 400)

    def test_list_tips_endpoint(self):
        worker_headers = {"Authorization": f"Bearer {self.token_worker}"}
        self.client.post(
            "/api/v1/projects/alpha/ostree/tip",
            json={"branch": "main", "ref": "os/x86_64", "commit": COMMIT_A},
            headers=worker_headers,
        )
        self.client.post(
            "/api/v1/projects/alpha/ostree/tip",
            json={"branch": "dev", "ref": "os/x86_64", "commit": COMMIT_B},
            headers=worker_headers,
        )

        dev_headers = {"Authorization": f"Bearer {self.token_developer}"}
        resp = self.client.get("/api/v1/projects/alpha/ostree/tips", headers=dev_headers)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()), 2)

        resp_filtered = self.client.get(
            "/api/v1/projects/alpha/ostree/tips", params={"branch": "main"}, headers=dev_headers
        )
        self.assertEqual(resp_filtered.status_code, 200)
        self.assertEqual(len(resp_filtered.json()), 1)
        self.assertEqual(resp_filtered.json()[0]["branch"], "main")

    def test_claim_job_attaches_updates_storage_for_release_image(self):
        self.app.state.settings.s3_endpoint = "http://s3.local:9000"
        self.app.state.settings.s3_default = {
            "dev": {"access_key": "key-dev", "secret_key": "sec-dev"},
            "prod": {"access_key": "key-prod", "secret_key": "sec-prod"},
        }

        self.db.create_build("bld-rel", "alpha", "amd64", "digest1", is_release=True)
        worker_headers = {"Authorization": f"Bearer {self.token_worker}"}
        resp = self.client.post(
            "/api/v1/workers/claim",
            json={"worker_id": "worker-1"},
            headers=worker_headers,
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIsNotNone(data.get("updates_storage"))
        self.assertEqual(data["updates_storage"]["bucket"], "seine-alpha-updates")

    def test_claim_job_non_release_has_no_updates_storage(self):
        self.app.state.settings.s3_endpoint = "http://s3.local:9000"
        self.app.state.settings.s3_default = {
            "dev": {"access_key": "key-dev", "secret_key": "sec-dev"},
            "prod": {"access_key": "key-prod", "secret_key": "sec-prod"},
        }

        self.db.create_build("bld-dev", "alpha", "amd64", "digest2", is_release=False)
        worker_headers = {"Authorization": f"Bearer {self.token_worker}"}
        resp = self.client.post(
            "/api/v1/workers/claim",
            json={"worker_id": "worker-1"},
            headers=worker_headers,
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIsNone(data.get("updates_storage"))

    def test_build_artifacts_endpoint_serves_ostree_payload_artifacts(self):
        from unittest.mock import MagicMock

        mock_provider = MagicMock()
        mock_provider.generate_download_url.return_value = "http://storage.local/download"
        mock_stream = MagicMock()
        mock_stream.iter_content.return_value = [b"delta-bytes"]
        mock_provider.open_artifact.return_value = mock_stream
        self.app.state.storage_provider = mock_provider

        self.db.create_build("bld-ostree", "alpha", "amd64", "digest3", is_release=True)
        meta = [
            {
                "name": "disk.ostree-commit",
                "size": 65,
                "sha256": "11" * 32,
                "key": "artifacts/alpha/bld-ostree/disk.ostree-commit",
            },
            {
                "name": "disk.ostree-summary",
                "size": 256,
                "sha256": "22" * 32,
                "key": "artifacts/alpha/bld-ostree/disk.ostree-summary",
            },
            {
                "name": "disk.static-delta-20",
                "size": 1024,
                "sha256": "33" * 32,
                "key": "artifacts/alpha/bld-ostree/disk.static-delta-20",
            },
        ]
        self.db.builds.update_status("bld-ostree", "completed", artifact_meta=meta)

        dev_headers = {"Authorization": f"Bearer {self.token_developer}"}
        resp = self.client.get("/api/v1/builds/bld-ostree", headers=dev_headers)
        self.assertEqual(resp.status_code, 200)
        names = [a["name"] for a in resp.json()["artifacts"]]
        self.assertIn("disk.ostree-commit", names)
        self.assertIn("disk.ostree-summary", names)
        self.assertIn("disk.static-delta-20", names)

        dl_resp = self.client.get(
            "/api/v1/builds/bld-ostree/artifacts/disk.static-delta-20",
            headers=dev_headers,
        )
        self.assertEqual(dl_resp.status_code, 200)
        self.assertEqual(dl_resp.content, b"delta-bytes")
        mock_provider.open_artifact.assert_called_with(
            "alpha", "artifacts/alpha/bld-ostree/disk.static-delta-20"
        )



class OstreeUploadArtifactsTest(Test):
    """Test worker artifact harvesting, cumulative ostree push, and CAS protocol."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="seine-test-ostree-upload-")
        self.job_dir = os.path.join(self.tmp_dir, "job-1")
        self.deploy_dir = os.path.join(self.job_dir, "build", "deploy")
        self.repo_dir = os.path.join(self.deploy_dir, "disk-payload", "repo")
        os.makedirs(os.path.join(self.repo_dir, "objects"), exist_ok=True)

        with open(os.path.join(self.deploy_dir, "disk.img"), "wb") as f:
            f.write(b"raw-image-bytes")
        with open(os.path.join(self.deploy_dir, "disk.ostree-commit"), "w") as f:
            f.write(f"{COMMIT_A}\n")
        with open(os.path.join(self.deploy_dir, "disk.ostree-parent"), "w") as f:
            f.write(f"{COMMIT_B}\n")
        with open(os.path.join(self.deploy_dir, "disk.ostree-ref"), "w") as f:
            f.write("os/debian/x86_64\n")
        self.uki_dir = os.path.join(self.deploy_dir, "disk-payload", "uki")
        os.makedirs(self.uki_dir, exist_ok=True)
        with open(os.path.join(self.uki_dir, "debian-1.efi"), "wb") as f:
            f.write(b"pe-binary")

    def tearDown(self):
        if os.path.exists(self.tmp_dir):
            shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_find_ostree_payloads(self):
        from seine.distributed.agent.executor import find_ostree_payloads

        payloads = find_ostree_payloads(self.job_dir)
        self.assertEqual(len(payloads), 1)
        p = payloads[0]
        self.assertEqual(p["stem"], "disk")
        self.assertEqual(p["commit"], COMMIT_A)
        self.assertEqual(p["parent"], COMMIT_B)
        self.assertEqual(p["ref"], "os/debian/x86_64")
        self.assertEqual(p["repo_dir"], self.repo_dir)
        self.assertEqual(p["uki_dir"], self.uki_dir)

    def test_upload_artifacts_release_protocol(self):
        from unittest.mock import MagicMock
        from seine.distributed.agent.executor import upload_artifacts

        provider = MagicMock()
        provider.push_artifact_info.return_value = {
            "name": "disk.img",
            "key": "artifacts/alpha/b1/disk.img",
            "sha256": "abc",
            "size": 15,
        }
        on_tip_update = MagicMock(return_value=True)

        artifacts = upload_artifacts(
            self.job_dir,
            "alpha",
            "b1",
            provider=provider,
            branch="main",
            is_release=True,
            on_tip_update=on_tip_update,
        )

        provider.push_ostree_objects.assert_called_once_with(
            "alpha", "main", "os/debian/x86_64", self.repo_dir
        )
        on_tip_update.assert_called_once_with(
            "alpha", "main", "os/debian/x86_64", COMMIT_A, COMMIT_B
        )
        provider.push_ostree_metadata.assert_called_once_with(
            "alpha", "main", "os/debian/x86_64", self.repo_dir
        )
        self.assertTrue(len(artifacts) >= 4)

    def test_upload_artifacts_aborts_on_tip_conflict(self):
        from unittest.mock import MagicMock
        from seine.distributed.agent.executor import upload_artifacts

        provider = MagicMock()
        on_tip_update = MagicMock(return_value=False)

        with self.assertRaises(RuntimeError) as ctx:
            upload_artifacts(
                self.job_dir,
                "alpha",
                "b1",
                provider=provider,
                branch="main",
                is_release=True,
                on_tip_update=on_tip_update,
            )

        self.assertIn("OSTree tip conflict", str(ctx.exception))
        provider.push_ostree_objects.assert_called_once()
        provider.push_ostree_metadata.assert_not_called()

    def test_upload_artifacts_non_release_skips_cumulative_sync(self):
        from unittest.mock import MagicMock
        from seine.distributed.agent.executor import upload_artifacts

        provider = MagicMock()
        provider.push_artifact_info.return_value = {
            "name": "disk.img",
            "key": "artifacts/alpha/b1/disk.img",
            "sha256": "abc",
            "size": 15,
        }
        on_tip_update = MagicMock(return_value=True)

        upload_artifacts(
            self.job_dir,
            "alpha",
            "b1",
            provider=provider,
            branch="main",
            is_release=False,
            on_tip_update=on_tip_update,
        )

        provider.push_ostree_objects.assert_not_called()
        provider.push_ostree_metadata.assert_not_called()
        on_tip_update.assert_not_called()

    def test_upload_artifacts_syncs_to_updates_bucket(self):
        from unittest.mock import MagicMock
        from seine.distributed.agent.executor import upload_artifacts

        provider = MagicMock()
        updates_provider = MagicMock()
        on_tip_update = MagicMock(return_value=True)

        upload_artifacts(
            self.job_dir,
            "alpha",
            "b1",
            provider=provider,
            branch="main",
            is_release=True,
            on_tip_update=on_tip_update,
            updates_provider=updates_provider,
        )

        updates_provider.sync_ostree_updates.assert_called_once_with(
            "alpha", "main", "os/debian/x86_64", self.repo_dir, uki_dir=self.uki_dir
        )

    def test_upload_artifacts_tip_conflict_does_not_touch_updates_bucket(self):
        from unittest.mock import MagicMock
        from seine.distributed.agent.executor import upload_artifacts

        provider = MagicMock()
        updates_provider = MagicMock()
        on_tip_update = MagicMock(return_value=False)

        with self.assertRaises(RuntimeError):
            upload_artifacts(
                self.job_dir,
                "alpha",
                "b1",
                provider=provider,
                branch="main",
                is_release=True,
                on_tip_update=on_tip_update,
                updates_provider=updates_provider,
            )

        updates_provider.sync_ostree_updates.assert_not_called()

    def test_upload_artifacts_first_commit_vs_later_deltas(self):
        from unittest.mock import MagicMock
        from seine.distributed.agent.executor import harvest_artifacts

        # First commit: deploy_dir has no static-delta files
        files = [os.path.basename(f) for f in harvest_artifacts(self.job_dir)]
        self.assertIn("disk.img", files)
        self.assertIn("disk.ostree-commit", files)
        self.assertFalse(any("static-delta" in f for f in files))

        # Later commit: static delta added
        delta_path = os.path.join(self.deploy_dir, "disk.static-delta-20")
        with open(delta_path, "w") as f:
            f.write("from=20\n")

        files_later = [os.path.basename(f) for f in harvest_artifacts(self.job_dir)]
        self.assertIn("disk.static-delta-20", files_later)

