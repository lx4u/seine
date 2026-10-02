# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import asyncio
import contextlib
import os
import sys
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


class ShortcutsTest(avocado.Test):
    """
    :avocado: tags=tui
    """

    def setUp(self):
        with _tui_required(self):
            from textual.app import App
            from textual.widgets import Static
            from seine.tui.base import BaseScreen, Prompt, PathCompletions
            from seine.tui.history import History
            from seine.tui import commands
        self.App = App
        self.Static = Static
        self.BaseScreen = BaseScreen
        self.Prompt = Prompt
        self.PathCompletions = PathCompletions
        self.History = History
        self.commands = commands

    def test_bindings_registered(self):
        binding_map = {b.key: (b.action, b.description) for b in self.BaseScreen.BINDINGS}
        self.assertEqual(binding_map.get("ctrl+b"), ("trigger_build", "Build"))
        self.assertEqual(binding_map.get("ctrl+r"), ("toggle_remote", "Remote"))
        self.assertEqual(binding_map.get("ctrl+t"), ("run_tests", "Test"))
        self.assertEqual(binding_map.get("ctrl+o"), ("show_overview", "Overview"))
        self.assertEqual(binding_map.get("ctrl+q"), ("quit_app", "Quit"))

    def test_hint_chips_updated(self):
        keys = [key for key, _ in self.BaseScreen.HINT_CHIPS]
        self.assertIn("build", keys)
        self.assertIn("remote", keys)

        screen = self.BaseScreen()
        self.assertIn("^B build", screen.HINT)
        self.assertIn("^R remote", screen.HINT)

    def test_action_trigger_build(self):
        screen = self.BaseScreen()
        mock_app = mock.Mock()
        with mock.patch.object(self.BaseScreen, "app", new_callable=mock.PropertyMock, return_value=mock_app):
            with mock.patch("seine.tui.commands.dispatch") as mock_dispatch:
                screen.action_trigger_build()
                mock_dispatch.assert_called_once_with(mock_app, "/build")

    def test_action_trigger_build_error_handled(self):
        screen = self.BaseScreen()
        screen.say = mock.Mock()
        mock_app = mock.Mock()
        with mock.patch.object(self.BaseScreen, "app", new_callable=mock.PropertyMock, return_value=mock_app):
            with mock.patch("seine.tui.commands.dispatch", side_effect=self.commands.CommandError("no spec")):
                screen.action_trigger_build()
                screen.say.assert_called_once_with("no spec", error=True)

    def test_action_toggle_remote_disconnected(self):
        screen = self.BaseScreen()
        mock_app = mock.Mock(remote_session=None)
        with mock.patch.object(self.BaseScreen, "app", new_callable=mock.PropertyMock, return_value=mock_app):
            with mock.patch("seine.tui.commands.dispatch") as mock_dispatch:
                screen.action_toggle_remote()
                mock_dispatch.assert_called_once_with(mock_app, "/remote")

    def test_action_toggle_remote_connected(self):
        screen = self.BaseScreen()
        mock_app = mock.Mock(remote_session=mock.Mock(connected=True))
        with mock.patch.object(self.BaseScreen, "app", new_callable=mock.PropertyMock, return_value=mock_app):
            with mock.patch("seine.tui.commands.dispatch") as mock_dispatch:
                screen.action_toggle_remote()
                mock_dispatch.assert_called_once_with(mock_app, "/remote disconnect")

    def test_action_toggle_remote_error_handled(self):
        screen = self.BaseScreen()
        screen.say = mock.Mock()
        mock_app = mock.Mock(remote_session=None)
        with mock.patch.object(self.BaseScreen, "app", new_callable=mock.PropertyMock, return_value=mock_app):
            with mock.patch("seine.tui.commands.dispatch", side_effect=self.commands.CommandError("unknown command")):
                screen.action_toggle_remote()
                screen.say.assert_called_once_with("unknown command", error=True)

    def test_action_run_tests(self):
        screen = self.BaseScreen()
        mock_app = mock.Mock()
        with mock.patch.object(self.BaseScreen, "app", new_callable=mock.PropertyMock, return_value=mock_app):
            with mock.patch("seine.tui.commands.dispatch") as mock_dispatch:
                screen.action_run_tests()
                mock_dispatch.assert_called_once_with(mock_app, "/test")

    def test_action_run_tests_error_handled(self):
        screen = self.BaseScreen()
        screen.say = mock.Mock()
        mock_app = mock.Mock()
        with mock.patch.object(self.BaseScreen, "app", new_callable=mock.PropertyMock, return_value=mock_app):
            with mock.patch("seine.tui.commands.dispatch", side_effect=self.commands.CommandError("fail")):
                screen.action_run_tests()
                screen.say.assert_called_once_with("fail", error=True)

    def test_action_show_overview(self):
        screen = self.BaseScreen()
        mock_app = mock.Mock()
        with mock.patch.object(self.BaseScreen, "app", new_callable=mock.PropertyMock, return_value=mock_app):
            screen.action_show_overview()
            mock_app.show.assert_called_once_with("overview")

    def test_action_quit_app(self):
        screen = self.BaseScreen()
        mock_app = mock.Mock()
        with mock.patch.object(self.BaseScreen, "app", new_callable=mock.PropertyMock, return_value=mock_app):
            screen.action_quit_app()
            mock_app.exit.assert_called_once_with()

    def test_keypress_dispatch_in_pilot(self):
        dispatched = []

        def fake_dispatch(app, cmd):
            dispatched.append(cmd)

        BaseScreen = self.BaseScreen
        Static = self.Static
        PathCompletions = self.PathCompletions
        Prompt = self.Prompt
        History = self.History

        class DummyScreen(BaseScreen):
            def compose(self):
                yield Static("dummy", id="status")
                completions = PathCompletions()
                yield completions
                yield Prompt(History(None), completions, {"commands"}, id="prompt")

        class DummyApp(self.App):
            def on_mount(self):
                self.push_screen(DummyScreen())

            def show(self, name):
                dispatched.append("show:" + name)

        async def scenario():
            app = DummyApp()
            with mock.patch("seine.tui.commands.dispatch", side_effect=fake_dispatch):
                async with app.run_test() as pilot:
                    await pilot.press("ctrl+b")
                    await pilot.press("ctrl+r")
                    await pilot.press("ctrl+t")
                    await pilot.press("ctrl+o")

        asyncio.run(scenario())
        self.assertEqual(dispatched, ["/build", "/remote", "/test", "show:overview"])
