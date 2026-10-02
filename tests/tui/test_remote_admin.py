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


class RemoteAdminRenderTest(avocado.Test):
    """
    :avocado: tags=tui
    """

    def setUp(self):
        with _tui_required(self):
            from seine.tui.render_remote import (
                render_remote_ops,
                render_remote_projects,
                render_remote_users,
            )
        self.render_remote_users = render_remote_users
        self.render_remote_projects = render_remote_projects
        self.render_remote_ops = render_remote_ops

        self.tmp_dir = tempfile.mkdtemp(prefix="seine-test-admin-render-")
        os.environ["XDG_CONFIG_HOME"] = self.tmp_dir

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_render_users_empty(self):
        rendered = self.render_remote_users([])
        self.assertIn("USER ROSTER", rendered)
        self.assertIn("No users registered.", rendered)
        self.assertIn("[n] New User", rendered)

    def test_render_users_populated_and_selection(self):
        now = time.time()
        users = [
            {"id": "alice", "is_admin": True, "active": True, "created_at": now - 3600},
            {"id": "bob", "is_admin": False, "active": True, "created_at": now - 7200},
            {"id": "charlie", "is_admin": False, "active": False, "created_at": now - 86400},
        ]
        tokens = [
            {"user_id": "alice", "id": "t1"},
            {"user_id": "alice", "id": "t2"},
            {"user_id": "bob", "id": "t3"},
        ]
        rendered0 = self.render_remote_users(users, tokens, selected_index=0)
        self.assertIn(" ▸ ● active", rendered0)
        self.assertIn("   ● active", rendered0)
        self.assertIn("○ disabled", rendered0)
        self.assertIn("alice", rendered0)
        self.assertIn("admin", rendered0)
        self.assertIn("[n] New User", rendered0)
        self.assertIn("[t] Issue PAT", rendered0)

        # token counts are passed and rendered (2 for alice, 1 for bob)
        # both alice's count and bob's count should appear
        self.assertIn("2", rendered0)

        rendered1 = self.render_remote_users(users, tokens, selected_index=1)
        self.assertIn(" ▸ ● active", rendered1)
        self.assertIn("bob", rendered1)

    def test_render_users_no_tokens_arg(self):
        # tokens defaults to None; renders without error
        rendered = self.render_remote_users([{"id": "solo", "is_admin": False, "active": True}])
        self.assertIn("solo", rendered)

    def test_render_projects_empty(self):
        rendered = self.render_remote_projects([])
        self.assertIn("PROJECT INVENTORY", rendered)
        self.assertIn("No projects configured.", rendered)
        self.assertIn("[n] New Project", rendered)

    def test_render_projects_populated_and_selection(self):
        now = time.time()
        projects = [
            {
                "name": "demo",
                "dev_bucket": "seine-demo-dev",
                "prod_bucket": "seine-demo-prod",
                "created_at": now - 86400,
            },
            {
                "name": "alpha",
                "dev_bucket": "seine-alpha-dev",
                "prod_bucket": "seine-alpha-prod",
                "created_at": now,
            },
        ]
        rendered0 = self.render_remote_projects(projects, selected_index=0)
        self.assertIn(" ▸ demo", rendered0)
        self.assertIn("seine-demo-dev", rendered0)
        self.assertIn("seine-demo-prod", rendered0)
        self.assertIn("[n] New Project", rendered0)
        self.assertIn("[m] Manage Members", rendered0)

        rendered1 = self.render_remote_projects(projects, selected_index=1)
        self.assertIn(" ▸ alpha", rendered1)
        self.assertIn("   demo", rendered1)

    def test_render_ops_empty(self):
        rendered = self.render_remote_ops({}, {})
        self.assertIn("SERVER", rendered)
        self.assertIn("BUILD QUEUE METRICS", rendered)
        self.assertIn("FLEET CAPACITY", rendered)

    def test_render_ops_populated(self):
        settings = {
            "url": "http://cluster.lan:8000",
            "user_id": "chombourger",
            "is_admin": True,
            "active_project": "demo",
            "ping_ms": 3.5,
        }
        stats = {
            "queued": 2,
            "running": 1,
            "completed": 40,
            "failed": 3,
            "cancelled": 1,
            "total_builds": 47,
            "workers_online": 2,
            "workers_paused": 1,
            "total_slots": 6,
            "free_disk_gb": 158.3,
        }
        rendered = self.render_remote_ops(settings, stats)
        self.assertIn("cluster.lan:8000", rendered)
        self.assertIn("chombourger", rendered)
        self.assertIn("[admin]", rendered)
        self.assertIn("3.5 ms", rendered)
        self.assertIn("47", rendered)
        self.assertIn("158.3 GB", rendered)


class RemoteAdminModalTest(avocado.Test):
    """
    :avocado: tags=tui
    """

    def setUp(self):
        with _tui_required(self):
            from seine.tui.remote_modals import (
                MemberAssignModal,
                ProjectCreateModal,
                TokenIssueModal,
                UserCreateModal,
            )
        self.UserCreateModal = UserCreateModal
        self.TokenIssueModal = TokenIssueModal
        self.ProjectCreateModal = ProjectCreateModal
        self.MemberAssignModal = MemberAssignModal

        self.tmp_dir = tempfile.mkdtemp(prefix="seine-test-admin-modal-")
        os.environ["XDG_CONFIG_HOME"] = self.tmp_dir

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _make_button_event(self, button_id):
        with _tui_required(self):
            from textual.widgets import Button
        btn = mock.Mock(spec=Button)
        btn.id = button_id
        btn.label = ""
        event = mock.Mock()
        event.button = btn
        return event

    def test_user_create_modal_valid(self):
        modal = self.UserCreateModal()
        dismissed = []
        modal.dismiss = lambda r: dismissed.append(r)

        username_input = mock.Mock()
        username_input.value = "alice"
        modal.query_one = mock.Mock(return_value=username_input)

        modal.on_button_pressed(self._make_button_event("submit"))
        self.assertEqual(len(dismissed), 1)
        self.assertEqual(dismissed[0]["username"], "alice")
        self.assertFalse(dismissed[0]["is_admin"])

    def test_user_create_modal_toggle_role_then_submit(self):
        modal = self.UserCreateModal()
        dismissed = []
        modal.dismiss = lambda r: dismissed.append(r)

        username_input = mock.Mock()
        username_input.value = "superuser"
        modal.query_one = mock.Mock(return_value=username_input)

        # toggle admin role on
        toggle_event = self._make_button_event("toggle-role")
        toggle_event.button.label = "Role: Standard User"
        modal.on_button_pressed(toggle_event)
        self.assertTrue(modal.is_admin)

        modal.on_button_pressed(self._make_button_event("submit"))
        self.assertEqual(dismissed[0]["is_admin"], True)

    def test_user_create_modal_invalid_username(self):
        modal = self.UserCreateModal()
        dismissed = []
        modal.dismiss = lambda r: dismissed.append(r)

        username_input = mock.Mock()
        username_input.value = "bad user!"
        error_static = mock.Mock()
        modal.query_one = mock.Mock(side_effect=lambda sel, cls: username_input if "username" in sel else error_static)

        modal.on_button_pressed(self._make_button_event("submit"))
        self.assertEqual(len(dismissed), 0)
        error_static.update.assert_called_once()

    def test_user_create_modal_cancel(self):
        modal = self.UserCreateModal()
        dismissed = []
        modal.dismiss = lambda r: dismissed.append(r)
        modal.on_button_pressed(self._make_button_event("cancel"))
        self.assertEqual(dismissed, [None])

    def test_token_issue_modal_no_expiry(self):
        modal = self.TokenIssueModal(user_id="alice")
        dismissed = []
        modal.dismiss = lambda r: dismissed.append(r)

        days_input = mock.Mock()
        days_input.value = ""
        modal.query_one = mock.Mock(return_value=days_input)

        modal.on_button_pressed(self._make_button_event("submit"))
        self.assertEqual(dismissed[0], {"days": None})

    def test_token_issue_modal_with_expiry(self):
        modal = self.TokenIssueModal(user_id="bob")
        dismissed = []
        modal.dismiss = lambda r: dismissed.append(r)

        days_input = mock.Mock()
        days_input.value = "30"
        modal.query_one = mock.Mock(return_value=days_input)

        modal.on_button_pressed(self._make_button_event("submit"))
        self.assertEqual(dismissed[0], {"days": 30})

    def test_token_issue_modal_invalid_expiry(self):
        modal = self.TokenIssueModal(user_id="carol")
        dismissed = []
        modal.dismiss = lambda r: dismissed.append(r)

        days_input = mock.Mock()
        days_input.value = "not-a-number"
        error_static = mock.Mock()
        modal.query_one = mock.Mock(side_effect=lambda sel, cls: days_input if "days" in sel else error_static)

        modal.on_button_pressed(self._make_button_event("submit"))
        self.assertEqual(len(dismissed), 0)
        error_static.update.assert_called_once()

    def test_project_create_modal_valid(self):
        modal = self.ProjectCreateModal()
        dismissed = []
        modal.dismiss = lambda r: dismissed.append(r)

        inputs = {"name": "demo", "dev-bucket": "", "prod-bucket": ""}

        def _query(sel, cls):
            key = sel.lstrip("#")
            m = mock.Mock()
            m.value = inputs.get(key, "")
            return m

        error_static = mock.Mock()
        modal.query_one = mock.Mock(side_effect=lambda sel, cls: error_static if "error" in sel else _query(sel, cls))

        modal.on_button_pressed(self._make_button_event("submit"))
        self.assertEqual(len(dismissed), 1)
        self.assertEqual(dismissed[0]["name"], "demo")
        self.assertIsNone(dismissed[0]["dev_bucket"])
        self.assertIsNone(dismissed[0]["prod_bucket"])

    def test_project_create_modal_invalid_name(self):
        modal = self.ProjectCreateModal()
        dismissed = []
        modal.dismiss = lambda r: dismissed.append(r)

        name_input = mock.Mock()
        name_input.value = "Bad Project Name!"
        error_static = mock.Mock()
        modal.query_one = mock.Mock(side_effect=lambda sel, cls: name_input if "name" in sel else error_static)

        modal.on_button_pressed(self._make_button_event("submit"))
        self.assertEqual(len(dismissed), 0)
        error_static.update.assert_called_once()

    def test_member_assign_modal_role_cycle(self):
        modal = self.MemberAssignModal(project_id="demo")
        dismissed = []
        modal.dismiss = lambda r: dismissed.append(r)

        username_input = mock.Mock()
        username_input.value = "dave"
        error_static = mock.Mock()
        toggle_btn = mock.Mock()
        toggle_btn.id = "toggle-role"
        toggle_btn.label = "Role: developer"

        def _query(sel, cls):
            if "username" in sel:
                return username_input
            if "error" in sel:
                return error_static
            return toggle_btn

        modal.query_one = mock.Mock(side_effect=_query)

        # cycle through roles: developer -> releaser -> admin -> developer
        self.assertEqual(modal.ROLES[modal.role_idx], "developer")
        toggle_event = self._make_button_event("toggle-role")
        toggle_event.button = toggle_btn
        modal.on_button_pressed(toggle_event)
        self.assertEqual(modal.ROLES[modal.role_idx], "releaser")
        modal.on_button_pressed(toggle_event)
        self.assertEqual(modal.ROLES[modal.role_idx], "admin")
        modal.on_button_pressed(toggle_event)
        self.assertEqual(modal.ROLES[modal.role_idx], "developer")

        modal.on_button_pressed(self._make_button_event("submit"))
        self.assertEqual(dismissed[0]["user_id"], "dave")
        self.assertEqual(dismissed[0]["role"], "developer")

    def test_member_assign_modal_empty_username(self):
        modal = self.MemberAssignModal(project_id="demo")
        dismissed = []
        modal.dismiss = lambda r: dismissed.append(r)

        username_input = mock.Mock()
        username_input.value = ""
        error_static = mock.Mock()
        modal.query_one = mock.Mock(side_effect=lambda sel, cls: username_input if "username" in sel else error_static)

        modal.on_button_pressed(self._make_button_event("submit"))
        self.assertEqual(len(dismissed), 0)
        error_static.update.assert_called_once()


class RemoteAdminActionTest(avocado.Test):
    """
    :avocado: tags=tui
    """

    def setUp(self):
        with _tui_required(self):
            from seine.tui.remote_screen import RemoteScreen
            from seine.tui.remote_session import RemoteSession
        self.RemoteScreen = RemoteScreen
        self.RemoteSession = RemoteSession

        self.tmp_dir = tempfile.mkdtemp(prefix="seine-test-admin-action-")
        os.environ["XDG_CONFIG_HOME"] = self.tmp_dir

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _make_screen(self, is_admin=True):
        mock_app = mock.Mock()
        mock_app.is_running = False
        session = self.RemoteSession(app=mock_app)
        session.connected = True
        session.is_admin = is_admin
        session.url = "http://cluster.lan:8000"
        session.token = "admin-token"
        mock_app.remote_session = session

        screen = self.RemoteScreen()
        screen.say = mock.Mock()
        screen.fetch_data = mock.Mock()
        return screen, mock_app, session

    def test_require_admin_blocks_non_admin(self):
        screen, mock_app, session = self._make_screen(is_admin=False)
        with mock.patch.object(self.RemoteScreen, "app", new_callable=mock.PropertyMock, return_value=mock_app):
            result = screen._require_admin()
            self.assertFalse(result)
            screen.say.assert_called_with("admin privileges required", warning=True)

    def test_require_admin_blocks_disconnected(self):
        screen, mock_app, session = self._make_screen()
        session.connected = False
        with mock.patch.object(self.RemoteScreen, "app", new_callable=mock.PropertyMock, return_value=mock_app):
            result = screen._require_admin()
            self.assertFalse(result)
            screen.say.assert_called_with("not connected to a remote server", warning=True)

    def test_toggle_admin_sends_patch(self):
        screen, mock_app, session = self._make_screen()
        with mock.patch.object(self.RemoteScreen, "app", new_callable=mock.PropertyMock, return_value=mock_app):
            screen.active_tab = 4
            screen.remote_users = [{"id": "alice", "is_admin": False, "active": True}]
            screen.selected_indices[4] = 0

            with mock.patch("requests.patch") as mock_patch:
                mock_patch.return_value = mock.Mock(status_code=200)
                screen.action_admin_toggle_admin()
                mock_patch.assert_called_once_with(
                    "http://cluster.lan:8000/api/v1/users/alice",
                    json={"is_admin": True},
                    headers={"Authorization": "Bearer admin-token"},
                    timeout=5.0,
                    verify=True,
                )
                screen.say.assert_called_with("admin granted for 'alice'", error=False)

    def test_toggle_admin_revoke(self):
        screen, mock_app, session = self._make_screen()
        with mock.patch.object(self.RemoteScreen, "app", new_callable=mock.PropertyMock, return_value=mock_app):
            screen.active_tab = 4
            screen.remote_users = [{"id": "bob", "is_admin": True, "active": True}]
            screen.selected_indices[4] = 0

            with mock.patch("requests.patch") as mock_patch:
                mock_patch.return_value = mock.Mock(status_code=200)
                screen.action_admin_toggle_admin()
                mock_patch.assert_called_once_with(
                    "http://cluster.lan:8000/api/v1/users/bob",
                    json={"is_admin": False},
                    headers={"Authorization": "Bearer admin-token"},
                    timeout=5.0,
                    verify=True,
                )
                screen.say.assert_called_with("admin revoked for 'bob'", error=False)

    def test_toggle_active_deactivates(self):
        screen, mock_app, session = self._make_screen()
        with mock.patch.object(self.RemoteScreen, "app", new_callable=mock.PropertyMock, return_value=mock_app):
            screen.active_tab = 4
            screen.remote_users = [{"id": "carol", "is_admin": False, "active": True}]
            screen.selected_indices[4] = 0

            with mock.patch("requests.patch") as mock_patch:
                mock_patch.return_value = mock.Mock(status_code=200)
                screen.action_admin_toggle_active()
                mock_patch.assert_called_once_with(
                    "http://cluster.lan:8000/api/v1/users/carol",
                    json={"active": False},
                    headers={"Authorization": "Bearer admin-token"},
                    timeout=5.0,
                    verify=True,
                )
                screen.say.assert_called_with("user 'carol' deactivated", error=False)

    def test_toggle_active_activates(self):
        screen, mock_app, session = self._make_screen()
        with mock.patch.object(self.RemoteScreen, "app", new_callable=mock.PropertyMock, return_value=mock_app):
            screen.active_tab = 4
            screen.remote_users = [{"id": "dave", "is_admin": False, "active": False}]
            screen.selected_indices[4] = 0

            with mock.patch("requests.patch") as mock_patch:
                mock_patch.return_value = mock.Mock(status_code=200)
                screen.action_admin_toggle_active()
                mock_patch.assert_called_once_with(
                    "http://cluster.lan:8000/api/v1/users/dave",
                    json={"active": True},
                    headers={"Authorization": "Bearer admin-token"},
                    timeout=5.0,
                    verify=True,
                )
                screen.say.assert_called_with("user 'dave' activated", error=False)

    def test_toggle_actions_wrong_tab_noop(self):
        screen, mock_app, session = self._make_screen()
        with mock.patch.object(self.RemoteScreen, "app", new_callable=mock.PropertyMock, return_value=mock_app):
            screen.active_tab = 1
            with mock.patch("requests.patch") as mock_patch:
                screen.action_admin_toggle_admin()
                mock_patch.assert_not_called()
                screen.action_admin_toggle_active()
                mock_patch.assert_not_called()

    def test_toggle_actions_no_user_selected(self):
        screen, mock_app, session = self._make_screen()
        with mock.patch.object(self.RemoteScreen, "app", new_callable=mock.PropertyMock, return_value=mock_app):
            screen.active_tab = 4
            screen.remote_users = []
            screen.action_admin_toggle_admin()
            screen.say.assert_called_with("no user selected", warning=True)

    def test_fetch_data_tab4_calls_users_and_tokens(self):
        screen, mock_app, session = self._make_screen()
        screen.update_body = mock.Mock()
        # re-enable fetch_data (was mocked; need real one)
        screen.fetch_data = self.RemoteScreen.fetch_data.__get__(screen, self.RemoteScreen)
        screen.update_body = mock.Mock()

        with mock.patch.object(self.RemoteScreen, "app", new_callable=mock.PropertyMock, return_value=mock_app):
            screen.active_tab = 4
            with mock.patch("requests.get") as mock_get:
                users_resp = mock.Mock(status_code=200)
                users_resp.json.return_value = [{"id": "alice"}, {"id": "bob"}]
                tokens_resp = mock.Mock(status_code=200)
                tokens_resp.json.return_value = [{"id": "t1", "user_id": "alice"}]
                mock_get.side_effect = [users_resp, tokens_resp]

                screen.fetch_data()

                calls = [c[0][0] for c in mock_get.call_args_list]
                self.assertIn("http://cluster.lan:8000/api/v1/users", calls)
                self.assertIn("http://cluster.lan:8000/api/v1/tokens", calls)
                self.assertEqual(len(screen.remote_users), 2)
                self.assertEqual(len(screen.remote_tokens), 1)

    def test_fetch_data_tab5_calls_projects(self):
        screen, mock_app, session = self._make_screen()
        screen.update_body = mock.Mock()
        screen.fetch_data = self.RemoteScreen.fetch_data.__get__(screen, self.RemoteScreen)
        screen.update_body = mock.Mock()

        with mock.patch.object(self.RemoteScreen, "app", new_callable=mock.PropertyMock, return_value=mock_app):
            screen.active_tab = 5
            with mock.patch("requests.get") as mock_get:
                proj_resp = mock.Mock(status_code=200)
                proj_resp.json.return_value = [{"name": "demo"}, {"name": "beta"}]
                mock_get.return_value = proj_resp

                screen.fetch_data()

                mock_get.assert_called_once_with(
                    "http://cluster.lan:8000/api/v1/projects",
                    headers={"Authorization": "Bearer admin-token"},
                    timeout=5.0,
                    verify=True,
                )
                self.assertEqual(len(screen.remote_projects), 2)

    def test_fetch_data_tab6_computes_stats(self):
        screen, mock_app, session = self._make_screen()
        screen.update_body = mock.Mock()
        screen.fetch_data = self.RemoteScreen.fetch_data.__get__(screen, self.RemoteScreen)
        screen.update_body = mock.Mock()

        with mock.patch.object(self.RemoteScreen, "app", new_callable=mock.PropertyMock, return_value=mock_app):
            screen.active_tab = 6
            with mock.patch("requests.get") as mock_get:
                builds_resp = mock.Mock(status_code=200)
                builds_resp.json.return_value = [
                    {"status": "completed"},
                    {"status": "running"},
                    {"status": "queued"},
                ]
                workers_resp = mock.Mock(status_code=200)
                workers_resp.json.return_value = {
                    "workers": [
                        {"status": "online", "concurrency_slots": 4, "free_disk_gb": 100.0},
                        {"status": "paused", "concurrency_slots": 2, "free_disk_gb": 50.0},
                    ]
                }
                mock_get.side_effect = [builds_resp, workers_resp]

                screen.fetch_data()

                stats = screen.remote_ops_stats
                self.assertEqual(stats["total_builds"], 3)
                self.assertEqual(stats["completed"], 1)
                self.assertEqual(stats["running"], 1)
                self.assertEqual(stats["queued"], 1)
                self.assertEqual(stats["workers_online"], 1)
                self.assertEqual(stats["workers_paused"], 1)
                self.assertEqual(stats["total_slots"], 6)
                self.assertAlmostEqual(stats["free_disk_gb"], 100.0)
                self.assertEqual(screen.remote_ops_settings["url"], "cluster.lan")

    def test_cursor_navigation_admin_tabs(self):
        screen, mock_app, session = self._make_screen()
        screen.update_body = mock.Mock()
        with mock.patch.object(self.RemoteScreen, "app", new_callable=mock.PropertyMock, return_value=mock_app):
            screen.active_tab = 4
            screen.remote_users = [{"id": "u1"}, {"id": "u2"}, {"id": "u3"}]
            self.assertEqual(screen.selected_indices[4], 0)

            screen.action_cursor_down()
            self.assertEqual(screen.selected_indices[4], 1)
            screen.action_cursor_down()
            self.assertEqual(screen.selected_indices[4], 2)
            screen.action_cursor_down()
            self.assertEqual(screen.selected_indices[4], 2)

            screen.action_cursor_up()
            self.assertEqual(screen.selected_indices[4], 1)
            screen.action_cursor_up()
            self.assertEqual(screen.selected_indices[4], 0)
            screen.action_cursor_up()
            self.assertEqual(screen.selected_indices[4], 0)

            # tab 5 projects
            screen.active_tab = 5
            screen.remote_projects = [{"name": "p1"}, {"name": "p2"}]
            screen.action_cursor_down()
            self.assertEqual(screen.selected_indices[5], 1)
            screen.action_cursor_up()
            self.assertEqual(screen.selected_indices[5], 0)

    def test_admin_new_wrong_tab_noop(self):
        screen, mock_app, session = self._make_screen()
        with mock.patch.object(self.RemoteScreen, "app", new_callable=mock.PropertyMock, return_value=mock_app):
            screen.active_tab = 1
            # should not open any modal
            with mock.patch.object(mock_app, "push_screen") as mock_push:
                screen.action_admin_new()
                mock_push.assert_not_called()

    def test_admin_manage_members_no_project_selected(self):
        screen, mock_app, session = self._make_screen()
        with mock.patch.object(self.RemoteScreen, "app", new_callable=mock.PropertyMock, return_value=mock_app):
            screen.active_tab = 5
            screen.remote_projects = []
            screen.action_admin_manage_members()
            screen.say.assert_called_with("no project selected", warning=True)

    def test_admin_issue_token_no_user_selected(self):
        screen, mock_app, session = self._make_screen()
        with mock.patch.object(self.RemoteScreen, "app", new_callable=mock.PropertyMock, return_value=mock_app):
            screen.active_tab = 4
            screen.remote_users = []
            screen.action_admin_issue_token()
            screen.say.assert_called_with("no user selected", warning=True)
