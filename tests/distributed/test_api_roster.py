# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import contextlib
import os
import shutil
import sys
import tempfile
from unittest import mock

import avocado

path_to_self = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.insert(0, path_to_sources)

@contextlib.contextmanager
def _distributed_required(test):
    try:
        yield
    except ImportError as e:
        test.cancel("distributed dependencies are not installed: %s" % e)


class ApiRosterTest(avocado.Test):
    """
    :avocado: tags=distributed
    """

    def setUp(self):
        with _distributed_required(self):
            from fastapi.testclient import TestClient
            from seine.distributed.client.admin import AdminClient
            from seine.distributed.server.api import create_app
            from seine.distributed.server.db import Database
        self.TestClient = TestClient
        self.AdminClient = AdminClient
        self.create_app = create_app
        self.Database = Database

        self.tmp_dir = tempfile.mkdtemp(prefix="seine-test-roster-")
        self.db_path = os.path.join(self.tmp_dir, "test.db")
        self.db = self.Database(self.db_path)
        self.app = self.create_app(db=self.db, enrollment_token="enroll-token")
        self.client = self.TestClient(self.app)

        # Seed admin user and token
        self.db.users.create("admin-user", is_admin=True)
        admin_rec = self.db.tokens.issue("admin-user", kind="pat")
        self.admin_token = admin_rec["token"]

        # Seed regular developer user and token
        self.db.users.create("dev-user", is_admin=False)
        dev_rec = self.db.tokens.issue("dev-user", kind="pat")
        self.dev_token = dev_rec["token"]

        # Seed projects and memberships
        self.db.projects.create("demo-project")
        self.db.projects.create("prod-project")
        self.db.projects.add_member("demo-project", "dev-user", "developer")
        self.db.projects.add_member("prod-project", "dev-user", "releaser")

    def tearDown(self):
        self.db.close()
        if os.path.exists(self.tmp_dir):
            shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_me_endpoint_developer(self):
        headers = {"Authorization": f"Bearer {self.dev_token}"}
        resp = self.client.get("/api/v1/me", headers=headers)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["id"], "dev-user")
        self.assertFalse(data["is_admin"])
        self.assertEqual(data["projects"], {
            "demo-project": "developer",
            "prod-project": "releaser",
        })

    def test_me_endpoint_admin(self):
        headers = {"Authorization": f"Bearer {self.admin_token}"}
        resp = self.client.get("/api/v1/me", headers=headers)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["id"], "admin-user")
        self.assertTrue(data["is_admin"])
        self.assertEqual(data["projects"], {})

    def _patch_me(self, token, default_project):
        return self.client.patch(
            "/api/v1/me", json={"default_project": default_project},
            headers={"Authorization": f"Bearer {token}"})

    def _me(self, token):
        return self.client.get(
            "/api/v1/me", headers={"Authorization": f"Bearer {token}"}).json()

    def test_default_project_is_none_until_chosen(self):
        self.assertIsNone(self._me(self.dev_token)["default_project"])
        # No implicit default for an administrator either.
        self.assertIsNone(self._me(self.admin_token)["default_project"])

    def test_member_sets_and_clears_the_default_project(self):
        resp = self._patch_me(self.dev_token, "demo-project")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["default_project"], "demo-project")
        self.assertEqual(self._me(self.dev_token)["default_project"], "demo-project")
        resp = self._patch_me(self.dev_token, None)
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(self._me(self.dev_token)["default_project"])

    def test_default_project_needs_membership_and_an_existing_project(self):
        self.db.projects.create("other-project")
        self.assertEqual(self._patch_me(self.dev_token, "other-project").status_code, 403)
        self.assertEqual(self._patch_me(self.dev_token, "ghost-project").status_code, 404)
        self.assertEqual(self._patch_me(self.dev_token, "Bad Name").status_code, 400)
        self.assertIsNone(self._me(self.dev_token)["default_project"])

    def test_preferences_request_needs_the_field(self):
        resp = self.client.patch(
            "/api/v1/me", json={}, headers={"Authorization": f"Bearer {self.dev_token}"})
        self.assertEqual(resp.status_code, 422)

    def test_administrator_may_pick_any_project(self):
        resp = self._patch_me(self.admin_token, "prod-project")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._me(self.admin_token)["default_project"], "prod-project")

    def test_default_project_is_dropped_with_the_membership(self):
        self._patch_me(self.dev_token, "demo-project")
        self.db.projects.remove_member("demo-project", "dev-user")
        self.assertIsNone(self._me(self.dev_token)["default_project"])

    def test_demoted_administrator_loses_a_default_they_do_not_belong_to(self):
        self.db.users.create("other-admin", is_admin=True)
        self._patch_me(self.admin_token, "prod-project")
        self.db.users.update("admin-user", is_admin=False)
        self.assertIsNone(self._me(self.admin_token)["default_project"])

    def test_default_project_requires_authentication(self):
        resp = self.client.patch("/api/v1/me", json={"default_project": None})
        self.assertEqual(resp.status_code, 401)

    def test_admin_client_sets_the_default_project(self):
        calls = []

        def fake_patch(url, json=None, headers=None, timeout=None):
            calls.append((url, json, headers["Authorization"]))
            resp = mock.Mock(status_code=200)
            resp.json.return_value = {"id": "dev-user", "default_project": "demo-project"}
            return resp

        client = self.AdminClient("http://srv:8000", token="tok", insecure=True)
        with mock.patch("requests.Session.patch", side_effect=fake_patch):
            self.assertEqual(client.set_default_project("demo-project")["default_project"],
                             "demo-project")
            client.set_default_project(None)
        self.assertEqual(calls[0][:2], ("http://srv:8000/api/v1/me",
                                        {"default_project": "demo-project"}))
        self.assertEqual(calls[1][1], {"default_project": None})
        self.assertEqual(calls[0][2], "Bearer tok")

    def test_me_endpoint_unauthenticated(self):
        resp = self.client.get("/api/v1/me")
        self.assertEqual(resp.status_code, 401)

    def test_workers_roster_listing(self):
        headers = {"Authorization": f"Bearer {self.dev_token}"}
        resp = self.client.get("/api/v1/workers", headers=headers)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["workers"], [])

        # Register workers in db
        self.db.workers.register(
            id="worker-arm64",
            hostname="rockchip-node",
            native_arch="arm64",
            arch_scores={"arm64": 1.0, "amd64": 0.3},
            free_disk_gb=48.5,
            token="w-token-1",
            concurrency_slots=2,
        )
        self.db.workers.register(
            id="worker-amd64",
            hostname="build-server",
            native_arch="amd64",
            arch_scores={"amd64": 1.0, "arm64": 0.7},
            free_disk_gb=120.0,
            token="w-token-2",
            concurrency_slots=4,
        )

        resp = self.client.get("/api/v1/workers", headers=headers)
        self.assertEqual(resp.status_code, 200)
        workers = resp.json()["workers"]
        self.assertEqual(len(workers), 2)
        # Sorted by hostname ASC: build-server before rockchip-node
        self.assertEqual(workers[0]["hostname"], "build-server")
        self.assertEqual(workers[0]["id"], "worker-amd64")
        self.assertEqual(workers[0]["native_arch"], "amd64")
        self.assertEqual(workers[0]["concurrency_slots"], 4)
        self.assertEqual(workers[0]["free_disk_gb"], 120.0)
        self.assertEqual(workers[0]["status"], "online")
        self.assertNotIn("token", workers[0])
        self.assertNotIn("token_hash", workers[0])

        self.assertEqual(workers[1]["hostname"], "rockchip-node")
        self.assertEqual(workers[1]["id"], "worker-arm64")

    def test_worker_pause_and_resume_admin(self):
        self.db.workers.register(
            id="worker-1",
            hostname="host-1",
            native_arch="amd64",
            arch_scores={"amd64": 1.0},
            free_disk_gb=50.0,
            token="w-token",
        )
        headers = {"Authorization": f"Bearer {self.admin_token}"}

        # Pause worker
        resp = self.client.post("/api/v1/workers/worker-1/pause", json={"paused": True}, headers=headers)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"ok": True, "worker_id": "worker-1", "status": "paused"})
        worker = self.db.workers.get("worker-1")
        self.assertEqual(worker["status"], "paused")

        # Unpause worker
        resp = self.client.post("/api/v1/workers/worker-1/pause", json={"paused": False}, headers=headers)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"ok": True, "worker_id": "worker-1", "status": "online"})
        worker = self.db.workers.get("worker-1")
        self.assertEqual(worker["status"], "online")

        # Not found
        resp = self.client.post("/api/v1/workers/non-existent/pause", json={"paused": True}, headers=headers)
        self.assertEqual(resp.status_code, 404)

    def test_worker_pause_developer_forbidden(self):
        self.db.workers.register(
            id="worker-1",
            hostname="host-1",
            native_arch="amd64",
            arch_scores={"amd64": 1.0},
            free_disk_gb=50.0,
            token="w-token",
        )
        headers = {"Authorization": f"Bearer {self.dev_token}"}
        resp = self.client.post("/api/v1/workers/worker-1/pause", json={"paused": True}, headers=headers)
        self.assertEqual(resp.status_code, 403)

    def test_worker_delete_admin(self):
        self.db.workers.register(
            id="worker-to-del",
            hostname="host-del",
            native_arch="amd64",
            arch_scores={"amd64": 1.0},
            free_disk_gb=50.0,
            token="w-token",
        )
        headers = {"Authorization": f"Bearer {self.admin_token}"}
        resp = self.client.delete("/api/v1/workers/worker-to-del", headers=headers)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"ok": True, "deleted": "worker-to-del"})
        self.assertIsNone(self.db.workers.get("worker-to-del"))

        resp = self.client.delete("/api/v1/workers/worker-to-del", headers=headers)
        self.assertEqual(resp.status_code, 404)

    def test_worker_delete_developer_forbidden(self):
        headers = {"Authorization": f"Bearer {self.dev_token}"}
        resp = self.client.delete("/api/v1/workers/worker-x", headers=headers)
        self.assertEqual(resp.status_code, 403)

    def test_admin_client_methods(self):
        admin = self.AdminClient(token=self.admin_token)
        # Point requests session to TestClient transport
        import requests
        session = requests.Session()
        admin._headers = lambda: {"Authorization": f"Bearer {self.admin_token}"}

        # Override requests.get/post/delete with adapter to self.client
        def fake_get(url, headers=None, timeout=None, params=None):
            path = url.replace(admin.server_url, "")
            resp = self.client.get(path, headers=headers, params=params)
            mock_resp = mock.Mock()
            mock_resp.status_code = resp.status_code
            mock_resp.json = resp.json
            mock_resp.raise_for_status = lambda: None if resp.status_code < 400 else (_ for _ in ()).throw(requests.HTTPError(resp.text))
            return mock_resp

        def fake_post(url, json=None, headers=None, timeout=None):
            path = url.replace(admin.server_url, "")
            resp = self.client.post(path, json=json, headers=headers)
            mock_resp = mock.Mock()
            mock_resp.status_code = resp.status_code
            mock_resp.json = resp.json
            mock_resp.raise_for_status = lambda: None if resp.status_code < 400 else (_ for _ in ()).throw(requests.HTTPError(resp.text))
            return mock_resp

        def fake_delete(url, headers=None, timeout=None):
            path = url.replace(admin.server_url, "")
            resp = self.client.delete(path, headers=headers)
            mock_resp = mock.Mock()
            mock_resp.status_code = resp.status_code
            mock_resp.json = resp.json
            mock_resp.raise_for_status = lambda: None if resp.status_code < 400 else (_ for _ in ()).throw(requests.HTTPError(resp.text))
            return mock_resp

        with mock.patch("requests.Session.get", side_effect=fake_get):
            with mock.patch("requests.Session.post", side_effect=fake_post):
                with mock.patch("requests.Session.delete", side_effect=fake_delete):
                    me_info = admin.me()
                    self.assertEqual(me_info["id"], "admin-user")
                    self.assertTrue(me_info["is_admin"])

                    self.db.workers.register(
                        id="worker-ac",
                        hostname="node-ac",
                        native_arch="amd64",
                        arch_scores={"amd64": 1.0},
                        free_disk_gb=10.0,
                        token="tok",
                    )
                    roster = admin.workers_list()
                    self.assertEqual(len(roster), 1)
                    self.assertEqual(roster[0]["id"], "worker-ac")

                    ok = admin.worker_pause("worker-ac", paused=True)
                    self.assertTrue(ok)
                    self.assertEqual(self.db.workers.get("worker-ac")["status"], "paused")

                    ok = admin.worker_delete("worker-ac")
                    self.assertTrue(ok)
                    self.assertIsNone(self.db.workers.get("worker-ac"))
