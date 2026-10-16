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
        test.cancel(f"the 'tui' extra (textual) is not installed: {e}")

def _run(coro):
    asyncio.run(coro())

def _content(widget):
    renderable = widget.render()
    return renderable.text if hasattr(renderable, "text") else str(renderable)


class PayloadServerIndicatorTest(avocado.Test):
    """
    :avocado: tags=tui
    """

    def setUp(self):
        with _tui_required(self):
            from textual.app import App
            from seine.tui import commands
            from seine.tui.app import SeineApp
            from seine.tui.base import BaseScreen, PayloadServerIndicator
            from seine.tui.updates_screen import UpdatesScreen
        self.App = App
        self.commands = commands
        self.SeineApp = SeineApp
        self.BaseScreen = BaseScreen
        self.PayloadServerIndicator = PayloadServerIndicator
        self.UpdatesScreen = UpdatesScreen

        self.tmp_dir = tempfile.mkdtemp(prefix="seine-test-payload-server-", dir="/var/tmp")

    def tearDown(self):
        if hasattr(self, "tmp_dir") and os.path.isdir(self.tmp_dir):
            shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_indicator_default_hidden(self):
        indicator = self.PayloadServerIndicator()
        self.assertFalse(indicator.display)

    def test_indicator_refresh_when_no_server(self):
        indicator = self.PayloadServerIndicator()
        mock_app = mock.Mock()
        mock_app._payload_server = None
        with mock.patch.object(self.PayloadServerIndicator, "app", new_callable=mock.PropertyMock, return_value=mock_app):
            indicator.refresh_text()
            self.assertFalse(indicator.display)

    def test_indicator_refresh_when_server_stopped(self):
        indicator = self.PayloadServerIndicator()
        mock_app = mock.Mock()
        mock_app._payload_server = mock.Mock(running=False)
        with mock.patch.object(self.PayloadServerIndicator, "app", new_callable=mock.PropertyMock, return_value=mock_app):
            indicator.refresh_text()
            self.assertFalse(indicator.display)

    def test_indicator_refresh_when_server_running(self):
        indicator = self.PayloadServerIndicator()
        mock_app = mock.Mock()
        mock_app._payload_server = mock.Mock(running=True, port=8000)
        with mock.patch.object(self.PayloadServerIndicator, "app", new_callable=mock.PropertyMock, return_value=mock_app):
            indicator.refresh_text()
            self.assertTrue(indicator.display)
            self.assertEqual(_content(indicator), "[serve: 8000]")

    def test_indicator_click_navigates_and_reports(self):
        indicator = self.PayloadServerIndicator()
        mock_app = mock.Mock()
        mock_app.SCREENS = {"updates": mock.Mock()}
        mock_app._payload_server = mock.Mock(running=True, directory="/var/tmp/payload", port=8080)
        mock_app.call_after_refresh.side_effect = lambda fn: fn()
        with mock.patch.object(self.PayloadServerIndicator, "app", new_callable=mock.PropertyMock, return_value=mock_app):
            indicator.on_click(mock.Mock())
            mock_app.show.assert_called_once_with("updates")
            mock_app.say.assert_called_once_with("serving /var/tmp/payload on port 8080")

    def test_indicator_click_when_server_stopped(self):
        indicator = self.PayloadServerIndicator()
        mock_app = mock.Mock()
        mock_app.SCREENS = {"updates": mock.Mock()}
        mock_app._payload_server = None
        with mock.patch.object(self.PayloadServerIndicator, "app", new_callable=mock.PropertyMock, return_value=mock_app):
            indicator.on_click(mock.Mock())
            mock_app.show.assert_not_called()
            mock_app.say.assert_not_called()

    def test_base_screen_footer_mounts_indicator(self):
        mock_app = mock.Mock()
        mock_app.history = mock.Mock()
        mock_app.history.entries.return_value = []
        screen = self.BaseScreen()
        with mock.patch.object(self.BaseScreen, "app", new_callable=mock.PropertyMock, return_value=mock_app):
            footer_widgets = list(screen.footer())
            infobar = next(w for w in footer_widgets if getattr(w, "id", None) == "infobar")
            child_ids = [getattr(c, "id", None) for c in getattr(infobar, "_pending_children", [])]
            self.assertIn("payload-server-indicator", child_ids)

    def test_serve_command_updates_indicator_live(self):
        spec_path = os.path.join(path_to_sources, "examples", "pc-ostree-update-image", "main.yaml")
        if not os.path.exists(spec_path):
            self.cancel(f"specification not found: {spec_path}")

        async def scenario():
            app = self.SeineApp(files=[spec_path])
            async with app.run_test() as pilot:
                indicator = app.screen.query_one("#payload-server-indicator")
                self.assertFalse(indicator.display)

                self.commands.dispatch(app, f"/serve 0 {self.tmp_dir}")
                await pilot.pause()
                self.assertIsNotNone(app._payload_server)
                self.assertTrue(app._payload_server.running)
                self.assertTrue(indicator.display)
                self.assertEqual(_content(indicator), f"[serve: {app._payload_server.port}]")

                indicator.on_click(mock.Mock())
                await pilot.pause()
                self.assertIsInstance(app.screen, self.UpdatesScreen)
                status_text = str(_content(app.screen.query_one("#status")))
                self.assertIn(f"serving {self.tmp_dir} on port {app._payload_server.port}", status_text)

                self.commands.dispatch(app, "/serve stop")
                await pilot.pause()
                self.assertIsNone(app._payload_server)
                self.assertFalse(indicator.display)

        _run(scenario)
