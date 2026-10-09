# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import contextlib
import os
import shutil
import sys
import tempfile
import time
from unittest import mock

import avocado

path_to_self = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.insert(0, path_to_sources)

@contextlib.contextmanager
def _tui_required(test):
    try:
        yield
    except ImportError as e:
        test.cancel("the 'tui' extra (textual) is not installed: %s" % e)


class RemoteViewsTest(avocado.Test):
    """
    :avocado: tags=tui
    """

    def setUp(self):
        with _tui_required(self):
            from textual.app import App
            from seine.tui.app import SeineApp
            from seine.tui.remote_screen import RemoteScreen
            from seine.tui.remote_session import RemoteSession
            from seine.utils import format_size, format_timestamp
            from seine.tui.render_remote import (
                render_remote_detail,
                extract_remote_artifacts,
                render_remote_artifacts,
                render_remote_builds,
                render_remote_workers,
            )
        self.App = App
        self.SeineApp = SeineApp
        self.RemoteScreen = RemoteScreen
        self.RemoteSession = RemoteSession
        self.render_remote_builds = render_remote_builds
        self.render_remote_workers = render_remote_workers
        self.render_remote_artifacts = render_remote_artifacts
        self.extract_remote_artifacts = extract_remote_artifacts
        self.detail = render_remote_detail
        self._format_size = format_size
        self._format_timestamp = format_timestamp

        self.tmp_dir = tempfile.mkdtemp(prefix="seine-test-remote-views-")
        os.environ["XDG_CONFIG_HOME"] = self.tmp_dir
        # Keep the developer's real CA out of the tests.
        self.env = mock.patch.dict(os.environ, {"SEINE_CA_CERT": ""})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_render_builds_empty(self):
        rendered = self.render_remote_builds([])
        self.assertIn("REMOTE BUILDS", rendered)
        self.assertIn("No remote builds recorded.", rendered)
        self.assertIn("[Enter] Stream Logs", rendered)

    def test_render_builds_populated_and_selection(self):
        now = time.time()
        builds = [
            {
                "id": "b11111111111111111",
                "project": "distro-core",
                "target_arch": "amd64",
                "status": "running",
                "started_at": now - 45,
                "finished_at": None,
                "user_id": "alice",
            },
            {
                "id": "b22222222222222222",
                "project": "distro-apps",
                "target_arch": "arm64",
                "status": "completed",
                "started_at": now - 120,
                "finished_at": now - 30,
                "user_id": "bob",
            },
            {
                "id": "b3333333333333333",
                "project": "distro-test",
                "target_arch": "riscv64",
                "status": "failed",
                "started_at": None,
                "finished_at": None,
                "user_id": "carol",
            },
            {
                "id": "b4444444444444444",
                "project": "distro-edge",
                "target_arch": "arm64",
                "status": "cancelled",
                "started_at": None,
                "finished_at": None,
                "user_id": "dave",
            },
            {
                "id": "b5555555555555555",
                "project": "distro-demo",
                "target_arch": "amd64",
                "status": "queued",
                "started_at": None,
                "finished_at": None,
                "user_id": "eve",
            },
        ]
        rendered0 = self.render_remote_builds(builds, selected_index=0)
        self.assertIn(" ▸ ● running", rendered0)
        self.assertIn("   ✔ completed", rendered0)
        self.assertIn("   ✖ failed", rendered0)
        self.assertIn("   ⊘ cancelled", rendered0)
        self.assertIn("   ⋯ queued", rendered0)
        self.assertIn("b11111111111", rendered0)
        self.assertIn("distro-core", rendered0)
        self.assertIn("amd64", rendered0)
        self.assertIn("alice", rendered0)

        # Test cursor on index 1
        rendered1 = self.render_remote_builds(builds, selected_index=1)
        self.assertIn("   ● running", rendered1)
        self.assertIn(" ▸ ✔ completed", rendered1)

    def test_render_workers_empty(self):
        rendered = self.render_remote_workers([])
        self.assertIn("REMOTE WORKERS", rendered)
        self.assertIn("No remote workers registered.", rendered)
        self.assertIn("[p] Pause/Resume Worker", rendered)

    def test_render_workers_populated_and_selection(self):
        workers = [
            {
                "id": "w11111111111111111",
                "hostname": "node-alpha.lan",
                "native_arch": "amd64",
                "concurrency_slots": 4,
                "free_disk_gb": 120.5,
                "status": "online",
                "arch_scores": {"amd64": 1.0, "arm64": 0.8},
            },
            {
                "id": "w22222222222222222",
                "hostname": "node-beta.lan",
                "native_arch": "arm64",
                "concurrency_slots": 2,
                "free_disk_gb": 45.0,
                "status": "paused",
                "arch_scores": {"arm64": 1.0},
            },
            {
                "id": "w33333333333333333",
                "hostname": "node-gamma.lan",
                "native_arch": "riscv64",
                "concurrency_slots": 1,
                "free_disk_gb": 10.2,
                "status": "offline",
                "arch_scores": {},
            },
        ]
        rendered0 = self.render_remote_workers(workers, selected_index=0)
        self.assertIn(" ▸ ● online", rendered0)
        self.assertIn("   ⏸ paused", rendered0)
        self.assertIn("   ✖ offline", rendered0)
        self.assertIn("w11111111111", rendered0)
        self.assertIn("node-alpha.lan", rendered0)
        self.assertIn("120.5 GB", rendered0)
        self.assertIn("amd64:1.0 arm64:0.8", rendered0)

        # Test cursor on index 1
        rendered1 = self.render_remote_workers(workers, selected_index=1)
        self.assertIn("   ● online", rendered1)
        self.assertIn(" ▸ ⏸ paused", rendered1)

    def test_cursor_navigation_and_clamping(self):
        mock_app = mock.Mock()
        mock_app.is_running = False
        screen = self.RemoteScreen()
        screen.update_body = mock.Mock()
        with mock.patch.object(self.RemoteScreen, "app", new_callable=mock.PropertyMock, return_value=mock_app):
            screen.active_tab = 1
            screen.remote_builds = [{"id": "b1"}, {"id": "b2"}, {"id": "b3"}]
            self.assertEqual(screen.selected_indices[1], 0)

            screen.action_cursor_down()
            self.assertEqual(screen.selected_indices[1], 1)
            screen.action_cursor_down()
            self.assertEqual(screen.selected_indices[1], 2)
            # Cannot exceed length - 1
            screen.action_cursor_down()
            self.assertEqual(screen.selected_indices[1], 2)

            screen.action_cursor_up()
            self.assertEqual(screen.selected_indices[1], 1)
            screen.action_cursor_up()
            self.assertEqual(screen.selected_indices[1], 0)
            # Cannot go below 0
            screen.action_cursor_up()
            self.assertEqual(screen.selected_indices[1], 0)

    def test_fetch_data_builds_and_workers(self):
        mock_app = mock.Mock()
        mock_app.is_running = False
        session = self.RemoteSession(app=mock_app)
        session.connected = True
        session.url = "http://cluster.lan:8000"
        session.token = "test-token"
        mock_app.remote_session = session

        screen = self.RemoteScreen()
        screen.update_body = mock.Mock()
        with mock.patch.object(self.RemoteScreen, "app", new_callable=mock.PropertyMock, return_value=mock_app):
            # Fetch tab 1 (builds)
            with mock.patch("requests.get") as mock_get:
                mock_resp = mock.Mock(status_code=200)
                mock_resp.json.return_value = [{"id": "b1", "status": "running"}]
                mock_get.return_value = mock_resp

                screen.active_tab = 1
                screen.fetch_data()
                mock_get.assert_called_with(
                    "http://cluster.lan:8000/api/v1/builds",
                    headers={"Authorization": "Bearer test-token"},
                    timeout=5.0,
                    verify=True,
                )
                self.assertEqual(len(screen.remote_builds), 1)
                self.assertEqual(screen.remote_builds[0]["id"], "b1")

            # Fetch tab 2 (workers)
            with mock.patch("requests.get") as mock_get:
                mock_resp = mock.Mock(status_code=200)
                mock_resp.json.return_value = {"workers": [{"id": "w1", "status": "online"}]}
                mock_get.return_value = mock_resp

                screen.active_tab = 2
                screen.fetch_data()
                mock_get.assert_called_with(
                    "http://cluster.lan:8000/api/v1/workers",
                    headers={"Authorization": "Bearer test-token"},
                    timeout=5.0,
                    verify=True,
                )
                self.assertEqual(len(screen.remote_workers), 1)
                self.assertEqual(screen.remote_workers[0]["id"], "w1")

    def test_cancel_build_action(self):
        mock_app = mock.Mock()
        mock_app.is_running = False
        session = self.RemoteSession(app=mock_app)
        session.connected = True
        session.url = "http://cluster.lan:8000"
        session.token = "test-token"
        mock_app.remote_session = session

        screen = self.RemoteScreen()
        screen.say = mock.Mock()
        screen.fetch_data = mock.Mock()

        with mock.patch.object(self.RemoteScreen, "app", new_callable=mock.PropertyMock, return_value=mock_app):
            screen.active_tab = 1
            screen.remote_builds = [{"id": "build-abcdef123456789"}]
            screen.selected_indices[1] = 0

            with mock.patch("requests.post") as mock_post:
                mock_resp = mock.Mock(status_code=200)
                mock_post.return_value = mock_resp

                screen.action_cancel_build()
                mock_post.assert_called_once_with(
                    "http://cluster.lan:8000/api/v1/builds/build-abcdef123456789/cancel",
                    headers={"Authorization": "Bearer test-token"},
                    timeout=5.0,
                    verify=True,
                )
                screen.say.assert_called_with("build build-abcdef cancel requested", error=False)
                screen.fetch_data.assert_called_once()

    def test_worker_pause_and_deregister_actions(self):
        mock_app = mock.Mock()
        mock_app.is_running = False
        session = self.RemoteSession(app=mock_app)
        session.connected = True
        session.is_admin = True
        session.url = "http://cluster.lan:8000"
        session.token = "admin-token"
        mock_app.remote_session = session

        screen = self.RemoteScreen()
        screen.say = mock.Mock()
        screen.fetch_data = mock.Mock()

        with mock.patch.object(self.RemoteScreen, "app", new_callable=mock.PropertyMock, return_value=mock_app):
            screen.active_tab = 2
            screen.remote_workers = [
                {"id": "worker-111122223333", "status": "online"},
                {"id": "worker-444455556666", "status": "paused"},
            ]
            screen.selected_indices[2] = 0

            # Pause online worker
            with mock.patch("requests.post") as mock_post:
                mock_post.return_value = mock.Mock(status_code=200)
                screen.action_toggle_worker_pause()
                mock_post.assert_called_once_with(
                    "http://cluster.lan:8000/api/v1/workers/worker-111122223333/pause",
                    json={"paused": True},
                    headers={"Authorization": "Bearer admin-token"},
                    timeout=5.0,
                    verify=True,
                )
                screen.say.assert_called_with("worker worker-11112 paused", error=False)

            # Resume paused worker
            screen.selected_indices[2] = 1
            with mock.patch("requests.post") as mock_post:
                mock_post.return_value = mock.Mock(status_code=200)
                screen.action_toggle_worker_pause()
                mock_post.assert_called_once_with(
                    "http://cluster.lan:8000/api/v1/workers/worker-444455556666/pause",
                    json={"paused": False},
                    headers={"Authorization": "Bearer admin-token"},
                    timeout=5.0,
                    verify=True,
                )
                screen.say.assert_called_with("worker worker-44445 resumed", error=False)

            # Deregister worker
            with mock.patch("requests.delete") as mock_del:
                mock_del.return_value = mock.Mock(status_code=200)
                screen.action_deregister_worker()
                mock_del.assert_called_once_with(
                    "http://cluster.lan:8000/api/v1/workers/worker-444455556666",
                    headers={"Authorization": "Bearer admin-token"},
                    timeout=5.0,
                    verify=True,
                )
                screen.say.assert_called_with("worker worker-44445 deregistered", error=False)

    def test_format_size(self):
        self.assertEqual(self._format_size(0), "0 B")
        self.assertEqual(self._format_size(512), "512 B")
        self.assertEqual(self._format_size(1024), "1.0 KB")
        self.assertEqual(self._format_size(1048576), "1.0 MB")
        self.assertEqual(self._format_size(1500000000), "1.4 GB")
        self.assertEqual(self._format_size(None), "0 B")
        self.assertEqual(self._format_size("bad"), "0 B")

    def test_detail_placeholders(self):
        self.assertIn("No active server connection", self.detail(1, {}, connected=False))
        for tab in (1, 2, 3, 4, 5):
            self.assertIn("Select an item", self.detail(tab, None))

    def test_detail_build(self):
        build = {
            "id": "b" * 32, "project": "demo", "target_arch": "arm64", "status": "completed",
            "user_id": "alice", "created_at": 1000, "started_at": 1010, "finished_at": 1100,
            "worktree_digest": "abc123", "spec_file": "main.yaml",
            "artifact_meta": [{"name": "disk.img", "size": 2048, "sha256": "f" * 64}],
            "artifacts_expired_at": 2000, "artifacts_expired_reason": "ttl",
        }
        text = self.detail(1, build)
        for want in ("b" * 32, "demo", "arm64", "completed", "alice", "1m30s",
                     "abc123", "main.yaml", "disk.img", "2.0 KB", "f" * 64, "expired"):
            self.assertIn(want, text)

    def test_detail_worker(self):
        worker = {"id": "w1", "hostname": "node", "native_arch": "amd64", "status": "paused",
                  "arch_scores": {"arm64": 0.5}, "concurrency_slots": 4, "free_disk_gb": 12.34,
                  "last_seen": 1}
        text = self.detail(2, worker)
        for want in ("w1", "node", "paused", "arm64", "0.5", "12.3 GB", "ago"):
            self.assertIn(want, text)

    def test_detail_artifact_with_progress(self):
        art = {"name": "disk.img", "size": 1048576, "sha256": "e" * 64, "key": "k/disk.img",
               "build_id": "b1", "project": "demo", "target_arch": "amd64"}
        text = self.detail(3, art, progress={"state": "running", "read": 524288, "total": 1048576})
        for want in ("1.0 MB", "e" * 64, "k/disk.img", "b1", "50%", "512.0 KB / 1.0 MB"):
            self.assertIn(want, text)
        self.assertIn("expired", self.detail(3, dict(art, expired=True)))

    def test_detail_user_and_project(self):
        tokens = [{"id": "t1", "user_id": "alice", "created_at": 5}, {"id": "t2", "user_id": "bob"}]
        text = self.detail(4, {"id": "alice", "is_admin": True, "active": False}, tokens=tokens)
        for want in ("System Administrator", "Disabled", "TOKENS (1)", "t1"):
            self.assertIn(want, text)
        self.assertNotIn("t2", text)
        project = {"name": "demo", "dev_bucket": "d", "prod_bucket": "p", "quota_gb": 5.0,
                   "members": [{"user_id": "alice", "role": "releaser"}]}
        builds = [{"project": "demo", "status": "queued"}, {"project": "other", "status": "queued"}]
        text = self.detail(5, project, builds=builds)
        for want in ("dev + prod", "5 GB", "alice  releaser"):
            self.assertIn(want, text)
        self.assertIn("dev-only", self.detail(5, dict(project, dev_only=1)))

    def test_detail_ops(self):
        text = self.detail(6, None, settings={"url": "srv", "ping_ms": 3}, stats={"queued": 2})
        for want in ("srv", "3 ms", "Queued"):
            self.assertIn(want, text)

    def test_format_timestamp(self):
        self.assertEqual(self._format_timestamp(None), "--")
        self.assertEqual(self._format_timestamp("bad"), "--")
        self.assertRegex(self._format_timestamp(86400 * 365), r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")
        self.assertRegex(self._format_timestamp(86400 * 365, date_only=True), r"^\d{4}-\d{2}-\d{2}$")

    def test_render_artifacts_empty(self):
        rendered = self.render_remote_artifacts([])
        self.assertIn("REMOTE ARTIFACTS", rendered)
        self.assertIn("No artifacts found in remote builds.", rendered)
        self.assertNotIn("[Enter] Download Artifact", rendered)

    def test_render_artifacts_marks_expired_ones(self):
        rendered = self.render_remote_artifacts([
            {"name": "disk.img", "size": 2048, "expired": True, "build_id": "b1", "project": "demo"},
            {"name": "[expired artifact]", "expired": True, "build_id": "b0"},
        ])
        row, legacy = rendered.splitlines()[4:6]
        for want in ("disk.img", "2.0 KB"):
            self.assertIn(want, row)
        self.assertTrue(row.endswith("expired"))
        self.assertTrue(legacy.endswith("expired"))
        self.assertIn(" - ", legacy)

    def test_render_artifacts_populated_and_selection(self):
        artifacts = [
            {
                "name": "pc-image.img",
                "size": 1500000000,
                "sha256": "111122223333",
                "build_id": "b111111111111111",
                "project": "distro-core",
                "target_arch": "amd64",
            },
            {
                "name": "pc-image.rootfs.tar",
                "size": 718274560,
                "sha256": "444455556666",
                "build_id": "b111111111111111",
                "project": "distro-core",
                "target_arch": "amd64",
            },
        ]
        rendered0 = self.render_remote_artifacts(artifacts, selected_index=0)
        self.assertIn(" ▸ pc-image.img", rendered0)
        self.assertIn("   pc-image.rootfs.tar", rendered0)
        self.assertIn("1.4 GB", rendered0)
        self.assertIn("685.0 MB", rendered0)
        self.assertIn("b11111111111", rendered0)
        self.assertIn("distro-core", rendered0)
        self.assertIn("amd64", rendered0)
        self.assertNotIn("[Enter] Download Artifact", rendered0)

        rendered1 = self.render_remote_artifacts(artifacts, selected_index=1)
        self.assertIn("   pc-image.img", rendered1)
        self.assertIn(" ▸ pc-image.rootfs.tar", rendered1)
        self.assertNotIn("[Enter] Download Artifact", rendered1)

    def test_download_artifact_key_on_artifacts_tab_is_noop(self):
        screen = self.RemoteScreen()
        screen.say = mock.Mock()
        screen.active_tab = 3
        screen.remote_artifacts = [
            {"name": "disk.raw", "build_id": "bld-1", "project": "demo"}
        ]
        screen.selected_indices[3] = 0
        screen.action_download_artifact()
        screen.say.assert_not_called()

    def test_extract_remote_artifacts(self):
        builds = [
            {"id": "b1", "project": "p", "target_arch": "arm64",
             "artifact_meta": [{"name": "a.img", "size": 5, "sha256": "x", "key": "k"}, "junk"]},
            {"id": "b2", "project": "p", "architecture": "amd64"},
        ]
        arts = self.extract_remote_artifacts(builds)
        self.assertEqual(len(arts), 1)
        self.assertEqual((arts[0]["name"], arts[0]["size"], arts[0]["key"]), ("a.img", 5, "k"))
        self.assertEqual((arts[0]["build_id"], arts[0]["target_arch"]), ("b1", "arm64"))

    def test_extract_remote_artifacts_sorted_by_recent_timestamp(self):
        builds = [
            {"id": "b1", "created_at": 100.0, "finished_at": 120.0,
             "artifact_meta": [{"name": "old.img", "size": 10}]},
            {"id": "b2", "created_at": 200.0, "finished_at": 250.0,
             "artifact_meta": [{"name": "new.img", "size": 20}]},
            {"id": "b3", "created_at": 150.0,
             "artifact_meta": [{"name": "mid.img", "size": 15}]},
        ]
        arts = self.extract_remote_artifacts(builds)
        self.assertEqual([a["name"] for a in arts], ["new.img", "mid.img", "old.img"])
        self.assertEqual(arts[0]["timestamp"], 250.0)
        self.assertEqual(arts[1]["timestamp"], 150.0)
        self.assertEqual(arts[2]["timestamp"], 120.0)

    def test_extract_remote_artifacts_expired(self):
        expired = {"artifacts_expired_at": 99, "artifacts_expired_reason": "pressure"}
        builds = [
            {"id": "b1", "artifact_meta": [{"name": "a.img", "size": 5}], **expired},
            {"id": "b2", "artifact_meta": [], **expired},
        ]
        kept, legacy = self.extract_remote_artifacts(builds)
        self.assertEqual((kept["name"], kept["size"], kept["expired"]), ("a.img", 5, True))
        self.assertEqual((kept["expired_at"], kept["expired_reason"]), (99, "pressure"))
        self.assertEqual(legacy["name"], "[expired artifact]")
        self.assertNotIn("size", legacy)

    def test_detail_expired_artifact(self):
        art = {"name": "disk.img", "size": 2048, "sha256": "e" * 64, "key": "k/disk.img",
               "build_id": "b1", "project": "demo", "target_arch": "amd64",
               "expired": True, "expired_at": 86400 * 365, "expired_reason": "ttl"}
        text = self.detail(3, art)
        for want in ("disk.img", "2.0 KB", "e" * 64, "k/disk.img", "State:", "expired",
                     "Expiry reason: age", "Expired at:", "expired (age)"):
            self.assertIn(want, text)

    def test_fetch_data_artifacts(self):
        mock_app = mock.Mock()
        mock_app.is_running = False
        session = self.RemoteSession(app=mock_app)
        session.connected = True
        session.url = "http://cluster.lan:8000"
        session.token = "test-token"
        mock_app.remote_session = session

        screen = self.RemoteScreen()
        screen.update_body = mock.Mock()
        with mock.patch.object(self.RemoteScreen, "app", new_callable=mock.PropertyMock, return_value=mock_app):
            with mock.patch("requests.get") as mock_get:
                mock_resp = mock.Mock(status_code=200)
                mock_resp.json.return_value = [
                    {
                        "id": "b1",
                        "project": "proj-a",
                        "target_arch": "arm64",
                        "status": "completed",
                        "artifact_meta": [
                            {"name": "rootfs.tar", "size": 1024, "sha256": "abc"},
                            {"name": "image.raw", "size": 2048, "sha256": "def"},
                        ],
                    }
                ]
                mock_get.return_value = mock_resp

                screen.active_tab = 3
                screen.fetch_data()
                mock_get.assert_called_with(
                    "http://cluster.lan:8000/api/v1/builds",
                    headers={"Authorization": "Bearer test-token"},
                    timeout=5.0,
                    verify=True,
                )
                self.assertEqual(len(screen.remote_artifacts), 2)
                self.assertEqual(screen.remote_artifacts[0]["name"], "rootfs.tar")
                self.assertEqual(screen.remote_artifacts[0]["build_id"], "b1")
                self.assertEqual(screen.remote_artifacts[1]["name"], "image.raw")

    def test_download_single_artifact_flow(self):
        import hashlib
        mock_app = mock.Mock()
        mock_app.is_running = False
        mock_app.download_dir = self.tmp_dir
        session = self.RemoteSession(app=mock_app)
        session.connected = True
        session.url = "http://cluster.lan:8000"
        session.token = "test-token"
        mock_app.remote_session = session

        screen = self.RemoteScreen()
        screen.say = mock.Mock()

        payload = b"test disk image payload"
        digest = hashlib.sha256(payload).hexdigest()

        with mock.patch.object(self.RemoteScreen, "app", new_callable=mock.PropertyMock, return_value=mock_app):
            screen.active_tab = 3
            screen.remote_artifacts = [
                {
                    "name": "disk.raw",
                    "build_id": "bld-123456",
                    "project": "demo",
                    "target_arch": "amd64",
                }
            ]
            screen.selected_indices[3] = 0

            with mock.patch("requests.get") as mock_get:
                build_resp = mock.Mock(status_code=200)
                build_resp.json.return_value = {
                    "id": "bld-123456",
                    "status": "completed",
                    "download_urls": {"disk.raw": "https://s3.lan/disk.raw"},
                    "artifacts": [{"name": "disk.raw", "size": len(payload), "sha256": digest}],
                }

                stream_resp = mock.Mock(status_code=200)
                stream_resp.iter_content.return_value = [payload]
                stream_resp.__enter__ = mock.Mock(return_value=stream_resp)
                stream_resp.__exit__ = mock.Mock(return_value=None)

                mock_get.side_effect = [build_resp, stream_resp]

                screen.action_view_logs()

                dest_file = os.path.join(self.tmp_dir, "disk.raw")
                self.assertTrue(os.path.exists(dest_file))
                with open(dest_file, "rb") as f:
                    self.assertEqual(f.read(), payload)

                screen.say.assert_called_with(
                    f"downloaded artifact 'disk.raw' to {self.tmp_dir}",
                    error=False,
                )
                # The presigned URL carries its own authorization: no token.
                _, kwargs = mock_get.call_args
                self.assertNotIn("Authorization", kwargs.get("headers") or {})
                self.assertIs(kwargs["verify"], True)

    def _download_with_progress(self, chunks, sha, size_hint=None, status="completed"):
        import hashlib
        from seine.tui.download import DownloadState
        mock_app = mock.Mock()
        mock_app.is_running = False
        mock_app.download_dir = self.tmp_dir
        mock_app.download_state = DownloadState()
        session = self.RemoteSession(app=mock_app)
        session.connected = True
        session.url = "https://cluster.lan:8000"
        session.token = "test-token"
        mock_app.remote_session = session
        screen = self.RemoteScreen()
        screen.say = mock.Mock()
        payload = b"".join(chunks)
        seen = []
        state = mock_app.download_state

        def iter_content(chunk_size):
            for chunk in chunks:
                seen.append(state.percent())
                yield chunk

        with mock.patch.object(self.RemoteScreen, "app", new_callable=mock.PropertyMock, return_value=mock_app):
            screen.active_tab = 3
            screen.remote_artifacts = [{"name": "disk.raw", "build_id": "bld-1"}]
            screen.selected_indices[3] = 0
            with mock.patch("requests.get") as mock_get:
                build_resp = mock.Mock(status_code=200)
                build_resp.json.return_value = {
                    "id": "bld-1", "status": status,
                    "download_urls": {"disk.raw": "https://s3.lan/disk.raw"},
                    "artifacts": [{"name": "disk.raw", "size": size_hint or len(payload),
                                   "sha256": sha or hashlib.sha256(payload).hexdigest()}],
                }
                stream_resp = mock.Mock(status_code=200)
                stream_resp.iter_content.side_effect = iter_content
                stream_resp.__enter__ = mock.Mock(return_value=stream_resp)
                stream_resp.__exit__ = mock.Mock(return_value=None)
                mock_get.side_effect = [build_resp, stream_resp]
                screen.action_view_logs()
        return state, seen

    def test_download_reports_its_progress_and_ends_done(self):
        state, seen = self._download_with_progress([b"x" * 50, b"y" * 50], sha=None)
        self.assertEqual(seen, [0, 50])
        item = state.snapshot()[("bld-1", "disk.raw")]
        self.assertEqual((item["state"], item["read"], item["total"]), ("done", 100, 100))
        self.assertFalse(state.active)

    def test_quitting_stops_a_download_and_removes_the_partial_file(self):
        import hashlib
        from seine.tui.download import DownloadState
        mock_app = mock.Mock()
        mock_app.is_running = False
        mock_app.download_dir = self.tmp_dir
        mock_app.download_state = state = DownloadState()
        session = self.RemoteSession(app=mock_app)
        session.connected = True
        session.url = "https://cluster.lan:8000"
        session.token = "test-token"
        mock_app.remote_session = session
        screen = self.RemoteScreen()
        screen.say = mock.Mock()
        payload = b"x" * 100

        def iter_content(chunk_size):
            yield payload[:50]
            state.cancel()
            yield payload[50:]

        with mock.patch.object(self.RemoteScreen, "app", new_callable=mock.PropertyMock, return_value=mock_app):
            screen.active_tab = 3
            screen.remote_artifacts = [{"name": "disk.raw", "build_id": "bld-1"}]
            screen.selected_indices[3] = 0
            with mock.patch("requests.get") as mock_get:
                build_resp = mock.Mock(status_code=200)
                build_resp.json.return_value = {
                    "id": "bld-1", "status": "completed",
                    "download_urls": {"disk.raw": "https://s3.lan/disk.raw"},
                    "artifacts": [{"name": "disk.raw", "size": 100,
                                   "sha256": hashlib.sha256(payload).hexdigest()}],
                }
                stream_resp = mock.Mock(status_code=200)
                stream_resp.iter_content.side_effect = iter_content
                stream_resp.__enter__ = mock.Mock(return_value=stream_resp)
                stream_resp.__exit__ = mock.Mock(return_value=None)
                mock_get.side_effect = [build_resp, stream_resp]
                screen.action_view_logs()
        self.assertEqual(state.snapshot()[("bld-1", "disk.raw")]["state"], "failed")
        self.assertFalse(state.active)
        self.assertEqual(sorted(os.listdir(self.tmp_dir)), [])
        self.assertTrue(any("cancelled" in str(c) for c in screen.say.call_args_list))

    def test_failed_download_is_marked_failed(self):
        state, _ = self._download_with_progress([b"x" * 10], sha="0" * 64)
        self.assertEqual(state.snapshot()[("bld-1", "disk.raw")]["state"], "failed")
        self.assertFalse(state.active)

    def test_artifacts_tab_shows_the_progress_of_each_download(self):
        from seine.tui.render_remote import render_remote_artifacts
        artifacts = [{"name": n, "build_id": "bld-1", "size": 100}
                     for n in ("a.raw", "b.raw", "c.raw", "d.raw", "e.raw")]
        progress = {
            ("bld-1", "a.raw"): {"state": "downloading", "read": 42, "total": 100},
            ("bld-1", "b.raw"): {"state": "done", "read": 100, "total": 100},
            ("bld-1", "c.raw"): {"state": "failed", "read": 3, "total": 100},
            ("bld-1", "d.raw"): {"state": "queued", "read": 0, "total": 100},
        }
        rows = render_remote_artifacts(artifacts, 0, progress).splitlines()
        by_name = {r.split()[1] if r.startswith(" ▸") else r.split()[0]: r for r in rows if ".raw" in r}
        self.assertTrue(by_name["a.raw"].endswith("42%"))
        self.assertTrue(by_name["b.raw"].endswith("✔ done"))
        self.assertTrue(by_name["c.raw"].endswith("✖ failed"))
        self.assertTrue(by_name["d.raw"].endswith("queued"))
        self.assertFalse(by_name["e.raw"].rstrip().endswith("%"))
        self.assertIn("DOWNLOAD", "\n".join(rows))

    def test_download_refuses_plain_http_artifact_url_unless_insecure(self):
        import hashlib
        payload = b"payload"
        digest = hashlib.sha256(payload).hexdigest()
        for insecure, expect_file in ((False, False), (True, True)):
            mock_app = mock.Mock()
            mock_app.is_running = False
            mock_app.download_dir = self.tmp_dir
            session = self.RemoteSession(app=mock_app)
            session.connected = True
            session.url = "https://cluster.lan:8000"
            session.token = "test-token"
            session.insecure = insecure
            mock_app.remote_session = session
            screen = self.RemoteScreen()
            screen.say = mock.Mock()
            with mock.patch.object(self.RemoteScreen, "app", new_callable=mock.PropertyMock, return_value=mock_app):
                screen.active_tab = 3
                screen.remote_artifacts = [{"name": "plain.raw", "build_id": "bld-1"}]
                screen.selected_indices[3] = 0
                with mock.patch("requests.get") as mock_get:
                    build_resp = mock.Mock(status_code=200)
                    build_resp.json.return_value = {
                        "id": "bld-1", "status": "completed",
                        "download_urls": {"plain.raw": "http://10.0.0.9:3900/plain.raw"},
                        "artifacts": [{"name": "plain.raw", "size": len(payload), "sha256": digest}],
                    }
                    stream_resp = mock.Mock(status_code=200)
                    stream_resp.iter_content.return_value = [payload]
                    stream_resp.__enter__ = mock.Mock(return_value=stream_resp)
                    stream_resp.__exit__ = mock.Mock(return_value=None)
                    mock_get.side_effect = [build_resp, stream_resp]
                    screen.action_view_logs()
                    self.assertEqual(mock_get.call_count, 2 if expect_file else 1)
            dest = os.path.join(self.tmp_dir, "plain.raw")
            self.assertEqual(os.path.exists(dest), expect_file)
            if expect_file:
                os.remove(dest)
            else:
                self.assertTrue(any("plain http" in str(c) for c in screen.say.call_args_list))
                self.assertTrue(any("remote_insecure" in str(c) for c in screen.say.call_args_list))

    def test_server_relative_download_url_carries_the_token_to_the_server_only(self):
        import hashlib
        mock_app = mock.Mock()
        mock_app.is_running = False
        mock_app.download_dir = self.tmp_dir
        session = self.RemoteSession(app=mock_app)
        session.connected = True
        session.url = "http://cluster.lan:8000"
        session.token = "test-token"
        session.insecure = True
        mock_app.remote_session = session
        screen = self.RemoteScreen()
        screen.say = mock.Mock()
        payload = b"proxied payload"
        with mock.patch.object(self.RemoteScreen, "app", new_callable=mock.PropertyMock, return_value=mock_app):
            screen.active_tab = 1
            screen.remote_builds = [{"id": "bld-1"}]
            screen.selected_indices[1] = 0
            with mock.patch("requests.get") as mock_get:
                build_resp = mock.Mock(status_code=200)
                build_resp.json.return_value = {
                    "id": "bld-1", "status": "completed",
                    "download_urls": {"disk.raw": "/api/v1/builds/bld-1/artifacts/disk.raw"},
                    "artifacts": [{"name": "disk.raw", "size": len(payload),
                                   "sha256": hashlib.sha256(payload).hexdigest()}],
                }
                stream_resp = mock.Mock(status_code=200)
                stream_resp.iter_content.return_value = [payload]
                stream_resp.__enter__ = mock.Mock(return_value=stream_resp)
                stream_resp.__exit__ = mock.Mock(return_value=None)
                mock_get.side_effect = [build_resp, stream_resp]
                screen.action_download_artifact()
        args, kwargs = mock_get.call_args_list[1]
        self.assertEqual(args[0], "http://cluster.lan:8000/api/v1/builds/bld-1/artifacts/disk.raw")
        self.assertEqual(kwargs["headers"], {"Authorization": "Bearer test-token"})
        self.assertFalse(kwargs["allow_redirects"])
        self.assertTrue(os.path.exists(os.path.join(self.tmp_dir, "disk.raw")))

    def test_a_storage_certificate_the_ca_file_lacks_says_which_host_and_what_to_do(self):
        import hashlib
        import requests
        mock_app = mock.Mock()
        mock_app.is_running = False
        mock_app.download_dir = self.tmp_dir
        session = self.RemoteSession(app=mock_app)
        session.connected = True
        session.url = "https://cluster.lan:8000"
        session.token = "test-token"
        mock_app.remote_session = session
        screen = self.RemoteScreen()
        screen.say = mock.Mock()
        with mock.patch.object(self.RemoteScreen, "app", new_callable=mock.PropertyMock, return_value=mock_app):
            screen.active_tab = 1
            screen.remote_builds = [{"id": "bld-1"}]
            screen.selected_indices[1] = 0
            with mock.patch("requests.get") as mock_get:
                build_resp = mock.Mock(status_code=200)
                build_resp.json.return_value = {
                    "id": "bld-1", "status": "completed",
                    "download_urls": {"disk.raw": "https://arti.lan/r/disk.raw"},
                    "artifacts": [{"name": "disk.raw", "size": 3, "sha256": hashlib.sha256(b"abc").hexdigest()}],
                }
                mock_get.side_effect = [build_resp, requests.exceptions.SSLError("self-signed certificate")]
                screen.action_download_artifact()
        said = " ".join(str(c) for c in screen.say.call_args_list)
        self.assertIn("certificate of arti.lan", said)
        self.assertIn("remote_ca_cert", said)

    def test_the_users_own_credential_goes_to_downloads_from_their_storage_endpoint_only(self):
        import hashlib
        payload = b"byot payload"
        for url, header in (("https://arti.lan/r/disk.raw", {"Authorization": "Bearer user-tok"}),
                            ("https://other.example/r/disk.raw", None)):
            mock_app = mock.Mock()
            mock_app.is_running = False
            mock_app.download_dir = self.tmp_dir
            session = self.RemoteSession(app=mock_app)
            session.connected = True
            session.url = "https://cluster.lan:8000"
            session.token = "test-token"
            session.storage = {"type": "artifactory", "endpoint": "https://arti.lan"}
            session.storage_credential = {"token": "user-tok"}
            mock_app.remote_session = session
            self.assertEqual(session.auth_headers["X-Seine-Own-Credential"], "1")
            screen = self.RemoteScreen()
            screen.say = mock.Mock()
            with mock.patch.object(self.RemoteScreen, "app", new_callable=mock.PropertyMock, return_value=mock_app):
                screen.active_tab = 1
                screen.remote_builds = [{"id": "bld-1"}]
                screen.selected_indices[1] = 0
                with mock.patch("requests.get") as mock_get:
                    build_resp = mock.Mock(status_code=200)
                    build_resp.json.return_value = {
                        "id": "bld-1", "status": "completed", "download_urls": {"disk.raw": url},
                        "artifacts": [{"name": "disk.raw", "size": len(payload),
                                       "sha256": hashlib.sha256(payload).hexdigest()}],
                    }
                    stream_resp = mock.Mock(status_code=200)
                    stream_resp.iter_content.return_value = [payload]
                    stream_resp.__enter__ = mock.Mock(return_value=stream_resp)
                    stream_resp.__exit__ = mock.Mock(return_value=None)
                    mock_get.side_effect = [build_resp, stream_resp]
                    screen.action_download_artifact()
                    self.assertEqual(mock_get.call_args_list[1][1]["headers"], header or {})
            dest = os.path.join(self.tmp_dir, "disk.raw")
            if os.path.exists(dest):
                os.remove(dest)

    def test_a_session_without_its_own_credential_says_nothing_extra(self):
        session = self.RemoteSession(app=mock.Mock())
        session.token = "test-token"
        self.assertEqual(session.auth_headers, {"Authorization": "Bearer test-token"})

    def test_download_all_artifacts_flow(self):
        import hashlib
        mock_app = mock.Mock()
        mock_app.is_running = False
        mock_app.download_dir = self.tmp_dir
        session = self.RemoteSession(app=mock_app)
        session.connected = True
        session.url = "http://cluster.lan:8000"
        session.token = "test-token"
        mock_app.remote_session = session

        screen = self.RemoteScreen()
        screen.say = mock.Mock()

        payload1 = b"disk image payload"
        digest1 = hashlib.sha256(payload1).hexdigest()
        payload2 = b"rootfs tar payload"
        digest2 = hashlib.sha256(payload2).hexdigest()

        with mock.patch.object(self.RemoteScreen, "app", new_callable=mock.PropertyMock, return_value=mock_app):
            screen.active_tab = 1
            screen.remote_builds = [{"id": "bld-buildall123"}]
            screen.selected_indices[1] = 0

            with mock.patch("requests.get") as mock_get:
                build_resp = mock.Mock(status_code=200)
                build_resp.json.return_value = {
                    "id": "bld-buildall123",
                    "status": "completed",
                    "download_urls": {
                        "img.raw": "https://s3.lan/img.raw",
                        "rootfs.tar": "https://s3.lan/rootfs.tar",
                    },
                    "artifacts": [
                        {"name": "img.raw", "size": len(payload1), "sha256": digest1},
                        {"name": "rootfs.tar", "size": len(payload2), "sha256": digest2},
                    ],
                }

                stream_resp1 = mock.Mock(status_code=200)
                stream_resp1.iter_content.return_value = [payload1]
                stream_resp1.__enter__ = mock.Mock(return_value=stream_resp1)
                stream_resp1.__exit__ = mock.Mock(return_value=None)

                stream_resp2 = mock.Mock(status_code=200)
                stream_resp2.iter_content.return_value = [payload2]
                stream_resp2.__enter__ = mock.Mock(return_value=stream_resp2)
                stream_resp2.__exit__ = mock.Mock(return_value=None)

                mock_get.side_effect = [build_resp, stream_resp1, stream_resp2]

                screen.action_download_artifact()

                self.assertTrue(os.path.exists(os.path.join(self.tmp_dir, "img.raw")))
                self.assertTrue(os.path.exists(os.path.join(self.tmp_dir, "rootfs.tar")))
                screen.say.assert_called_with(
                    f"downloaded 2 artifact(s) to {self.tmp_dir}",
                    error=False,
                )

    def test_download_corrupt_checksum_handled(self):
        import hashlib
        mock_app = mock.Mock()
        mock_app.is_running = False
        mock_app.download_dir = self.tmp_dir
        session = self.RemoteSession(app=mock_app)
        session.connected = True
        session.url = "http://cluster.lan:8000"
        session.token = "test-token"
        mock_app.remote_session = session

        screen = self.RemoteScreen()
        screen.say = mock.Mock()

        payload = b"bad corrupted content"
        wrong_digest = hashlib.sha256(b"original good content").hexdigest()

        with mock.patch.object(self.RemoteScreen, "app", new_callable=mock.PropertyMock, return_value=mock_app):
            screen.active_tab = 3
            screen.remote_artifacts = [
                {
                    "name": "corrupt.raw",
                    "build_id": "bld-999999",
                }
            ]
            screen.selected_indices[3] = 0

            with mock.patch("requests.get") as mock_get:
                build_resp = mock.Mock(status_code=200)
                build_resp.json.return_value = {
                    "id": "bld-999999",
                    "status": "completed",
                    "download_urls": {"corrupt.raw": "https://s3.lan/corrupt.raw"},
                    "artifacts": [{"name": "corrupt.raw", "size": len(payload), "sha256": wrong_digest}],
                }

                stream_resp = mock.Mock(status_code=200)
                stream_resp.iter_content.return_value = [payload]
                stream_resp.__enter__ = mock.Mock(return_value=stream_resp)
                stream_resp.__exit__ = mock.Mock(return_value=None)

                mock_get.side_effect = [build_resp, stream_resp]

                screen.action_view_logs()

                corrupt_file = os.path.join(self.tmp_dir, "corrupt.raw.corrupt")
                self.assertTrue(os.path.exists(corrupt_file))
                self.assertFalse(os.path.exists(os.path.join(self.tmp_dir, "corrupt.raw")))
                self.assertTrue(any("failed" in str(call) for call in screen.say.call_args_list))

    def test_download_build_not_completed_warning(self):
        mock_app = mock.Mock()
        mock_app.is_running = False
        session = self.RemoteSession(app=mock_app)
        session.connected = True
        session.url = "http://cluster.lan:8000"
        mock_app.remote_session = session

        screen = self.RemoteScreen()
        screen.say = mock.Mock()

        with mock.patch.object(self.RemoteScreen, "app", new_callable=mock.PropertyMock, return_value=mock_app):
            screen.active_tab = 1
            screen.remote_builds = [{"id": "bld-running"}]
            screen.selected_indices[1] = 0

            with mock.patch("requests.get") as mock_get:
                build_resp = mock.Mock(status_code=200)
                build_resp.json.return_value = {
                    "id": "bld-running",
                    "status": "running",
                }
                mock_get.return_value = build_resp

                screen.action_download_artifact()
                screen.say.assert_called_with("build bld-running has no artifacts (status: running)", warning=True)

    def test_body_click_selection(self):
        with _tui_required(self):
            from seine.tui.remote_screen import RemoteBodyStatic
        screen = self.RemoteScreen()
        screen.update_body = mock.Mock()

        # Tab 1 click
        screen.active_tab = 1
        screen.remote_builds = [{"id": "b1"}, {"id": "b2"}, {"id": "b3"}]
        screen.selected_indices[1] = 0

        body_widget = RemoteBodyStatic()
        body_widget._screen = screen

        click_event = mock.Mock()
        click_event.y = 5
        body_widget.on_click(click_event)
        self.assertEqual(screen.selected_indices[1], 1)

        # Tab 3 click
        screen.active_tab = 3
        screen.remote_artifacts = [{"name": "a1"}, {"name": "a2"}]
        screen.selected_indices[3] = 0
        click_event.y = 5
        body_widget.on_click(click_event)
        self.assertEqual(screen.selected_indices[3], 1)


