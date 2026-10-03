# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for administrative CLI and RBAC management."""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
from unittest import mock

import uvicorn
from avocado import Test
from fastapi.testclient import TestClient

from seine.cli import AdminCmd
from seine.distributed.client.admin import AdminClient, run_client_admin
from seine.distributed.server.admin import (
    handle_admin_command,
    member_add,
    member_list,
    member_remove,
    project_create,
    project_delete,
    project_list,
    project_update,
    run_server_admin,
    token_issue,
    token_list,
    token_revoke,
    user_create,
    user_list,
    user_update,
)
from seine.distributed.server import housekeeping
from seine.distributed.server.api import create_app
from seine.distributed.server.db import Database
from seine.distributed.server.housekeeping import HousekeepingBusy, ProjectReport
from seine.distributed.server.settings import EnvRetention, Retention, Settings


class ServerAdminLocalCLITest(Test):
    """Test server host administration commands operating on local Database."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="seine-test-admin-cli-")
        self.db_path = os.path.join(self.tmp_dir, "test.db")
        self.db = Database(self.db_path)

    def tearDown(self):
        self.db.close()
        if os.path.exists(self.tmp_dir):
            shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_user_create_follows_the_new_user_project_setting(self):
        with mock.patch.dict(os.environ, {"SEINE_NEW_USER_PROJECT": "auto"}):
            out = io.StringIO()
            with mock.patch("sys.stdout", out):
                code = run_server_admin(["--db-path", self.db_path, "user", "create", "erin"])
        self.assertEqual(code, 0)
        self.assertIn("(default project: home-erin)", out.getvalue())
        self.assertEqual(self.db.users.get("erin")["default_project"], "home-erin")

    def test_project_create_refuses_the_home_prefix(self):
        with self.assertRaises(ValueError):
            project_create(self.db, "home-dave")

    def test_project_create_and_list(self):
        proj = project_create(self.db, "alpha")
        self.assertEqual(proj["name"], "alpha")
        self.assertEqual(proj["dev_bucket"], "seine-alpha-dev")
        self.assertEqual(proj["prod_bucket"], "seine-alpha-prod")

        projs = project_list(self.db)
        self.assertEqual(len(projs), 1)
        self.assertEqual(projs[0]["name"], "alpha")

    def test_project_create_custom_buckets(self):
        proj = project_create(
            self.db,
            "beta",
            dev_bucket="custom-dev",
            prod_bucket="custom-prod",
        )
        self.assertEqual(proj["dev_bucket"], "custom-dev")
        self.assertEqual(proj["prod_bucket"], "custom-prod")

    def test_project_create_with_bucket_provisioning(self):
        mock_provider = mock.MagicMock()
        proj = project_create(
            self.db,
            "gamma",
            provision_buckets=True,
            storage_provider=mock_provider,
        )
        self.assertEqual(proj["name"], "gamma")
        self.assertEqual(mock_provider.ensure_bucket.call_count, 2)

    def test_project_delete(self):
        project_create(self.db, "delta")
        self.assertTrue(project_delete(self.db, "delta"))
        self.assertFalse(project_delete(self.db, "delta"))
        self.assertEqual(len(project_list(self.db)), 0)

    def test_member_management(self):
        project_create(self.db, "epsilon")
        user_create(self.db, "alice")

        mem = member_add(self.db, "epsilon", "alice", "developer")
        self.assertEqual(mem["user_id"], "alice")
        self.assertEqual(mem["role"], "developer")

        mem_updated = member_add(self.db, "epsilon", "alice", "admin")
        self.assertEqual(mem_updated["role"], "admin")

        members = member_list(self.db, "epsilon")
        self.assertEqual(len(members), 1)
        self.assertEqual(members[0]["role"], "admin")

        self.assertTrue(member_remove(self.db, "epsilon", "alice"))
        self.assertFalse(member_remove(self.db, "epsilon", "alice"))
        self.assertEqual(len(member_list(self.db, "epsilon")), 0)

    def test_member_add_requires_existing_user(self):
        project_create(self.db, "epsilon")
        with self.assertRaises(ValueError):
            member_add(self.db, "epsilon", "ghost", "developer")

    def test_user_management(self):
        user = user_create(self.db, "alice", is_admin=True)
        self.assertTrue(user["is_admin"])
        user_create(self.db, "bob")
        with self.assertRaises(ValueError):
            user_create(self.db, "bob")

        self.assertEqual([u["id"] for u in user_list(self.db)], ["alice", "bob"])
        self.assertFalse(user_update(self.db, "bob", active=False)["active"])
        with self.assertRaises(ValueError):
            user_update(self.db, "ghost", active=False)
        # alice is the only active administrator.
        with self.assertRaises(ValueError):
            user_update(self.db, "alice", is_admin=False)
        with self.assertRaises(ValueError):
            user_update(self.db, "alice", active=False)

    def test_token_generation_and_revocation(self):
        user_create(self.db, "alice")
        tok = token_issue(self.db, "alice", kind="pat", days=7)
        self.assertIn("token", tok)
        self.assertEqual(tok["user_id"], "alice")
        self.assertEqual(tok["kind"], "pat")
        self.assertIsNotNone(tok["expires_at"])
        self.assertGreater(tok["expires_at"], time.time())

        with self.assertRaises(ValueError):
            token_issue(self.db, "alice", kind="worker")
        with self.assertRaises(ValueError):
            token_issue(self.db, "ghost")

        tokens = token_list(self.db)
        self.assertEqual(len(tokens), 1)
        self.assertNotIn("token", tokens[0])
        self.assertNotIn("token_hash", tokens[0])

        self.assertIsNotNone(self.db.tokens.validate(tok["token"]))
        self.assertTrue(token_revoke(self.db, tok["id"]))
        self.assertIsNone(self.db.tokens.validate(tok["token"]))
        self.assertFalse(token_revoke(self.db, tok["id"]))

    def test_bootstrap_first_admin_and_token(self):
        db = ["--db-path", self.db_path]
        self.assertEqual(run_server_admin(db + ["user", "create", "alice", "--is-admin"]), 0)
        self.assertEqual(run_server_admin(db + ["user", "create", "alice"]), 1)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(run_server_admin(db + ["token", "issue", "alice"]), 0)
        secret = out.getvalue().split("Secret (shown only once): ")[1].strip()
        record = self.db.tokens.validate(secret)
        self.assertEqual(record["user_id"], "alice")
        self.assertTrue(self.db.users.get("alice")["is_admin"])

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(run_server_admin(db + ["token", "list"]), 0)
        self.assertIn(record["id"], out.getvalue())
        self.assertNotIn(secret, out.getvalue())

        self.assertEqual(run_server_admin(db + ["token", "issue", "ghost"]), 1)

    def test_user_cli_update_flags(self):
        db = ["--db-path", self.db_path]
        run_server_admin(db + ["user", "create", "alice", "--is-admin"])
        run_server_admin(db + ["user", "create", "bob"])
        self.assertEqual(run_server_admin(db + ["user", "update", "bob", "--is-admin", "--no-active"]), 0)
        bob = self.db.users.get("bob")
        self.assertTrue(bob["is_admin"])
        self.assertFalse(bob["active"])
        self.assertEqual(run_server_admin(db + ["user", "update", "bob", "--active"]), 0)
        self.assertTrue(self.db.users.get("bob")["active"])
        self.assertEqual(run_server_admin(db + ["user", "list"]), 0)
        # Once bob is demoted, alice is the last active admin.
        self.assertEqual(run_server_admin(db + ["user", "update", "bob", "--no-is-admin"]), 0)
        self.assertEqual(run_server_admin(db + ["user", "update", "alice", "--no-is-admin"]), 1)

    def test_project_update_sets_and_clears_the_quota(self):
        project_create(self.db, "quota-p")
        self.assertEqual(project_update(self.db, "quota-p", quota_gb=50)["quota_gb"], 50)
        self.assertIsNone(project_update(self.db, "quota-p", quota_gb=None)["quota_gb"])
        self.assertIsNone(project_update(self.db, "no-such", quota_gb=5))

    def test_project_update_cli(self):
        db = ["--db-path", self.db_path]
        project_create(self.db, "quota-p")
        err = io.StringIO()
        out = io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            self.assertEqual(run_server_admin(db + ["project", "update", "quota-p", "--quota-gb", "2.5"]), 0)
            self.assertEqual(self.db.projects.get("quota-p")["quota_gb"], 2.5)
            run_server_admin(db + ["project", "list"])
            self.assertIn("quota=2.5GB", out.getvalue())
            self.assertEqual(run_server_admin(db + ["project", "update", "quota-p", "--no-quota"]), 0)
            self.assertIsNone(self.db.projects.get("quota-p")["quota_gb"])
            self.assertEqual(run_server_admin(db + ["project", "update", "quota-p", "--quota-gb", "0"]), 1)
            self.assertEqual(run_server_admin(db + ["project", "update", "no-such", "--quota-gb", "1"]), 1)
            self.assertEqual(run_server_admin(db + ["project", "update", "quota-p"]), 1)
        self.assertIn("not found", err.getvalue())

    def test_server_admin_cli_execution(self):
        res = run_server_admin(["--db-path", self.db_path, "user", "create", "bob"])
        self.assertEqual(res, 0)

        res = run_server_admin(["--db-path", self.db_path, "project", "create", "zeta"])
        self.assertEqual(res, 0)

        res = run_server_admin(["--db-path", self.db_path, "project", "list"])
        self.assertEqual(res, 0)

        res = run_server_admin(["--db-path", self.db_path, "member", "add", "zeta", "bob", "releaser"])
        self.assertEqual(res, 0)

        res = run_server_admin(["--db-path", self.db_path, "member", "list", "zeta"])
        self.assertEqual(res, 0)

        res = run_server_admin(["--db-path", self.db_path, "token", "issue", "bob", "--days", "5"])
        self.assertEqual(res, 0)

        res = run_server_admin(["--db-path", self.db_path, "token", "list"])
        self.assertEqual(res, 0)

        res = run_server_admin(["--db-path", self.db_path, "member", "remove", "zeta", "bob"])
        self.assertEqual(res, 0)

        res = run_server_admin(["--db-path", self.db_path, "project", "delete", "zeta"])
        self.assertEqual(res, 0)


class AdminRESTAPITest(Test):
    """Test REST API administration endpoints and RBAC gate enforcement."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="seine-test-admin-api-")
        self.db_path = os.path.join(self.tmp_dir, "test.db")
        self.db = Database(self.db_path)
        self.app = create_app(db=self.db, enrollment_token="test-enrollment-token")
        self.client = TestClient(self.app)

        self.db.projects.create("proj-a")
        self.db.projects.create("proj-b")

        self.db.users.create("sysadmin", is_admin=True)
        for name in ("alice", "bob", "charlie", "dave", "eva"):
            self.db.users.create(name)

        tok_admin = self.db.tokens.issue(user_id="sysadmin", kind="pat")
        self.admin_token = tok_admin["token"]

        tok_alice = self.db.tokens.issue(user_id="alice", kind="pat")
        self.alice_token = tok_alice["token"]
        self.db.projects.add_member("proj-a", "alice", "admin")

        tok_bob = self.db.tokens.issue(user_id="bob", kind="pat")
        self.dev_token = tok_bob["token"]
        self.db.projects.add_member("proj-a", "bob", "developer")

    def tearDown(self):
        self.db.close()
        if os.path.exists(self.tmp_dir):
            shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _projects_as(self, token):
        resp = self.client.get("/api/v1/projects", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(resp.status_code, 200)
        return resp.json()

    def test_project_listing_shows_a_member_only_their_projects(self):
        projects = self._projects_as(self.dev_token)
        self.assertEqual([p["name"] for p in projects], ["proj-a"])
        self.assertEqual(set(projects[0]), {"id", "name", "created_at"})

    def test_project_listing_is_empty_for_a_non_member(self):
        token = self.db.tokens.issue(user_id="charlie", kind="pat")["token"]
        self.assertEqual(self._projects_as(token), [])

    def test_project_listing_gives_a_system_administrator_everything(self):
        projects = {p["name"]: p for p in self._projects_as(self.admin_token)}
        self.assertEqual(set(projects), {"proj-a", "proj-b"})
        self.assertEqual(projects["proj-a"]["dev_bucket"], "seine-proj-a-dev")
        self.assertEqual(projects["proj-a"]["prod_bucket"], "seine-proj-a-prod")

    def _create_user(self, mode, body):
        from seine.distributed.server.settings import Settings
        app = create_app(db=self.db, settings=Settings(
            enrollment_token="enroll-token", new_user_project=mode))
        return TestClient(app).post(
            "/api/v1/users", json=body,
            headers={"Authorization": f"Bearer {self.admin_token}"})

    def test_home_prefix_cannot_be_claimed_by_hand(self):
        headers = {"Authorization": f"Bearer {self.admin_token}"}
        resp = self.client.post("/api/v1/projects", json={"name": "home-zed"}, headers=headers)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("reserved", resp.json()["detail"])
        self.assertIsNone(self.db.projects.get("home-zed"))
        resp = self.client.post("/api/v1/projects", json={"name": "homer"}, headers=headers)
        self.assertEqual(resp.status_code, 201)

    def test_new_user_gets_a_home_project_when_the_server_says_so(self):
        resp = self._create_user("auto", {"id": "newbie"})
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()["default_project"], "home-newbie")
        self.assertTrue(self.db.projects.get("home-newbie")["dev_only"])

    def test_new_administrator_gets_no_project(self):
        resp = self._create_user("auto", {"id": "boss", "is_admin": True})
        self.assertEqual(resp.status_code, 201)
        self.assertIsNone(resp.json()["default_project"])
        self.assertIsNone(self.db.projects.get("home-boss"))

    def test_new_user_joins_the_shared_project(self):
        resp = self._create_user("proj-b", {"id": "newbie"})
        self.assertEqual(resp.json()["default_project"], "proj-b")
        self.assertEqual(self.db.projects.get_member("proj-b", "newbie")["role"], "developer")

    def test_unusable_new_user_project_creates_no_user(self):
        resp = self._create_user("no-such-project", {"id": "newbie"})
        self.assertEqual(resp.status_code, 500)
        self.assertIn("no-such-project", resp.json()["detail"])
        self.assertIsNone(self.db.users.get("newbie"))

    def test_duplicate_user_gets_no_stray_home_project(self):
        resp = self._create_user("auto", {"id": "alice"})
        self.assertEqual(resp.status_code, 409)
        self.assertIsNone(self.db.projects.get("home-alice"))

    def test_projects_api_admin_allowed(self):
        headers = {"Authorization": f"Bearer {self.admin_token}"}
        resp = self.client.post("/api/v1/projects", json={"name": "proj-new"}, headers=headers)
        self.assertEqual(resp.status_code, 201)
        data = resp.json()
        self.assertEqual(data["name"], "proj-new")

        resp_list = self.client.get("/api/v1/projects", headers=headers)
        self.assertEqual(resp_list.status_code, 200)
        names = [p["name"] for p in resp_list.json()]
        self.assertIn("proj-new", names)

        resp_del = self.client.delete("/api/v1/projects/proj-new", headers=headers)
        self.assertEqual(resp_del.status_code, 200)

    def _patch_project(self, token, name, body):
        return self.client.patch(
            f"/api/v1/projects/{name}", json=body, headers={"Authorization": f"Bearer {token}"}
        )

    def test_project_update_sets_and_clears_the_quota(self):
        resp = self._patch_project(self.admin_token, "proj-a", {"quota_gb": 100})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["quota_gb"], 100)
        listed = {p["name"]: p for p in self._projects_as(self.admin_token)}
        self.assertEqual(listed["proj-a"]["quota_gb"], 100)
        self.assertIsNone(listed["proj-b"]["quota_gb"])

        resp = self._patch_project(self.admin_token, "proj-a", {"quota_gb": None})
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(self.db.projects.get("proj-a")["quota_gb"])

    def test_project_update_without_fields_changes_nothing(self):
        self.db.projects.set_quota("proj-a", 7)
        resp = self._patch_project(self.admin_token, "proj-a", {})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["quota_gb"], 7)

    def test_project_update_rejects_a_bad_quota(self):
        for bad in (0, -1, "lots"):
            resp = self._patch_project(self.admin_token, "proj-a", {"quota_gb": bad})
            self.assertEqual(resp.status_code, 422, bad)
        self.assertIsNone(self.db.projects.get("proj-a")["quota_gb"])

    def test_project_update_unknown_project_is_404(self):
        resp = self._patch_project(self.admin_token, "no-such", {"quota_gb": 5})
        self.assertEqual(resp.status_code, 404)

    def test_project_update_needs_a_system_administrator(self):
        for token in (self.dev_token, self.alice_token):
            resp = self._patch_project(token, "proj-a", {"quota_gb": 5})
            self.assertEqual(resp.status_code, 403)
        self.assertIsNone(self.db.projects.get("proj-a")["quota_gb"])

    def _gc(self, token, body=None):
        return self.client.post(
            "/api/v1/storage/gc", json=body or {}, headers={"Authorization": f"Bearer {token}"}
        )

    def test_storage_gc_needs_a_system_administrator(self):
        with mock.patch("seine.distributed.server.api.run_housekeeping") as run:
            for token in (self.dev_token, self.alice_token):
                self.assertEqual(self._gc(token).status_code, 403)
            run.assert_not_called()

    def test_storage_gc_without_retention_is_not_an_error(self):
        resp = self._gc(self.admin_token)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"retention_enabled": False, "reports": []})

    def test_storage_gc_serialises_the_reports(self):
        report = ProjectReport(
            project="proj-a", dry_run=True, evicted=[("b1", "ttl", 5)], failures=[("b2", "boom")],
            usage_before=9, usage_after=4, high_water_bytes=8, low_water_bytes=3,
        )
        app = create_app(db=self.db, settings=Settings(retention=Retention(interval=60, dev=None, prod=None)))
        with mock.patch("seine.distributed.server.api.run_housekeeping", return_value=[report]) as run:
            resp = TestClient(app).post(
                "/api/v1/storage/gc", json={"project": "proj-a", "dry_run": True},
                headers={"Authorization": f"Bearer {self.admin_token}"},
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(run.call_args.kwargs, {"project": "proj-a", "dry_run": True})
        self.assertEqual(resp.json(), {"retention_enabled": True, "reports": [{
            "project": "proj-a", "dry_run": True,
            "evicted": [{"build": "b1", "reason": "ttl", "bytes": 5}],
            "failures": [{"build": "b2", "error": "boom"}],
            "usage_before": 9, "usage_after": 4, "high_water_bytes": 8, "low_water_bytes": 3,
            "skipped_reason": None,
        }]})

    def test_storage_gc_defaults_to_a_real_run_on_every_project(self):
        with mock.patch("seine.distributed.server.api.run_housekeeping", return_value=[]) as run:
            self.assertEqual(self._gc(self.admin_token).status_code, 200)
        self.assertEqual(run.call_args.kwargs, {"project": None, "dry_run": False})

    def test_storage_gc_maps_busy_to_409_and_unknown_project_to_404(self):
        with mock.patch("seine.distributed.server.api.run_housekeeping", side_effect=HousekeepingBusy("housekeeping is already running")):
            resp = self._gc(self.admin_token)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["detail"], "housekeeping is already running")
        with mock.patch("seine.distributed.server.api.run_housekeeping", side_effect=ValueError("unknown project: x")):
            self.assertEqual(self._gc(self.admin_token, {"project": "x"}).status_code, 404)

    def test_storage_gc_end_to_end_dry_run_deletes_nothing(self):
        objects = {"artifacts/proj-a/old/a": 100}

        class Provider:
            def usage(self, prefix=""):
                return sum(objects.values())

            def delete_prefix(self, prefix):
                objects.clear()
                return 1, 100

        env = EnvRetention(worktrees=None, artifacts=3600, cache=None, high_water=None, low_water=None, min_age=0)
        app = create_app(db=self.db, settings=Settings(retention=Retention(interval=60, dev=env, prod=env)))
        self.db.builds.create("old", "proj-a", artifact_urls=["s3://b/old/a"])
        self.db.builds.update_status("old", "completed", finished_at=1.0, artifact_meta=[{"name": "a", "size": 100}])
        gc = TestClient(app)
        headers = {"Authorization": f"Bearer {self.admin_token}"}
        with mock.patch.object(housekeeping, "provider_for", lambda *a: Provider()):
            resp = gc.post("/api/v1/storage/gc", json={"project": "proj-a", "dry_run": True}, headers=headers)
            self.assertEqual(resp.json()["reports"][0]["evicted"], [{"build": "old", "reason": "ttl", "bytes": 100}])
            self.assertEqual(len(objects), 1)
            self.assertIsNone(self.db.builds.get("old")["artifacts_expired_reason"])
            gc.post("/api/v1/storage/gc", json={"project": "proj-a"}, headers=headers)
        self.assertEqual(objects, {})
        self.assertEqual(self.db.builds.get("old")["artifacts_expired_reason"], "ttl")

    def test_projects_api_with_provision_buckets(self):
        mock_provider = mock.MagicMock()
        self.app.state.storage_provider = mock_provider
        headers = {"Authorization": f"Bearer {self.admin_token}"}

        resp = self.client.post(
            "/api/v1/projects",
            json={"name": "proj-prov", "provision_buckets": True},
            headers=headers,
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(mock_provider.ensure_bucket.call_count, 2)

    def test_projects_api_rbac_developer_rejected(self):
        headers = {"Authorization": f"Bearer {self.dev_token}"}

        resp = self.client.post("/api/v1/projects", json={"name": "proj-dev-fail"}, headers=headers)
        self.assertEqual(resp.status_code, 403)

        resp_del = self.client.delete("/api/v1/projects/proj-a", headers=headers)
        self.assertEqual(resp_del.status_code, 403)

    def test_project_admin_scope_enforcement(self):
        headers_alice = {"Authorization": f"Bearer {self.alice_token}"}

        resp_a = self.client.delete("/api/v1/projects/proj-a", headers=headers_alice)
        self.assertEqual(resp_a.status_code, 200)

        resp_b = self.client.delete("/api/v1/projects/proj-b", headers=headers_alice)
        self.assertEqual(resp_b.status_code, 403)

    def test_member_api_admin_allowed(self):
        headers = {"Authorization": f"Bearer {self.admin_token}"}

        resp = self.client.post(
            "/api/v1/projects/proj-b/members",
            json={"user_id": "charlie", "role": "releaser"},
            headers=headers,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["role"], "releaser")

        resp_list = self.client.get("/api/v1/projects/proj-b/members", headers=headers)
        self.assertEqual(resp_list.status_code, 200)
        self.assertEqual(len(resp_list.json()), 1)

        resp_del = self.client.delete("/api/v1/projects/proj-b/members/charlie", headers=headers)
        self.assertEqual(resp_del.status_code, 200)

    def test_member_api_rbac_developer_rejected(self):
        headers = {"Authorization": f"Bearer {self.dev_token}"}

        resp_add = self.client.post(
            "/api/v1/projects/proj-a/members",
            json={"user_id": "dave", "role": "developer"},
            headers=headers,
        )
        self.assertEqual(resp_add.status_code, 403)

        resp_list = self.client.get("/api/v1/projects/proj-a/members", headers=headers)
        self.assertEqual(resp_list.status_code, 403)

        resp_rem = self.client.delete("/api/v1/projects/proj-a/members/alice", headers=headers)
        self.assertEqual(resp_rem.status_code, 403)

    def test_member_api_invalid_role(self):
        headers = {"Authorization": f"Bearer {self.admin_token}"}
        resp = self.client.post(
            "/api/v1/projects/proj-b/members",
            json={"user_id": "dave", "role": "superman"},
            headers=headers,
        )
        self.assertEqual(resp.status_code, 400)

    def test_token_api_admin_allowed(self):
        headers = {"Authorization": f"Bearer {self.admin_token}"}

        resp = self.client.post(
            "/api/v1/tokens",
            json={"user_id": "eva", "kind": "pat", "days": 30},
            headers=headers,
        )
        self.assertEqual(resp.status_code, 200)
        tok_id = resp.json()["id"]

        resp_list = self.client.get("/api/v1/tokens", headers=headers)
        self.assertEqual(resp_list.status_code, 200)
        tokens = resp_list.json()
        self.assertTrue(any(t["id"] == tok_id for t in tokens))

        resp_del = self.client.delete(f"/api/v1/tokens/{tok_id}", headers=headers)
        self.assertEqual(resp_del.status_code, 200)

    def test_token_api_rbac_developer_rejected(self):
        headers = {"Authorization": f"Bearer {self.dev_token}"}

        resp_issue = self.client.post(
            "/api/v1/tokens",
            json={"user_id": "eva", "kind": "pat"},
            headers=headers,
        )
        self.assertEqual(resp_issue.status_code, 403)

        resp_list = self.client.get("/api/v1/tokens", headers=headers)
        self.assertEqual(resp_list.status_code, 403)

        resp_del = self.client.delete("/api/v1/tokens/dummy-token", headers=headers)
        self.assertEqual(resp_del.status_code, 403)

    def test_unauthenticated_requests_return_401(self):
        resp = self.client.get("/api/v1/projects")
        self.assertEqual(resp.status_code, 401)

        resp = self.client.post("/api/v1/projects", json={"name": "unauth"})
        self.assertEqual(resp.status_code, 401)


class RemoteClientAdminTest(Test):
    """Test remote client administration commands against live REST server."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="seine-test-client-admin-")
        self.db_path = os.path.join(self.tmp_dir, "test.db")
        self.db = Database(self.db_path)
        self.app = create_app(db=self.db, enrollment_token="enroll-token")

        self.db.users.create("sysadmin", is_admin=True)
        tok = self.db.tokens.issue(user_id="sysadmin", kind="pat")
        self.admin_tok = tok["token"]

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        self.port = sock.getsockname()[1]
        sock.close()

        self.server_url = f"http://127.0.0.1:{self.port}"
        if not hasattr(sys.stdout, "isatty"):
            sys.stdout.isatty = lambda: False
        config = uvicorn.Config(
            self.app,
            host="127.0.0.1",
            port=self.port,
            log_config=None,
            log_level="warning",
        )
        self.server = uvicorn.Server(config)
        self.server_thread = threading.Thread(target=self.server.run, daemon=True)
        self.server_thread.start()

        timeout = time.time() + 5.0
        while not self.server.started and time.time() < timeout:
            time.sleep(0.05)

    def tearDown(self):
        if hasattr(self, "server"):
            self.server.should_exit = True
        if hasattr(self, "server_thread"):
            self.server_thread.join(timeout=2.0)
        self.db.close()
        if os.path.exists(self.tmp_dir):
            shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_admin_client_methods(self):
        client = AdminClient(server_url=self.server_url, token=self.admin_tok)

        proj = client.project_create("p-remote")
        self.assertEqual(proj["name"], "p-remote")

        projs = client.project_list()
        self.assertTrue(any(p["name"] == "p-remote" for p in projs))

        client.user_create("frank")
        mem = client.member_add("p-remote", "frank", "developer")
        self.assertEqual(mem["user_id"], "frank")

        members = client.member_list("p-remote")
        self.assertEqual(len(members), 1)

        client.member_remove("p-remote", "frank")
        self.assertEqual(len(client.member_list("p-remote")), 0)

        tok = client.token_issue("frank", kind="pat", days=10)
        self.assertIn("token", tok)

        tokens = client.token_list()
        self.assertTrue(any(t["id"] == tok["id"] for t in tokens))

        client.token_revoke(tok["id"])
        tokens_after = client.token_list()
        self.assertFalse(any(t["id"] == tok["id"] for t in tokens_after))

        del_res = client.project_delete("p-remote")
        self.assertTrue(del_res["deleted"])

    def test_client_project_update(self):
        client = AdminClient(server_url=self.server_url, token=self.admin_tok)
        client.project_create("p-quota")
        self.assertEqual(client.project_update("p-quota", quota_gb=20)["quota_gb"], 20)
        self.assertEqual(self.db.projects.get("p-quota")["quota_gb"], 20)
        self.assertIsNone(client.project_update("p-quota", quota_gb=None)["quota_gb"])

    def test_client_project_update_cli_request_shape(self):
        argv = ["--server", self.server_url, "--token", self.admin_tok, "project", "update", "p-q"]
        self.db.projects.create("p-q")
        out = io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("requests.Session.patch") as patch:
            patch.return_value.json.return_value = {"name": "p-q", "quota_gb": 3.5}
            self.assertEqual(run_client_admin(argv + ["--quota-gb", "3.5"]), 0)
            self.assertEqual(patch.call_args.args[0], f"{self.server_url}/api/v1/projects/p-q")
            self.assertEqual(patch.call_args.kwargs["json"], {"quota_gb": 3.5})
            self.assertIn("quota: 3.5GB", out.getvalue())

            patch.return_value.json.return_value = {"name": "p-q", "quota_gb": None}
            self.assertEqual(run_client_admin(argv + ["--no-quota"]), 0)
            self.assertEqual(patch.call_args.kwargs["json"], {"quota_gb": None})

            patch.reset_mock()
            with mock.patch("sys.stderr", io.StringIO()):
                self.assertEqual(run_client_admin(argv), 1)
            patch.assert_not_called()

    def _gc_cli(self, result, *args):
        argv = ["--server", self.server_url, "--token", self.admin_tok, "storage", "gc", *args]
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err), mock.patch("requests.Session.post") as post:
            post.return_value.json.return_value = result
            code = run_client_admin(argv)
        return code, out.getvalue(), err.getvalue(), post

    def _gc_report(self, **kw):
        report = {
            "project": "test", "dry_run": False, "evicted": [], "failures": [], "usage_before": None,
            "usage_after": None, "high_water_bytes": None, "low_water_bytes": None, "skipped_reason": None,
        }
        report.update(kw)
        return {"retention_enabled": True, "reports": [report]}

    def test_storage_gc_cli_prints_a_real_run(self):
        gb = 1024**3
        result = self._gc_report(
            usage_before=4 * gb, usage_after=2 * gb,
            evicted=[{"build": "b1", "reason": "ttl", "bytes": gb}, {"build": "b2", "reason": "pressure", "bytes": gb}],
        )
        code, out, _, post = self._gc_cli(result, "--project", "test")
        self.assertEqual(code, 0)
        self.assertEqual(post.call_args.args[0], f"{self.server_url}/api/v1/storage/gc")
        self.assertEqual(post.call_args.kwargs["json"], {"project": "test", "dry_run": False})
        self.assertEqual(out.splitlines(), [
            "test: usage 4.0 GB -> 2.0 GB, evicted 2 builds (2.0 GB)",
            "  build b1  ttl  1.0 GB",
            "  build b2  pressure  1.0 GB",
        ])

    def test_storage_gc_cli_says_one_build_not_one_builds(self):
        evicted = [{"build": "b1", "reason": "ttl", "bytes": 1}]
        _, out, _, _ = self._gc_cli(self._gc_report(evicted=evicted))
        self.assertIn("test: evicted 1 build (1 B)", out.splitlines())
        _, out, _, _ = self._gc_cli(self._gc_report())
        self.assertIn("test: evicted 0 builds (0 B)", out.splitlines())

    def test_storage_gc_cli_dry_run_says_would(self):
        result = self._gc_report(dry_run=True, evicted=[{"build": "b1", "reason": "ttl", "bytes": 10}])
        code, out, _, post = self._gc_cli(result, "--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(post.call_args.kwargs["json"], {"project": None, "dry_run": True})
        self.assertIn("test: would evict 1 build (10 B)", out)

    def test_storage_gc_cli_reports_failures_and_skips(self):
        result = self._gc_report(failures=[{"build": "b9", "error": "boom"}])
        code, out, _, _ = self._gc_cli(result)
        self.assertEqual(code, 1)
        self.assertIn("  failed b9: boom", out)
        code, out, _, _ = self._gc_cli(self._gc_report(skipped_reason="no quota set"))
        self.assertEqual(code, 0)
        self.assertIn("test: skipped, no quota set", out)

    def test_storage_gc_cli_without_retention(self):
        code, out, _, _ = self._gc_cli({"retention_enabled": False, "reports": []})
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "retention is not configured on this server")

    def test_storage_gc_cli_shows_server_errors_on_one_line(self):
        for token, detail in (("bad", "Admin privileges required"), (self.admin_tok, "housekeeping is already running")):
            argv = ["--server", self.server_url, "--token", token, "storage", "gc"]
            err = io.StringIO()
            with mock.patch("sys.stderr", err), mock.patch("sys.stdout", io.StringIO()):
                with mock.patch.object(AdminClient, "storage_gc", side_effect=self._http_error(detail)):
                    self.assertEqual(run_client_admin(argv), 1)
            self.assertEqual(err.getvalue(), f"error: {detail}\n")

    def _http_error(self, detail):
        import requests
        resp = requests.Response()
        resp.status_code = 409
        resp.json = lambda: {"detail": detail}
        return requests.HTTPError("409", response=resp)

    def test_client_storage_gc_against_the_server(self):
        client = AdminClient(server_url=self.server_url, token=self.admin_tok)
        self.assertEqual(client.storage_gc(dry_run=True), {"retention_enabled": False, "reports": []})

    def test_client_project_list_shows_the_quota(self):
        self.db.projects.create("p-listed")
        self.db.projects.set_quota("p-listed", 12)
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            code = run_client_admin(["--server", self.server_url, "--token", self.admin_tok, "project", "list"])
        self.assertEqual(code, 0)
        self.assertIn("quota=12GB", out.getvalue())

    def test_client_project_list_works_for_a_member_without_bucket_names(self):
        self.db.projects.create("p-member")
        self.db.users.create("mia")
        self.db.projects.add_member("p-member", "mia", "developer")
        token = self.db.tokens.issue(user_id="mia", kind="pat")["token"]
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            code = run_client_admin([
                "--server", self.server_url, "--token", token, "project", "list"])
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue().strip(), "p-member")

    def test_run_client_admin_cli(self):
        code = run_client_admin([
            "--server", self.server_url,
            "--token", self.admin_tok,
            "project", "create", "p-cli",
        ])
        self.assertEqual(code, 0)

        code = run_client_admin([
            "--server", self.server_url,
            "--token", self.admin_tok,
            "project", "list",
        ])
        self.assertEqual(code, 0)

        code = run_client_admin([
            "--server", self.server_url,
            "--token", self.admin_tok,
            "user", "create", "george",
        ])
        self.assertEqual(code, 0)

        code = run_client_admin([
            "--server", self.server_url,
            "--token", self.admin_tok,
            "user", "update", "george", "--no-active",
        ])
        self.assertEqual(code, 0)

        code = run_client_admin([
            "--server", self.server_url,
            "--token", self.admin_tok,
            "user", "list",
        ])
        self.assertEqual(code, 0)

        code = run_client_admin([
            "--server", self.server_url,
            "--token", self.admin_tok,
            "member", "add", "p-cli", "george", "releaser",
        ])
        self.assertEqual(code, 0)

        code = run_client_admin([
            "--server", self.server_url,
            "--token", self.admin_tok,
            "member", "list", "p-cli",
        ])
        self.assertEqual(code, 0)

        code = run_client_admin([
            "--server", self.server_url,
            "--token", self.admin_tok,
            "token", "issue", "george", "--days", "15",
        ])
        self.assertEqual(code, 0)

        code = run_client_admin([
            "--server", self.server_url,
            "--token", self.admin_tok,
            "token", "list",
        ])
        self.assertEqual(code, 0)

        code = run_client_admin([
            "--server", self.server_url,
            "--token", self.admin_tok,
            "member", "remove", "p-cli", "george",
        ])
        self.assertEqual(code, 0)

        code = run_client_admin([
            "--server", self.server_url,
            "--token", self.admin_tok,
            "project", "delete", "p-cli",
        ])
        self.assertEqual(code, 0)

    def test_admin_client_user_methods(self):
        client = AdminClient(server_url=self.server_url, token=self.admin_tok)
        self.assertTrue(client.user_create("heidi", is_admin=True)["is_admin"])
        self.assertEqual([u["id"] for u in client.user_list()], ["heidi", "sysadmin"])
        self.assertFalse(client.user_update("heidi", active=False)["active"])

    def test_seine_cli_admin_cmd_help(self):
        cmd = AdminCmd()
        cmd.main(["--help"])


class AdminAuthzTest(Test):
    """A project admin must not gain system-wide powers."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="seine-test-admin-authz-")
        self.db = Database(os.path.join(self.tmp_dir, "test.db"))
        self.client = TestClient(create_app(db=self.db, enrollment_token="tok"))

        self.db.users.create("sysadmin", is_admin=True)
        for name in ("alice", "bob", "admin", "root"):
            self.db.users.create(name)
        self.db.projects.create("proj-a")
        self.db.projects.create("proj-b")
        self.db.projects.add_member("proj-a", "alice", "admin")
        self.db.projects.add_member("proj-a", "bob", "developer")
        self.db.projects.add_member("proj-b", "bob", "developer")
        for bid, proj, user in (("bld-a", "proj-a", "alice"), ("bld-b", "proj-b", "bob")):
            self.db.create_build(
                build_id=bid, project=proj, target_arch="amd64", worktree_digest="d",
                spec_file="spec.yaml", is_release=False, options={}, user_id=user,
            )
        self.sys_h = self.headers("sysadmin")
        self.alice_h = self.headers("alice")

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def headers(self, user):
        return {"Authorization": f"Bearer {self.db.tokens.issue(user_id=user)['token']}"}

    def test_project_admin_cannot_create_projects(self):
        resp = self.client.post("/api/v1/projects", json={"name": "proj-x"}, headers=self.alice_h)
        self.assertEqual(resp.status_code, 403)

    def test_project_admin_cannot_manage_tokens(self):
        issue = self.client.post("/api/v1/tokens", json={"user_id": "bob"}, headers=self.alice_h)
        self.assertEqual(issue.status_code, 403)
        self.assertEqual(self.client.get("/api/v1/tokens", headers=self.alice_h).status_code, 403)
        tok_id = self.db.tokens.issue(user_id="bob")["id"]
        self.assertEqual(self.client.delete(f"/api/v1/tokens/{tok_id}", headers=self.alice_h).status_code, 403)
        self.assertIsNotNone(self.db.tokens.get(tok_id))

    def test_project_admin_cannot_manage_users(self):
        self.assertEqual(self.client.get("/api/v1/users", headers=self.alice_h).status_code, 403)
        resp = self.client.post("/api/v1/users", json={"id": "eve", "is_admin": True}, headers=self.alice_h)
        self.assertEqual(resp.status_code, 403)
        resp = self.client.patch("/api/v1/users/alice", json={"is_admin": True}, headers=self.alice_h)
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(self.db.users.get("alice")["is_admin"])
        self.assertIsNone(self.db.users.get("eve"))

    def test_project_admin_sees_only_its_builds(self):
        resp = self.client.get("/api/v1/builds", headers=self.alice_h)
        self.assertEqual({b["id"] for b in resp.json()}, {"bld-a"})
        resp = self.client.get("/api/v1/builds", params={"project": "proj-b"}, headers=self.alice_h)
        self.assertEqual(resp.json(), [])
        self.assertEqual(self.client.get("/api/v1/builds/bld-b", headers=self.alice_h).status_code, 403)
        sys_ids = {b["id"] for b in self.client.get("/api/v1/builds", headers=self.sys_h).json()}
        self.assertEqual(sys_ids, {"bld-a", "bld-b"})

    def test_project_admin_cannot_touch_other_projects(self):
        headers = self.alice_h
        self.assertEqual(self.client.get("/api/v1/projects/proj-b/members", headers=headers).status_code, 403)
        resp = self.client.post(
            "/api/v1/projects/proj-b/members", json={"user_id": "alice", "role": "admin"}, headers=headers
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(self.client.delete("/api/v1/projects/proj-b", headers=headers).status_code, 403)

    def test_magic_user_names_have_no_powers(self):
        for name in ("admin", "root"):
            headers = self.headers(name)
            resp = self.client.post("/api/v1/projects", json={"name": f"proj-{name}"}, headers=headers)
            self.assertEqual(resp.status_code, 403)
            self.assertEqual(self.client.get("/api/v1/tokens", headers=headers).status_code, 403)
            self.assertEqual(self.client.get("/api/v1/users", headers=headers).status_code, 403)
            self.assertEqual(self.client.get("/api/v1/builds/bld-a", headers=headers).status_code, 403)
            self.assertEqual(self.client.get("/api/v1/projects/proj-a/members", headers=headers).status_code, 403)

    def test_token_user_must_exist(self):
        token = self.db.tokens.issue(user_id="ghost")["token"]
        resp = self.client.get("/api/v1/builds", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(resp.status_code, 401)

    def test_inactive_user_token_is_rejected(self):
        self.db.users.update("alice", active=False)
        self.assertEqual(self.client.get("/api/v1/builds", headers=self.alice_h).status_code, 401)

    def test_inactive_system_admin_is_rejected(self):
        self.db.users.create("sysadmin2", is_admin=True)
        headers = self.headers("sysadmin2")
        self.db.users.update("sysadmin2", active=False)
        self.assertEqual(self.client.get("/api/v1/users", headers=headers).status_code, 401)

    def test_issue_token_for_unknown_user_is_404(self):
        resp = self.client.post("/api/v1/tokens", json={"user_id": "ghost"}, headers=self.sys_h)
        self.assertEqual(resp.status_code, 404)
        self.assertIn("ghost", resp.json()["detail"])

    def test_issue_token_returns_secret_once(self):
        resp = self.client.post("/api/v1/tokens", json={"user_id": "alice", "days": 1}, headers=self.sys_h)
        self.assertEqual(resp.status_code, 200)
        issued = resp.json()
        self.assertEqual(self.db.tokens.validate(issued["token"])["id"], issued["id"])

        listed = self.client.get("/api/v1/tokens", headers=self.sys_h).json()
        self.assertTrue(listed)
        for row in listed:
            self.assertEqual(set(row), {"id", "user_id", "kind", "created_at", "expires_at"})
        self.assertNotIn(issued["token"], str(listed))

    def test_issue_token_bad_kind_is_422(self):
        for kind in ("admin", "worker", "system", "bogus"):
            resp = self.client.post("/api/v1/tokens", json={"user_id": "alice", "kind": kind}, headers=self.sys_h)
            self.assertEqual(resp.status_code, 422, kind)

    def test_revoke_token_by_id(self):
        issued = self.db.tokens.issue(user_id="bob")
        resp = self.client.delete(f"/api/v1/tokens/{issued['token']}", headers=self.sys_h)
        self.assertEqual(resp.status_code, 404)
        self.assertIsNotNone(self.db.tokens.validate(issued["token"]))

        resp = self.client.delete(f"/api/v1/tokens/{issued['id']}", headers=self.sys_h)
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(self.db.tokens.validate(issued["token"]))
        self.assertEqual(self.client.delete("/api/v1/tokens/tok_none", headers=self.sys_h).status_code, 404)

    def test_add_member_requires_existing_user(self):
        resp = self.client.post(
            "/api/v1/projects/proj-a/members", json={"user_id": "ghost", "role": "developer"}, headers=self.alice_h
        )
        self.assertEqual(resp.status_code, 404)
        self.assertIsNone(self.db.projects.get_member("proj-a", "ghost"))

    def test_user_management_lifecycle(self):
        resp = self.client.post("/api/v1/users", json={"id": "carol", "is_admin": True}, headers=self.sys_h)
        self.assertEqual(resp.status_code, 201)
        self.assertTrue(resp.json()["is_admin"])
        again = self.client.post("/api/v1/users", json={"id": "carol"}, headers=self.sys_h)
        self.assertEqual(again.status_code, 409)
        bad = self.client.post("/api/v1/users", json={"id": "bad id!"}, headers=self.sys_h)
        self.assertEqual(bad.status_code, 422)

        ids = {u["id"] for u in self.client.get("/api/v1/users", headers=self.sys_h).json()}
        self.assertIn("carol", ids)

        resp = self.client.patch("/api/v1/users/carol", json={"active": False}, headers=self.sys_h)
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json()["active"])
        self.assertTrue(resp.json()["is_admin"])
        missing = self.client.patch("/api/v1/users/ghost", json={"active": False}, headers=self.sys_h)
        self.assertEqual(missing.status_code, 404)

    def test_last_active_admin_is_protected(self):
        for body in ({"is_admin": False}, {"active": False}):
            resp = self.client.patch("/api/v1/users/sysadmin", json=body, headers=self.sys_h)
            self.assertEqual(resp.status_code, 409, body)
        self.assertTrue(self.db.users.get("sysadmin")["is_admin"])

        # With a second admin the first one may step down.
        self.db.users.create("sysadmin2", is_admin=True)
        resp = self.client.patch("/api/v1/users/sysadmin", json={"is_admin": False}, headers=self.sys_h)
        self.assertEqual(resp.status_code, 200)
        resp = self.client.patch("/api/v1/users/sysadmin2", json={"active": False}, headers=self.headers("sysadmin2"))
        self.assertEqual(resp.status_code, 409)


class AdminClientTransportTest(Test):
    """Test the server URL check and the TLS options of the admin client."""

    def test_plain_http_to_a_remote_host_is_refused(self):
        with self.assertRaises(ValueError):
            AdminClient(server_url="http://203.0.113.5:8000", token="t")

    def test_plain_http_to_a_remote_host_is_accepted_with_insecure(self):
        AdminClient(server_url="http://203.0.113.5:8000", token="t", insecure=True)

    def test_plain_http_to_loopback_is_accepted(self):
        AdminClient(server_url="http://127.0.0.1:8000", token="t")

    def test_ca_cert_reaches_the_session(self):
        client = AdminClient(server_url="https://seine.example", token="t", ca_cert="/etc/ca.pem")
        self.assertEqual(client.session.verify, "/etc/ca.pem")
        self.assertIs(AdminClient(server_url="https://seine.example", token="t").session.verify, True)

    def test_cli_ca_cert_option_and_environment(self):
        for argv, env, expected in (
            (["--ca-cert", "/a.pem"], {}, "/a.pem"),
            ([], {"SEINE_CA_CERT": "/b.pem"}, "/b.pem"),
        ):
            with mock.patch.dict(os.environ, env), mock.patch("requests.Session.get") as get:
                get.return_value.json.return_value = []
                with mock.patch("seine.distributed.client.admin.AdminClient", wraps=AdminClient) as cls:
                    argv = ["--server", "https://seine.example", "--token", "t", *argv, "project", "list"]
                    self.assertEqual(run_client_admin(argv), 0)
                    self.assertEqual(cls.call_args.kwargs["ca_cert"], expected)

    def test_cli_refusal_is_one_error_line(self):
        err = io.StringIO()
        with mock.patch("sys.stderr", err), mock.patch("requests.Session.get") as get:
            code = run_client_admin(["--server", "http://203.0.113.5:8000", "--token", "t", "project", "list"])
        self.assertEqual(code, 1)
        self.assertEqual(len(err.getvalue().splitlines()), 1)
        self.assertTrue(err.getvalue().startswith("error: "))
        get.assert_not_called()

    def test_cli_insecure_allows_plain_http(self):
        with mock.patch("requests.Session.get") as get:
            get.return_value.json.return_value = []
            argv = ["--server", "http://203.0.113.5:8000", "--token", "t", "--insecure", "project", "list"]
            self.assertEqual(run_client_admin(argv), 0)
