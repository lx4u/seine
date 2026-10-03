# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import asyncio
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
def _tui_required(test):
    try:
        yield
    except ImportError as e:
        test.cancel("the 'tui' extra (textual) is not installed: %s" % e)


class RemoteScreenTest(avocado.Test):
    """
    :avocado: tags=tui
    """

    def setUp(self):
        with _tui_required(self):
            from textual.app import App
            from seine.tui.app import SeineApp, SCREENS
            from seine.tui.remote_screen import RemoteScreen, SidebarStatic
            from seine.tui.remote_session import RemoteSession
        self.App = App
        self.SeineApp = SeineApp
        self.SCREENS = SCREENS
        self.RemoteScreen = RemoteScreen
        self.SidebarStatic = SidebarStatic
        self.RemoteSession = RemoteSession

        self.tmp_dir = tempfile.mkdtemp(prefix="seine-test-remote-screen-")
        os.environ["XDG_CONFIG_HOME"] = self.tmp_dir

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_screen_registered_in_screens(self):
        self.assertIn("remote", self.SCREENS)
        self.assertIs(self.SCREENS["remote"], self.RemoteScreen)

    def test_screen_composition(self):
        mock_app = mock.Mock()
        mock_app.history = mock.Mock()
        mock_app.history.entries.return_value = []
        screen = self.RemoteScreen()
        with mock.patch.object(self.RemoteScreen, "app", new_callable=mock.PropertyMock, return_value=mock_app):
            widgets = list(screen.compose())
            main_horiz = widgets[0]
            child_ids = [getattr(c, "id", None) for c in getattr(main_horiz, "_pending_children", [])]
            self.assertIn("remotemain", child_ids)
            self.assertIn("remoteside", child_ids)
            side = main_horiz._pending_children[child_ids.index("remoteside")]
            side_ids = [getattr(c, "id", None) for c in side._pending_children]
            self.assertEqual(side_ids, ["remotedetailpane", "remotesidebar"])

    def test_detail_follows_the_cursor(self):
        mock_app = mock.Mock()
        session = self.RemoteSession(app=mock_app)
        mock_app.remote_session = session
        screen = self.RemoteScreen()
        screen.remote_workers = [{"id": "w-one", "hostname": "h1"}, {"id": "w-two", "hostname": "h2"}]
        screen.active_tab = 2
        with mock.patch.object(self.RemoteScreen, "app", new_callable=mock.PropertyMock, return_value=mock_app):
            self.assertIn("No active server connection", screen._render_detail())
            session.connected = True
            self.assertIn("w-one", screen._render_detail())
            screen.action_cursor_down()
            self.assertIn("w-two", screen._render_detail())
            screen.remote_workers = []
            self.assertIn("Select an item", screen._render_detail())

    def test_sidebar_rendering_disconnected(self):
        mock_app = mock.Mock()
        session = self.RemoteSession(app=mock_app)
        mock_app.remote_session = session

        screen = self.RemoteScreen()
        with mock.patch.object(self.RemoteScreen, "app", new_callable=mock.PropertyMock, return_value=mock_app):
            sidebar = screen._render_sidebar()
            self.assertIn("Status: Disconnected", sidebar)
            self.assertIn("[^1] Builds", sidebar)
            self.assertIn("[^2] Workers", sidebar)
            self.assertIn("[^3] Artifacts", sidebar)
            self.assertNotIn("SUPER-POWERS", sidebar)
            self.assertNotIn("[^4] Users", sidebar)

    def test_sidebar_says_when_no_project_is_chosen(self):
        mock_app = mock.Mock()
        session = self.RemoteSession(app=mock_app)
        session.connected = True
        session.url = "http://cluster.lan:8000"
        mock_app.remote_session = session
        screen = self.RemoteScreen()
        with mock.patch.object(self.RemoteScreen, "app", new_callable=mock.PropertyMock, return_value=mock_app):
            self.assertIn("Project: none", screen._render_sidebar())

    def test_tabs_use_ctrl_digits_not_bare_digits(self):
        keys = {b.key: b.action for b in self.RemoteScreen.BINDINGS}
        for n in range(1, 7):
            self.assertEqual(keys[f"ctrl+{n}"], f"select_tab({n})")
            self.assertNotIn(str(n), keys)

    def test_ctrl_digit_switches_tab_while_typing_in_the_prompt(self):
        from seine.tui.remote_screen import RemoteScreen
        env = {"SEINE_HISTORY_FILE": os.path.join(self.tmp_dir, "history.json"),
               "SEINE_CACHE_DIR": self.tmp_dir}

        async def scenario():
            app = self.SeineApp()
            app.remote_session.connected = True
            app.remote_session.url = "https://cluster.lan:8443"
            with mock.patch.object(RemoteScreen, "fetch_data"):
                async with app.run_test() as pilot:
                    await pilot.pause()
                    app.show("remote")
                    await pilot.pause()
                    self.assertEqual(app.focused.id, "prompt")
                    await pilot.press("ctrl+2")
                    self.assertEqual(app.screen.active_tab, 2)
                    await pilot.press("ctrl+3")
                    self.assertEqual(app.screen.active_tab, 3)
                    await pilot.press("3")
                    self.assertEqual(app.screen.active_tab, 3)
                    self.assertEqual(app.query_one("#prompt").value, "3")

        with mock.patch.dict(os.environ, env):
            asyncio.run(scenario())

    def test_status_bar_shows_download_progress_and_a_click_opens_the_artifacts_tab(self):
        from seine.tui.remote_screen import RemoteScreen
        env = {"SEINE_HISTORY_FILE": os.path.join(self.tmp_dir, "history.json"),
               "SEINE_CACHE_DIR": self.tmp_dir}

        async def scenario():
            app = self.SeineApp()
            app.remote_session.connected = True
            app.remote_session.url = "https://cluster.lan:8443"
            with mock.patch.object(RemoteScreen, "fetch_data"):
                async with app.run_test() as pilot:
                    await pilot.pause()
                    chip = app.screen.query_one("#download-indicator")
                    self.assertFalse(chip.display)

                    state = app.download_state
                    state.queue("bld-1", "disk.raw", 200)
                    state.start("bld-1", "disk.raw")
                    state.advance("bld-1", "disk.raw", 46)
                    app.refresh_indicators()
                    await pilot.pause()
                    self.assertTrue(chip.display)
                    self.assertEqual(str(chip.renderable), "downloading 23%")

                    await pilot.click("#download-indicator")
                    await pilot.pause()
                    self.assertIsInstance(app.screen, RemoteScreen)
                    self.assertEqual(app.screen.active_tab, 3)

                    state.finish("bld-1", "disk.raw")
                    app.refresh_indicators()
                    self.assertFalse(app.screen.query_one("#download-indicator").display)

        with mock.patch.dict(os.environ, env):
            asyncio.run(scenario())

    def test_download_of_an_expired_artifact_warns_without_a_request(self):
        mock_app = mock.Mock()
        screen = self.RemoteScreen()
        screen.say = mock.Mock()
        screen.active_tab = 3
        screen.remote_artifacts = [{"name": "disk.img", "build_id": "b" * 32, "expired": True,
                                    "expired_reason": "pressure"}]
        screen._trigger_download = mock.Mock()
        with mock.patch.object(self.RemoteScreen, "app", new_callable=mock.PropertyMock, return_value=mock_app):
            screen.action_download_artifact()
            screen.action_view_logs()
        screen._trigger_download.assert_not_called()
        self.assertEqual(screen.say.call_count, 2)
        screen.say.assert_called_with(
            f"artifacts of build {'b' * 12} expired (storage pressure); rebuild to get them again",
            warning=True,
        )

    def test_screen_keeps_its_app_for_workers_that_outlive_it(self):
        from seine.tui.remote_screen import RemoteScreen
        screen = RemoteScreen()
        fake_app = mock.Mock()
        screen._app_ref = fake_app
        self.assertIs(screen.app, fake_app)

    def test_say_on_a_detached_screen_goes_to_the_app(self):
        from seine.tui.base import BaseScreen
        from seine.tui.remote_screen import RemoteScreen
        from textual.css.query import NoMatches
        screen = RemoteScreen()
        screen._app_ref = fake_app = mock.Mock()
        with mock.patch.object(BaseScreen, "say", side_effect=NoMatches("#status")):
            screen.say("downloaded 1 artifact(s)", error=False)
        fake_app.say.assert_called_once_with("downloaded 1 artifact(s)", error=False, warning=False)

    def test_a_download_worker_can_report_after_another_screen_took_over(self):
        from seine.tui.remote_screen import RemoteScreen
        env = {"SEINE_HISTORY_FILE": os.path.join(self.tmp_dir, "history.json"),
               "SEINE_CACHE_DIR": self.tmp_dir}

        async def scenario():
            app = self.SeineApp()
            app.remote_session.connected = True
            app.remote_session.url = "https://cluster.lan:8443"
            with mock.patch.object(RemoteScreen, "fetch_data"):
                async with app.run_test() as pilot:
                    await pilot.pause()
                    app.show("remote")
                    await pilot.pause()
                    old = app.screen
                    self.assertIsInstance(old, RemoteScreen)
                    old.app  # what the real worker has done before the switch
                    app.show("overview")
                    await pilot.pause()
                    self.assertIsNot(app.screen, old)
                    old._parent = None  # Textual does this when it removes a screen
                    # Like a Textual worker: a bare executor, no copied context.
                    loop = asyncio.get_running_loop()
                    await loop.run_in_executor(None, old._download_changed)
                    await loop.run_in_executor(None, old._notify_say, "downloaded", False, False)
                    await pilot.pause()

        with mock.patch.dict(os.environ, env):
            asyncio.run(scenario())

    def test_s_switches_project_and_p_still_pauses_a_worker(self):
        keys = {b.key: b.action for b in self.RemoteScreen.BINDINGS}
        self.assertEqual(keys["s"], "switch_project")
        self.assertEqual(keys["p"], "toggle_worker_pause")

    def test_switch_project_runs_the_project_command(self):
        mock_app = mock.Mock()
        screen = self.RemoteScreen()
        screen.say = mock.Mock()
        with mock.patch.object(self.RemoteScreen, "app", new_callable=mock.PropertyMock, return_value=mock_app):
            with mock.patch("seine.tui.commands.dispatch") as dispatch:
                screen.action_switch_project()
                dispatch.assert_called_once_with(mock_app, "/project")

                from seine.tui.commands import CommandError
                dispatch.side_effect = CommandError("not connected")
                screen.action_switch_project()
        screen.say.assert_called_once_with("not connected", warning=True)

    def test_server_host_drops_scheme_port_and_path(self):
        from seine.tui.remote_screen import server_host
        for url, host in (
            ("https://cluster.lan:8443", "cluster.lan"),
            ("http://192.168.1.111:8000/", "192.168.1.111"),
            ("https://[fd00::1]:8443/x", "fd00::1"),
            ("cluster", "cluster"),
        ):
            self.assertEqual(server_host(url), host)

    def test_sidebar_rendering_user(self):
        mock_app = mock.Mock()
        session = self.RemoteSession(app=mock_app)
        session.connected = True
        session.url = "http://cluster.lan:8000"
        session.user_id = "bob"
        session.is_admin = False
        session.active_project = "my-distro"
        mock_app.remote_session = session

        screen = self.RemoteScreen()
        with mock.patch.object(self.RemoteScreen, "app", new_callable=mock.PropertyMock, return_value=mock_app):
            sidebar = screen._render_sidebar()
            self.assertIn("Server:  cluster.lan\n", sidebar)
            self.assertNotIn("8000", sidebar)
            self.assertNotIn("http", sidebar)
            self.assertIn("User:    bob", sidebar)
            self.assertNotIn("[admin]", sidebar)
            self.assertIn("Project: my-distro", sidebar)
            self.assertIn("[^1] Builds", sidebar)
            self.assertIn("[^2] Workers", sidebar)
            self.assertIn("[^3] Artifacts", sidebar)
            self.assertNotIn("SUPER-POWERS", sidebar)
            self.assertNotIn("[^4] Users", sidebar)

    def test_sidebar_rendering_admin(self):
        mock_app = mock.Mock()
        session = self.RemoteSession(app=mock_app)
        session.connected = True
        session.url = "http://cluster.lan:8000"
        session.user_id = "alice"
        session.is_admin = True
        session.active_project = "infra-core"
        mock_app.remote_session = session

        screen = self.RemoteScreen()
        with mock.patch.object(self.RemoteScreen, "app", new_callable=mock.PropertyMock, return_value=mock_app):
            sidebar = screen._render_sidebar()
            self.assertIn("User:    alice [admin]", sidebar)
            self.assertIn("SUPER-POWERS", sidebar)
            self.assertIn("[^4] Users", sidebar)
            self.assertIn("[^5] Projects", sidebar)
            self.assertIn("[^6] Server / Ops", sidebar)

    def test_tab_selection_standard_and_admin_guard(self):
        mock_app = mock.Mock()
        mock_app.say = mock.Mock()
        session = self.RemoteSession(app=mock_app)
        session.connected = True
        session.is_admin = False
        mock_app.remote_session = session

        screen = self.RemoteScreen()
        screen.say = mock.Mock()
        with mock.patch.object(self.RemoteScreen, "app", new_callable=mock.PropertyMock, return_value=mock_app):
            screen.update_body = mock.Mock()
            self.assertEqual(screen.active_tab, 1)

            screen.action_select_tab(2)
            self.assertEqual(screen.active_tab, 2)

            screen.action_select_tab(3)
            self.assertEqual(screen.active_tab, 3)

            screen.action_select_tab(4)
            self.assertEqual(screen.active_tab, 3)
            screen.say.assert_called_with("admin privileges required for tab 4", warning=True)

            session.is_admin = True
            screen.action_select_tab(4)
            self.assertEqual(screen.active_tab, 4)

            screen.action_select_tab(5)
            self.assertEqual(screen.active_tab, 5)

            screen.action_select_tab(6)
            self.assertEqual(screen.active_tab, 6)

    def test_main_pane_rendering_content(self):
        mock_app = mock.Mock()
        session = self.RemoteSession(app=mock_app)
        mock_app.remote_session = session

        screen = self.RemoteScreen()
        with mock.patch.object(self.RemoteScreen, "app", new_callable=mock.PropertyMock, return_value=mock_app):
            text = screen._render_main()
            self.assertIn("No active remote server connection", text)

            session.connected = True
            session.url = "http://cluster:8000"
            session.active_project = "demo"
            screen.active_tab = 2

            text = screen._render_main()
            self.assertIn("REMOTE WORKERS", text)
            self.assertIn("No remote workers registered", text)

            screen.active_tab = 3
            text = screen._render_main()
            self.assertIn("REMOTE ARTIFACTS", text)
            self.assertIn("No artifacts found in remote builds", text)

            screen.active_tab = 4
            text = screen._render_main()
            self.assertIn("USER ROSTER", text)

            screen.active_tab = 7
            text = screen._render_main()
            self.assertIn("Active view: Unknown", text)
            self.assertIn("Target cluster: cluster\n", text)

    def test_sidebar_click_action(self):
        screen = self.RemoteScreen()
        screen.action_select_tab = mock.Mock()
        sidebar_widget = self.SidebarStatic()
        sidebar_widget._screen = screen

        sidebar_widget.update("\n".join([
            " NAVIGATION",
            "   [^1] Builds",
            "   [^2] Workers",
            "   [^3] Artifacts",
        ]))

        click_event = mock.Mock()
        click_event.y = 2
        sidebar_widget.on_click(click_event)
        screen.action_select_tab.assert_called_once_with(2)

    def test_screen_in_pilot(self):
        app = self.SeineApp()

        async def scenario():
            async with app.run_test() as pilot:
                app.show("remote")
                await pilot.pause()
                self.assertIsInstance(app.screen, self.RemoteScreen)
                remotemain = app.screen.query_one("#remotemain")
                remotesidebar = app.screen.query_one("#remotesidebar")
                self.assertIsNotNone(remotemain)
                self.assertIsNotNone(remotesidebar)
                self.assertIsNotNone(app.screen.query_one("#remotedetail"))

                # Focus a remote pane so numeric hotkeys are handled by the screen.
                remotemain.focus()
                await pilot.press("ctrl+2")
                self.assertEqual(app.screen.active_tab, 2)
                await pilot.press("ctrl+3")
                self.assertEqual(app.screen.active_tab, 3)
                await pilot.press("ctrl+1")
                self.assertEqual(app.screen.active_tab, 1)

        asyncio.run(scenario())
