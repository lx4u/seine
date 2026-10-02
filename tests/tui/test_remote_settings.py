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


class RemoteSettingsTest(avocado.Test):
    """
    :avocado: tags=tui
    """

    def setUp(self):
        with _tui_required(self):
            from textual.app import App
            from seine import settings
            from seine.tui import commands
            from seine.tui.render import render_settings
            from seine.tui.settings import GeneralSettings, SettingsScreen
        self.App = App
        self.settings = settings
        self.commands = commands
        self.render_settings = render_settings
        self.GeneralSettings = GeneralSettings
        self.SettingsScreen = SettingsScreen

        self.tmp_dir = tempfile.mkdtemp(prefix="seine-test-remote-settings-")
        os.environ["XDG_CONFIG_HOME"] = self.tmp_dir

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_defaults_contain_remote_keys(self):
        self.assertIn("default_remote", self.settings.DEFAULTS)
        self.assertIn("auto_connect_remote", self.settings.DEFAULTS)
        self.assertIsNone(self.settings.DEFAULTS["default_remote"])
        self.assertFalse(self.settings.DEFAULTS["auto_connect_remote"])

    def test_load_and_save_roundtrip(self):
        current = self.settings.load()
        self.assertIsNone(current["default_remote"])
        self.assertFalse(current["auto_connect_remote"])

        current["default_remote"] = "http://192.168.1.111:8000"
        current["auto_connect_remote"] = True
        self.settings.save(current)

        reloaded = self.settings.load()
        self.assertEqual(reloaded["default_remote"], "http://192.168.1.111:8000")
        self.assertTrue(reloaded["auto_connect_remote"])

    def test_render_settings_shows_default_remote(self):
        text = self.render_settings()
        self.assertIn("default_remote   (unset)", text)

        current = self.settings.load()
        current["default_remote"] = "http://192.168.1.111:8000"
        self.settings.save(current)

        text = self.render_settings()
        self.assertIn("default_remote   http://192.168.1.111:8000", text)

    def test_set_command_default_remote(self):
        mock_app = mock.Mock()
        self.commands.dispatch(mock_app, "/set default_remote http://192.168.1.111:8000")
        self.assertEqual(self.settings.load()["default_remote"], "http://192.168.1.111:8000")
        mock_app.say.assert_called_once_with("default_remote = http://192.168.1.111:8000")

    def test_set_command_auto_connect_remote(self):
        mock_app = mock.Mock()
        self.commands.dispatch(mock_app, "/set auto_connect_remote true")
        self.assertTrue(self.settings.load()["auto_connect_remote"])

        self.commands.dispatch(mock_app, "/set auto_connect_remote false")
        self.assertFalse(self.settings.load()["auto_connect_remote"])

        with self.assertRaises(self.commands.CommandError):
            self.commands.dispatch(mock_app, "/set auto_connect_remote not-a-bool")

    def test_defaults_contain_tls_keys(self):
        self.assertFalse(self.settings.DEFAULTS["remote_insecure"])
        self.assertIsNone(self.settings.DEFAULTS["remote_ca_cert"])

    def test_render_settings_shows_tls_keys(self):
        text = self.render_settings()
        self.assertIn("remote_insecure  off (default)", text)
        self.assertIn("remote_ca_cert   (unset)", text)
        current = self.settings.load()
        current["remote_insecure"] = True
        current["remote_ca_cert"] = "/etc/seine/ca.pem"
        self.settings.save(current)
        text = self.render_settings()
        self.assertIn("remote_insecure  on", text)
        self.assertIn("remote_ca_cert   /etc/seine/ca.pem", text)

    def test_set_command_remote_insecure(self):
        mock_app = mock.Mock()
        self.commands.dispatch(mock_app, "/set remote_insecure on")
        self.assertTrue(self.settings.load()["remote_insecure"])
        self.commands.dispatch(mock_app, "/set remote_insecure false")
        self.assertFalse(self.settings.load()["remote_insecure"])
        with self.assertRaises(self.commands.CommandError):
            self.commands.dispatch(mock_app, "/set remote_insecure maybe")

    def test_set_command_remote_ca_cert(self):
        mock_app = mock.Mock()
        cert = os.path.join(self.tmp_dir, "ca.pem")
        with open(cert, "w") as f:
            f.write("pem")
        self.commands.dispatch(mock_app, "/set remote_ca_cert %s" % cert)
        self.assertEqual(self.settings.load()["remote_ca_cert"], cert)
        self.commands.dispatch(mock_app, "/set remote_ca_cert none")
        self.assertIsNone(self.settings.load()["remote_ca_cert"])
        with self.assertRaises(self.commands.CommandError):
            self.commands.dispatch(mock_app, "/set remote_ca_cert /no/such/ca.pem")
        self.assertIsNone(self.settings.load()["remote_ca_cert"])

    def test_settings_screen_validates_and_clears_tls_keys(self):
        screen = self.SettingsScreen()
        keys = self.GeneralSettings.KEYS
        cert = os.path.join(self.tmp_dir, "ca.pem")
        with open(cert, "w") as f:
            f.write("pem")
        with mock.patch.object(screen, "_redraw"), \
                mock.patch.object(screen, "_edit_error") as error, \
                mock.patch.object(screen, "query_one") as mock_query:
            general = mock_query.return_value
            general.key_at.return_value = "remote_insecure"
            screen._commit_general(keys.index("remote_insecure"), "true")
            self.assertTrue(self.settings.load()["remote_insecure"])
            screen._commit_general(keys.index("remote_insecure"), "maybe")
            error.assert_called_once()
            self.assertTrue(self.settings.load()["remote_insecure"])
            screen._clear_general(keys.index("remote_insecure"))
            self.assertIs(self.settings.load()["remote_insecure"], False)

            general.key_at.return_value = "remote_ca_cert"
            screen._commit_general(keys.index("remote_ca_cert"), cert)
            self.assertEqual(self.settings.load()["remote_ca_cert"], cert)
            screen._commit_general(keys.index("remote_ca_cert"), "/no/such.pem")
            self.assertEqual(error.call_count, 2)
            self.assertEqual(self.settings.load()["remote_ca_cert"], cert)

    def test_general_settings_keys_contains_default_remote(self):
        self.assertIn("default_remote", self.GeneralSettings.KEYS)
        index = self.GeneralSettings.KEYS.index("default_remote")
        option_list = self.GeneralSettings()
        self.assertEqual(option_list.key_at(index), "default_remote")

    def test_settings_screen_commit_and_clear_remote(self):
        screen = self.SettingsScreen()
        index = self.GeneralSettings.KEYS.index("default_remote")

        with mock.patch.object(screen, "_redraw"):
            with mock.patch.object(screen, "query_one") as mock_query:
                mock_general = mock.Mock()
                mock_general.key_at.return_value = "default_remote"
                mock_query.return_value = mock_general

                screen._commit_general(index, "http://10.0.0.1:8000")
                self.assertEqual(self.settings.load()["default_remote"], "http://10.0.0.1:8000")

                screen._clear_general(index)
                self.assertIsNone(self.settings.load()["default_remote"])

    def test_settings_screen_in_pilot(self):
        class DummyApp(self.App):
            def __init__(self, screen):
                super().__init__()
                self._test_screen = screen

            def on_mount(self):
                self.push_screen(self._test_screen)

        screen = self.SettingsScreen()
        app = DummyApp(screen)

        async def scenario():
            async with app.run_test() as pilot:
                general = screen.query_one(self.GeneralSettings)
                self.assertTrue(general.has_focus)
                idx = self.GeneralSettings.KEYS.index("default_remote")
                general.highlighted = idx
                await pilot.press("enter")

                editrow = screen.query_one("#editrow")
                self.assertTrue(editrow.display)
                editrow.value = "https://seine.corp.lan"
                await pilot.press("enter")

                self.assertEqual(self.settings.load()["default_remote"], "https://seine.corp.lan")
                general = screen.query_one(self.GeneralSettings)
                self.assertTrue(general.has_focus)

                await pilot.press("delete")
                self.assertIsNone(self.settings.load()["default_remote"])

        asyncio.run(scenario())
