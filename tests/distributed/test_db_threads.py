# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0
"""Tests for the per-thread SQLite connections of Database."""

import os
import shutil
import tempfile
import threading

from avocado import Test

from seine.distributed.server.db import Database


class ThreadConnectionTest(Test):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="seine-test-dbthreads-")

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_threads_share_one_in_memory_db(self):
        self.db = db = Database(":memory:")
        db.create_build("bld-main", "alpha", "amd64", "digest")
        seen = []
        t = threading.Thread(target=lambda: seen.append(db.get_build("bld-main")))
        t.start()
        t.join()
        self.assertEqual(seen[0]["id"], "bld-main")

    def test_each_thread_gets_its_own_connection(self):
        self.db = db = Database(os.path.join(self.tmp_dir, "d.db"))
        ids = []
        t = threading.Thread(target=lambda: ids.append(id(db.conn._get())))
        t.start()
        t.join()
        self.assertNotEqual(ids[0], id(db.conn._get()))

    def test_connections_get_row_factory_and_foreign_keys(self):
        self.db = db = Database(os.path.join(self.tmp_dir, "e.db"))
        seen = []

        def probe():
            seen.append(db.conn.execute("PRAGMA foreign_keys").fetchone()[0])
            seen.append(db.conn.execute("SELECT 1 AS one").fetchone()["one"])

        t = threading.Thread(target=probe)
        t.start()
        t.join()
        self.assertEqual(seen, [1, 1])

    def test_threads_create_builds_in_parallel(self):
        self.db = db = Database(os.path.join(self.tmp_dir, "p.db"))
        errors = []

        def work(n):
            try:
                for i in range(10):
                    db.create_build(f"bld-{n}-{i}", "alpha", "amd64", "digest")
            except Exception as e:
                errors.append(repr(e))

        threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(db.builds.list()), 80)
