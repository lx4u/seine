# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the distributed database schema and repository layer."""

import os
import shutil
import sqlite3
import tempfile
import time

from avocado import Test

from seine.distributed.server.db import (
    BuildRepo,
    Database,
    ProjectRepo,
    TokenRepo,
    WorkerRepo,
    connect_db,
    hash_secret,
    init_db,
    validate_project_name,
)


class DatabaseSchemaTest(Test):
    """Test database schema initialization and PRAGMA settings."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="seine-test-db-")
        self.db_path = os.path.join(self.tmp_dir, "schema.db")

    def tearDown(self):
        if os.path.exists(self.tmp_dir):
            shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_schema_initialization_and_pragmas(self):
        conn = connect_db(self.db_path)
        init_db(conn)

        journal_mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
        self.assertEqual(journal_mode.lower(), "wal")

        fk_enabled = conn.execute("PRAGMA foreign_keys;").fetchone()[0]
        self.assertEqual(fk_enabled, 1)

        cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = {row[0] for row in cur.fetchall()}
        expected = {"projects", "project_members", "builds", "workers", "jobs", "tokens"}
        self.assertTrue(expected.issubset(tables))
        conn.close()


class DatabaseRepositoryTest(Test):
    """Test repository operations for projects, members, builds, jobs, workers, and tokens."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="seine-test-repo-")
        self.db_path = os.path.join(self.tmp_dir, "test.db")
        self.db = Database(self.db_path)

    def tearDown(self):
        self.db.close()
        if os.path.exists(self.tmp_dir):
            shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_project_repo_crud(self):
        repo = self.db.projects

        proj = repo.create("alpha", prod_bucket="s3-alpha-prod", dev_bucket="s3-alpha-dev")
        self.assertEqual(proj["name"], "alpha")
        self.assertEqual(proj["id"], "alpha")
        self.assertEqual(proj["prod_bucket"], "s3-alpha-prod")
        self.assertEqual(proj["dev_bucket"], "s3-alpha-dev")

        with self.assertRaises(sqlite3.IntegrityError):
            repo.create("alpha")

        ensured = repo.ensure("alpha", dev_bucket="s3-alpha-dev2", prod_bucket="s3-alpha-prod2")
        self.assertEqual(ensured["dev_bucket"], "s3-alpha-dev2")
        self.assertEqual(ensured["prod_bucket"], "s3-alpha-prod2")

        fetched = repo.get("alpha")
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched["name"], "alpha")

        repo.ensure("beta")
        projs = repo.list()
        self.assertEqual(len(projs), 2)
        self.assertEqual([p["name"] for p in projs], ["alpha", "beta"])

        self.assertTrue(repo.delete("alpha"))
        self.assertIsNone(repo.get("alpha"))
        self.assertFalse(repo.delete("nonexistent"))

    def test_project_members_management(self):
        repo = self.db.projects
        repo.create("myproj")

        member = repo.add_member("myproj", "alice", "developer")
        self.assertEqual(member["user_id"], "alice")
        self.assertEqual(member["role"], "developer")

        updated = repo.add_member("myproj", "alice", "admin")
        self.assertEqual(updated["role"], "admin")

        with self.assertRaises(ValueError):
            repo.add_member("myproj", "bob", "superuser")

        repo.add_member("myproj", "bob", "releaser")

        m = repo.get_member("myproj", "alice")
        self.assertIsNotNone(m)
        self.assertEqual(m["role"], "admin")

        members = repo.list_members("myproj")
        self.assertEqual(len(members), 2)
        self.assertEqual(members[0]["user_id"], "alice")
        self.assertEqual(members[1]["user_id"], "bob")

        self.assertTrue(repo.remove_member("myproj", "alice"))
        self.assertIsNone(repo.get_member("myproj", "alice"))
        self.assertFalse(repo.remove_member("myproj", "alice"))

    def test_build_repo_crud_and_queries(self):
        self.db.projects.create("sample-proj")
        repo = self.db.builds

        build = repo.create(
            id="bld-1",
            project="sample-proj",
            target_arch="arm64",
            is_release=True,
            worktree_digest="tree123",
            options={"s3_cache": True, "timeout": 600},
            artifact_urls=["https://s3/artifacts/bld-1/disk.raw"],
        )
        self.assertEqual(build["id"], "bld-1")
        self.assertEqual(build["target_arch"], "arm64")
        self.assertTrue(build["is_release"])
        self.assertEqual(build["options"], {"s3_cache": True, "timeout": 600})
        self.assertEqual(build["artifact_urls"], ["https://s3/artifacts/bld-1/disk.raw"])
        self.assertEqual(build["status"], "queued")

        fetched = repo.get("bld-1")
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched["options"]["timeout"], 600)

        self.assertEqual(fetched["artifact_meta"], [])
        meta = [{"name": "disk.raw", "key": "artifacts/sample-proj/bld-1/disk.raw",
                 "sha256": "ef" * 32, "size": 42}]
        repo.update_status("bld-1", "completed", artifact_meta=meta)
        self.assertEqual(repo.get("bld-1")["artifact_meta"], meta)
        self.assertEqual(repo.list(project="sample-proj")[0]["artifact_meta"], meta)
        repo.update_status("bld-1", "completed")
        self.assertEqual(repo.get("bld-1")["artifact_meta"], meta)

        repo.create(
            id="bld-2",
            project="sample-proj",
            target_arch="amd64",
            status="running",
        )

        self.assertEqual(len(repo.list(project="sample-proj")), 2)
        running = repo.list(status="running")
        self.assertEqual(len(running), 1)
        self.assertEqual(running[0]["id"], "bld-2")
        self.assertEqual(len(repo.list(limit=1)), 1)

        now = time.time()
        updated = repo.update_status(
            "bld-1",
            status="completed",
            started_at=now - 10,
            finished_at=now,
            artifact_urls=["https://s3/artifacts/bld-1/disk.raw", "https://s3/artifacts/bld-1/sbom.json"],
        )
        self.assertEqual(updated["status"], "completed")
        self.assertAlmostEqual(updated["started_at"], now - 10, delta=1.0)
        self.assertEqual(len(updated["artifact_urls"]), 2)

    def test_jobs_management_and_status_sync(self):
        self.db.projects.create("demo-proj")
        self.db.builds.create(id="bld-job-test", project="demo-proj", target_arch="arm64")
        repo = self.db.builds

        job1 = repo.create_job(
            id="j-pkg-1",
            build_id="bld-job-test",
            kind="package",
            target_arch="arm64",
            package_name="busybox",
        )
        job2 = repo.create_job(
            id="j-img-1",
            build_id="bld-job-test",
            kind="image",
            target_arch="arm64",
        )
        self.assertEqual(job1["kind"], "package")
        self.assertEqual(job1["package_name"], "busybox")
        self.assertEqual(job2["kind"], "image")

        self.assertEqual(repo.get_job("j-pkg-1")["id"], "j-pkg-1")
        self.assertEqual(len(repo.list_jobs(build_id="bld-job-test")), 2)
        self.assertEqual(len(repo.list_jobs(status="queued")), 2)

        repo.update_job_status("j-pkg-1", status="failed")
        self.assertEqual(repo.get("bld-job-test")["status"], "failed")

        repo.update_status("bld-job-test", status="running")
        repo.update_job_status("j-pkg-1", status="completed")
        self.assertEqual(repo.get("bld-job-test")["status"], "running")

        repo.update_job_status("j-img-1", status="completed")
        self.assertEqual(repo.get("bld-job-test")["status"], "completed")

    def test_worker_repo_crud_and_heartbeat(self):
        repo = self.db.workers

        worker = repo.register(
            id="worker-arm64",
            hostname="orange.lan",
            native_arch="arm64",
            arch_scores={"arm64": 1.0, "amd64": 0.3},
            free_disk_gb=45.2,
            token="tok-worker-secret",
        )
        self.assertEqual(worker["id"], "worker-arm64")
        self.assertEqual(worker["hostname"], "orange.lan")
        self.assertEqual(worker["arch_scores"], {"arm64": 1.0, "amd64": 0.3})
        self.assertEqual(worker["free_disk_gb"], 45.2)
        self.assertEqual(worker["status"], "online")
        created_at = worker["created_at"]

        time.sleep(0.01)
        re_registered = repo.register(
            id="worker-arm64",
            hostname="orange-updated.lan",
            native_arch="arm64",
            arch_scores={"arm64": 1.0},
            free_disk_gb=50.0,
            token="tok-worker-secret-new",
        )
        self.assertEqual(re_registered["hostname"], "orange-updated.lan")
        self.assertEqual(re_registered["free_disk_gb"], 50.0)
        self.assertEqual(re_registered["created_at"], created_at)

        by_token = repo.get_by_token("tok-worker-secret-new")
        self.assertIsNotNone(by_token)
        self.assertEqual(by_token["id"], "worker-arm64")

        self.assertTrue(repo.heartbeat("worker-arm64", free_disk_gb=42.0))
        self.assertEqual(repo.get("worker-arm64")["free_disk_gb"], 42.0)
        self.assertFalse(repo.heartbeat("nonexistent", free_disk_gb=10.0))

        repo.register(
            id="worker-x86",
            hostname="desktop.lan",
            native_arch="amd64",
            arch_scores={"amd64": 1.0},
            free_disk_gb=120.0,
            token="tok-x86",
            status="busy",
        )
        self.assertEqual(len(repo.list()), 2)
        self.assertEqual(len(repo.list(status="busy")), 1)
        self.assertEqual(repo.list(status="busy")[0]["id"], "worker-x86")

        self.assertTrue(repo.update_status("worker-x86", "offline"))
        self.assertEqual(repo.get("worker-x86")["status"], "offline")

        self.assertTrue(repo.delete("worker-x86"))
        self.assertIsNone(repo.get("worker-x86"))

    def test_token_repo_lifecycle_and_expiry(self):
        repo = self.db.tokens

        tok1 = repo.issue(user_id="alice", kind="pat")
        self.assertIsNotNone(tok1["token"])
        self.assertTrue(tok1["id"].startswith("tok_"))
        self.assertEqual(tok1["user_id"], "alice")
        self.assertEqual(tok1["kind"], "pat")
        self.assertIsNone(tok1["expires_at"])
        self.assertIsNotNone(repo.validate(tok1["token"]))

        tok2 = repo.issue(user_id="bob", kind="pat", expires_at=time.time() + 3600)
        self.assertIsNotNone(repo.validate(tok2["token"]))

        tok3 = repo.issue(user_id="carol", kind="pat", expires_at=time.time() - 60)
        self.assertIsNotNone(repo.get(tok3["id"]))
        self.assertIsNone(repo.validate(tok3["token"]))

        alice_tokens = repo.list(user_id="alice")
        self.assertEqual(len(alice_tokens), 1)
        self.assertEqual(alice_tokens[0]["id"], tok1["id"])

        self.assertTrue(repo.revoke(tok1["id"]))
        self.assertIsNone(repo.validate(tok1["token"]))
        self.assertFalse(repo.revoke("nonexistent"))

    def test_tokens_are_hashed_at_rest(self):
        repo = self.db.tokens
        tok = repo.issue(user_id="alice", kind="admin")

        for rec in (repo.validate(tok["token"]), repo.get(tok["id"]), repo.list()[0]):
            self.assertEqual(rec["id"], tok["id"])
            self.assertNotIn("token", rec)
            self.assertNotIn("token_hash", rec)

        self.assertIsNone(repo.validate("not-the-secret"))
        self.assertIsNone(repo.validate(hash_secret(tok["token"])))

        rows = self.db.conn.execute("SELECT * FROM tokens").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertNotIn(tok["token"], [str(v) for v in tuple(rows[0])])
        self.assertEqual(rows[0]["token_hash"], hash_secret(tok["token"]))

        self.db.conn.commit()
        self.db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        with open(self.db_path, "rb") as f:
            self.assertNotIn(tok["token"].encode(), f.read())

    def test_token_kind_is_constrained(self):
        for kind in ("pat", "admin", "worker"):
            self.db.tokens.issue(user_id="alice", kind=kind)
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.tokens.issue(user_id="alice", kind="root")

    def test_project_name_validation(self):
        for good in ("ab", "demo", "my-proj-1", "0day", "a" * 41):
            self.assertEqual(validate_project_name(good), good)
        for bad in ("", "a", "-abc", "Demo", "has space", "under_score", "a/b", "a" * 42):
            with self.assertRaises(ValueError):
                validate_project_name(bad)

        with self.assertRaises(ValueError):
            self.db.projects.create("Bad_Name")
        with self.assertRaises(ValueError):
            self.db.ensure_project("../etc")
        self.assertIsNone(self.db.projects.get("Bad_Name"))

    def test_user_repo(self):
        repo = self.db.users
        alice = repo.create("alice")
        self.assertFalse(alice["is_admin"])
        self.assertTrue(alice["active"])
        repo.create("root", is_admin=True)
        self.assertEqual([u["id"] for u in repo.list()], ["alice", "root"])

        self.assertTrue(repo.set_admin("alice", True))
        self.assertTrue(repo.get("alice")["is_admin"])
        self.assertTrue(repo.set_active("alice", False))
        self.assertFalse(repo.get("alice")["active"])
        self.assertFalse(repo.set_admin("ghost", True))
        self.assertIsNone(repo.get("ghost"))
        with self.assertRaises(sqlite3.IntegrityError):
            repo.create("alice")

    def test_user_default_project(self):
        projects, users = self.db.projects, self.db.users
        core = projects.create("core")
        web = projects.create("web")
        users.create("alice")
        self.assertIsNone(users.get("alice")["default_project"])

        self.assertTrue(users.set_default_project("alice", core["id"]))
        self.assertEqual(users.get("alice")["default_project"], core["id"])
        self.assertTrue(users.set_default_project("alice", None))
        self.assertIsNone(users.get("alice")["default_project"])
        self.assertFalse(users.set_default_project("ghost", core["id"]))
        with self.assertRaises(sqlite3.IntegrityError):
            users.set_default_project("alice", "no-such-project")

        # Losing the membership drops the default, only for that project.
        projects.add_member(core["id"], "alice")
        projects.add_member(web["id"], "alice")
        users.set_default_project("alice", core["id"])
        projects.remove_member(web["id"], "alice")
        self.assertEqual(users.get("alice")["default_project"], core["id"])
        projects.remove_member(core["id"], "alice")
        self.assertIsNone(users.get("alice")["default_project"])

        # Deleting the project drops it for everybody.
        users.create("bob")
        users.set_default_project("alice", web["id"])
        users.set_default_project("bob", web["id"])
        projects.delete(web["id"])
        self.assertIsNone(users.get("alice")["default_project"])
        self.assertIsNone(users.get("bob")["default_project"])

    def test_default_project_survives_deactivation(self):
        project = self.db.projects.create("core")
        users = self.db.users
        users.create("alice")
        users.set_default_project("alice", project["id"])
        users.update("alice", active=False)
        users.update("alice", active=True)
        self.assertEqual(users.get("alice")["default_project"], project["id"])
        users.set_active("alice", False)
        users.set_active("alice", True)
        self.assertEqual(users.get("alice")["default_project"], project["id"])

    def test_dev_only_project(self):
        repo = self.db.projects
        home = repo.create("home-alice", dev_only=True)
        self.assertIs(home["dev_only"], True)
        self.assertEqual(home["prod_bucket"], "")
        self.assertEqual(home["dev_bucket"], "seine-home-alice-dev")
        self.assertIs(repo.get("home-alice")["dev_only"], True)

        normal = repo.create("team")
        self.assertIs(normal["dev_only"], False)
        self.assertEqual(normal["prod_bucket"], "seine-team-prod")
        self.assertEqual([p["dev_only"] for p in repo.list()], [True, False])

        with self.assertRaises(ValueError):
            repo.create("home-bob", prod_bucket="b", dev_only=True)

    def test_home_prefix_is_reserved_for_home_projects(self):
        repo = self.db.projects
        with self.assertRaises(ValueError):
            repo.create("home-alice")
        with self.assertRaises(ValueError):
            repo.ensure("home-alice")
        self.assertIsNone(repo.get("home-alice"))
        repo.create("homework")
        repo.create("home-bob", dev_only=True)

        self.db.provision_new_user("carol", mode="auto")
        self.assertEqual(repo.ensure("home-carol")["name"], "home-carol")
        self.assertTrue(repo.get("home-carol")["dev_only"])

    def test_new_user_without_a_project_by_default(self):
        for is_admin, mode in ((False, "none"), (True, "auto"), (True, "shared")):
            user = self.db.provision_new_user(f"u-{is_admin}-{mode}", is_admin, mode)
            self.assertIsNone(user["default_project"])
        self.assertEqual(self.db.projects.list(), [])

    def test_auto_gives_a_user_a_dev_only_home_project(self):
        user = self.db.provision_new_user("alice", mode="auto")
        self.assertEqual(user["default_project"], "home-alice")
        home = self.db.projects.get("home-alice")
        self.assertTrue(home["dev_only"])
        self.assertEqual(home["prod_bucket"], "")
        self.assertEqual(home["dev_bucket"], "seine-home-alice-dev")
        member = self.db.projects.get_member("home-alice", "alice")
        self.assertEqual(member["role"], "developer")

    def test_home_project_name_comes_from_the_user_id(self):
        names = [
            self.db.provision_new_user(uid, mode="auto")["default_project"]
            for uid in ("Alice.Smith_1", "alice-smith-1", "alice smith 1", "!!!")
        ]
        self.assertEqual(
            names, ["home-alice-smith-1", "home-alice-smith-1-2", "home-alice-smith-1-3", "home-user"])
        for name in names:
            validate_project_name(name)
        long_name = self.db.provision_new_user("x" * 80, mode="auto")["default_project"]
        validate_project_name(long_name)

    def test_shared_project_is_joined_and_becomes_the_default(self):
        self.db.projects.create("team")
        user = self.db.provision_new_user("bob", mode="team")
        self.assertEqual(user["default_project"], "team")
        self.assertEqual(self.db.projects.get_member("team", "bob")["role"], "developer")
        self.assertEqual([p["name"] for p in self.db.projects.list()], ["team"])

    def test_failed_provisioning_creates_nothing(self):
        with self.assertRaises(ValueError):
            self.db.provision_new_user("carol", mode="missing")
        self.assertIsNone(self.db.users.get("carol"))

        self.db.users.create("dave")
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.provision_new_user("dave", mode="auto")
        self.assertEqual(self.db.projects.list(), [])

    def test_job_columns_defaults(self):
        self.db.projects.create("demo")
        self.db.builds.create(id="bld-1", project="demo")
        job = self.db.builds.create_job(id="job-1", build_id="bld-1")
        self.assertEqual(job["cancel_requested"], 0)
        self.assertEqual(job["attempts"], 0)


class DatabaseConstraintsAndIntegrationTest(Test):
    """Test foreign key enforcement, cascading, multi-engine config, and job scheduling."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="seine-test-int-")
        self.db_path = os.path.join(self.tmp_dir, "int.db")
        self.db = Database(self.db_path)

    def tearDown(self):
        self.db.close()
        if os.path.exists(self.tmp_dir):
            shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_foreign_key_rejections(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.conn.execute(
                "INSERT INTO project_members (project_id, user_id, role, created_at) VALUES (?, ?, ?, ?)",
                ("ghost-proj", "alice", "developer", time.time()),
            )

        with self.assertRaises(sqlite3.IntegrityError):
            self.db.conn.execute(
                "INSERT INTO builds (id, project, target_arch, created_at) VALUES (?, ?, ?, ?)",
                ("bld-ghost", "ghost-proj", "amd64", time.time()),
            )

        with self.assertRaises(sqlite3.IntegrityError):
            self.db.conn.execute(
                "INSERT INTO jobs (id, build_id, kind, target_arch, created_at) VALUES (?, ?, 'image', 'amd64', ?)",
                ("job-ghost", "bld-ghost", time.time()),
            )

        self.db.projects.create("valid-proj")
        self.db.builds.create(id="bld-valid", project="valid-proj", target_arch="amd64")
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.conn.execute(
                "INSERT INTO jobs (id, build_id, kind, target_arch, worker_id, created_at) VALUES (?, ?, 'image', 'amd64', ?, ?)",
                ("job-bad-worker", "bld-valid", "ghost-worker", time.time()),
            )

    def test_foreign_key_cascading(self):
        self.db.projects.create("cascade-proj")
        self.db.projects.add_member("cascade-proj", "alice", "developer")

        self.db.workers.register(
            id="worker-w1",
            hostname="w1.lan",
            native_arch="amd64",
            arch_scores={"amd64": 1.0},
            free_disk_gb=50.0,
            token="tok-w1",
        )

        self.db.builds.create(id="bld-casc", project="cascade-proj", target_arch="amd64")
        self.db.builds.create_job(
            id="job-casc",
            build_id="bld-casc",
            target_arch="amd64",
            worker_id="worker-w1",
        )

        # Deleting worker sets job's worker_id to NULL without deleting the job
        self.db.workers.delete("worker-w1")
        job = self.db.builds.get_job("job-casc")
        self.assertIsNotNone(job)
        self.assertIsNone(job["worker_id"])

        self.assertTrue(self.db.projects.delete("cascade-proj"))
        self.assertIsNone(self.db.projects.get_member("cascade-proj", "alice"))
        self.assertIsNone(self.db.builds.get("bld-casc"))
        self.assertIsNone(self.db.builds.get_job("job-casc"))

    def test_database_factory_and_configuration(self):
        factory_db_path = os.path.join(self.tmp_dir, "factory.db")

        db1 = Database(factory_db_path)
        self.assertEqual(db1.engine, "sqlite")
        self.assertIsInstance(db1.projects, ProjectRepo)
        self.assertIsInstance(db1.builds, BuildRepo)
        self.assertIsInstance(db1.workers, WorkerRepo)
        self.assertIsInstance(db1.tokens, TokenRepo)
        db1.close()

        cfg_nested = {"database": {"type": "sqlite", "path": factory_db_path}}
        db2 = Database(cfg_nested)
        self.assertEqual(db2.engine, "sqlite")
        db2.close()

        cfg_direct = {"type": "sqlite", "path": factory_db_path}
        with Database.from_config(cfg_direct) as db3:
            self.assertEqual(db3.engine, "sqlite")
            db3.projects.ensure("factory-proj")
            self.assertIsNotNone(db3.projects.get("factory-proj"))

        with self.assertRaises(NotImplementedError):
            Database({"type": "postgres", "url": "postgresql://user:pass@localhost/db"})

        with self.assertRaises(ValueError):
            Database({"type": "mysql", "url": "mysql://..."})

    def test_capability_aware_job_claiming(self):
        self.db.projects.create("demo", dev_bucket="s3-dev", prod_bucket="s3-prod")

        self.db.workers.register(
            id="worker-rk3588",
            hostname="orange.lan",
            native_arch="arm64",
            arch_scores={"arm64": 1.0, "amd64": 0.7},
            free_disk_gb=40.0,
            token="tok-arm",
            concurrency_slots=2,
        )

        bld_dev = self.db.create_build(
            build_id="bld-dev-1",
            project="demo",
            target_arch="arm64",
            worktree_digest="digest-dev",
            is_release=False,
        )
        self.assertEqual(bld_dev, "bld-dev-1")

        claimed = self.db.claim_next_job("worker-rk3588")
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["build_id"], "bld-dev-1")
        self.assertEqual(claimed["target_arch"], "arm64")
        self.assertEqual(claimed["s3_bucket"], "s3-dev")

        self.assertIsNone(self.db.claim_next_job("worker-rk3588"))

        self.db.create_build(
            build_id="bld-rel-1",
            project="demo",
            target_arch="amd64",
            worktree_digest="digest-rel",
            is_release=True,
        )

        claimed_rel = self.db.claim_next_job("worker-rk3588")
        self.assertIsNotNone(claimed_rel)
        self.assertEqual(claimed_rel["build_id"], "bld-rel-1")
        self.assertEqual(claimed_rel["target_arch"], "amd64")
        self.assertEqual(claimed_rel["s3_bucket"], "s3-prod")
