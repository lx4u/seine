# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the seine-server authentication helpers."""

import os
import shutil
import tempfile

from avocado import Test
from fastapi import HTTPException
from fastapi.testclient import TestClient

from seine.distributed.server.api import create_app
from seine.distributed.server.auth import is_system_admin, parse_bearer, require_member
from seine.distributed.server.db import Database


class ParseBearerTest(Test):
    """Test the single Authorization header parser."""

    def test_parse_bearer(self):
        self.assertEqual(parse_bearer("Bearer abc"), "abc")
        self.assertEqual(parse_bearer("Bearer  abc "), "abc")
        self.assertIsNone(parse_bearer(None))
        self.assertIsNone(parse_bearer("Basic abc"))
        self.assertIsNone(parse_bearer("Bearer "))


class AuthDependenciesTest(Test):
    """Test current_user and require_member through the API."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="seine-test-auth-deps-")
        self.db = Database(os.path.join(self.tmp_dir, "test.db"))
        self.client = TestClient(create_app(db=self.db))
        self.db.projects.create("demo")
        self.db.users.create("dev")
        self.db.projects.add_member("demo", "dev", role="developer")

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_disabled_user_token_is_rejected(self):
        token = self.db.tokens.issue(user_id="dev")["token"]
        headers = {"Authorization": f"Bearer {token}"}
        self.assertEqual(self.client.get("/api/v1/projects", headers=headers).status_code, 200)

        self.db.users.set_active("dev", False)
        self.assertEqual(self.client.get("/api/v1/projects", headers=headers).status_code, 401)

    def test_token_without_users_row_is_rejected(self):
        token = self.db.tokens.issue(user_id="legacy")["token"]
        resp = self.client.get("/api/v1/projects", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(resp.status_code, 401)

    def test_worker_token_is_not_a_user_token(self):
        self.db.upsert_worker("w1", "h", "amd64", {}, 1.0, token="wtok")
        resp = self.client.get("/api/v1/projects", headers={"Authorization": "Bearer wtok"})
        self.assertEqual(resp.status_code, 401)

    def test_require_member_roles(self):
        dev = {"user_id": "dev", "kind": "pat"}
        self.assertIs(require_member(self.db, dev, "demo"), dev)
        self.assertIs(require_member(self.db, dev, "demo", ("developer",)), dev)
        with self.assertRaises(HTTPException) as ctx:
            require_member(self.db, dev, "demo", ("releaser", "admin"))
        self.assertEqual(ctx.exception.status_code, 403)
        with self.assertRaises(HTTPException):
            require_member(self.db, {"user_id": "eve", "kind": "pat"}, "demo")

    def test_system_admin_passes_membership(self):
        self.db.users.create("boss", is_admin=True)
        boss = {"user_id": "boss", "kind": "pat"}
        self.assertTrue(is_system_admin(self.db, boss))
        self.assertIs(require_member(self.db, boss, "demo", ("releaser",)), boss)

    def test_only_an_active_admin_user_is_system_admin(self):
        for name in ("admin", "root", "nobody"):
            self.assertFalse(is_system_admin(self.db, {"user_id": name, "kind": "admin"}))
        self.db.users.create("boss", is_admin=True)
        self.db.users.create("boss2", is_admin=True)
        self.db.users.update("boss", active=False)
        self.assertFalse(is_system_admin(self.db, {"user_id": "boss"}))
        self.assertFalse(is_system_admin(self.db, {"user_id": "dev", "kind": "system"}))
