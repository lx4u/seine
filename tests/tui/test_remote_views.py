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
            from seine.tui.render_remote import (
                render_remote_builds,
                render_remote_workers,
            )
        self.App = App
        self.SeineApp = SeineApp
        self.RemoteScreen = RemoteScreen
        self.RemoteSession = RemoteSession
        self.render_remote_builds = render_remote_builds
        self.render_remote_workers = render_remote_workers

        self.tmp_dir = tempfile.mkdtemp(prefix="seine-test-remote-views-")
        os.environ["XDG_CONFIG_HOME"] = self.tmp_dir

    def tearDown(self):
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

    def test_log_and_artifact_actions(self):
        mock_app = mock.Mock()
        mock_app.is_running = False
        screen = self.RemoteScreen()
        screen.say = mock.Mock()

        with mock.patch.object(self.RemoteScreen, "app", new_callable=mock.PropertyMock, return_value=mock_app):
            screen.active_tab = 1
            screen.remote_builds = [{"id": "build-abcdef123456"}]
            screen.selected_indices[1] = 0

            screen.action_view_logs()
            screen.say.assert_called_with("streaming logs for build build-abcdef...")

            screen.action_download_artifact()
            screen.say.assert_called_with("downloading artifacts for build build-abcdef...")

    def test_body_click_selection(self):
        with _tui_required(self):
            from seine.tui.remote_screen import RemoteBodyStatic
        screen = self.RemoteScreen()
        screen.update_body = mock.Mock()
        screen.active_tab = 1
        screen.remote_builds = [{"id": "b1"}, {"id": "b2"}, {"id": "b3"}]
        screen.selected_indices[1] = 0

        body_widget = RemoteBodyStatic()
        body_widget._screen = screen

        # Click at y=5 (which corresponds to row index 5 - 4 = 1)
        click_event = mock.Mock()
        click_event.y = 5
        body_widget.on_click(click_event)
        self.assertEqual(screen.selected_indices[1], 1)

