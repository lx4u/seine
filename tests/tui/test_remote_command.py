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


class RemoteCommandTest(avocado.Test):
    """
    :avocado: tags=tui
    """

    def setUp(self):
        with _tui_required(self):
            from textual.app import App
            from seine import settings
            from seine.tui import app, commands
            from seine.tui.app import SeineApp, run
            from seine.tui.remote_session import RemoteSession
        self.App = App
        self.settings = settings
        self.tui_app = app
        self.commands = commands
        self.SeineApp = SeineApp
        self.run = run
        self.RemoteSession = RemoteSession

        self.tmp_dir = tempfile.mkdtemp(prefix="seine-test-remote-cmd-")
        os.environ["XDG_CONFIG_HOME"] = self.tmp_dir

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_registry_and_options(self):
        self.assertIn("remote", self.commands.REGISTRY)
        cmd = self.commands.REGISTRY["remote"]
        self.assertEqual(cmd.name, "remote")
        self.assertIn("disconnect", self.commands.OPTIONS["remote"])
        self.assertIn("status", self.commands.OPTIONS["remote"])
        self.assertIn("screen", self.commands.OPTIONS["remote"])

    def test_command_syntax_errors(self):
        mock_app = mock.Mock()
        with self.assertRaises(self.commands.CommandError):
            self.commands.dispatch(mock_app, "/remote disconnect extra")

        with self.assertRaises(self.commands.CommandError):
            self.commands.dispatch(mock_app, "/remote status extra")

        with self.assertRaises(self.commands.CommandError):
            self.commands.dispatch(mock_app, "/remote screen extra")

        with self.assertRaises(self.commands.CommandError):
            self.commands.dispatch(mock_app, "/remote http://one http://two")

    def test_remote_status_disconnected(self):
        mock_app = mock.Mock()
        mock_app.remote_session = self.RemoteSession(app=mock_app)
        self.commands.dispatch(mock_app, "/remote status")
        mock_app.say.assert_called_once_with("remote: disconnected (local engine mode)")

    def test_remote_status_connected(self):
        mock_app = mock.Mock()
        session = self.RemoteSession(app=mock_app)
        session.connected = True
        session.url = "http://10.0.0.1:8000"
        session.ping_ms = 4.2
        session.user_id = "alice"
        session.is_admin = True
        session.active_project = "distro-core"
        mock_app.remote_session = session

        self.commands.dispatch(mock_app, "/remote status")
        mock_app.say.assert_called_once_with(
            "remote: http://10.0.0.1:8000 (4.2ms) · user: alice [admin] · project: distro-core"
        )

    def test_remote_disconnect(self):
        mock_app = mock.Mock()
        session = self.RemoteSession(app=mock_app)
        session.connected = True
        session.url = "http://10.0.0.1:8000"
        mock_app.remote_session = session

        self.commands.dispatch(mock_app, "/remote disconnect")
        self.assertFalse(session.connected)
        self.assertIsNone(session.url)
        mock_app.say.assert_called_once_with("remote: disconnected")

    def test_remote_screen_registered_and_unregistered(self):
        mock_app = mock.Mock()
        mock_app.SCREENS = {}
        self.commands.dispatch(mock_app, "/remote screen")
        mock_app.say.assert_called_once_with("remote: cockpit screen not registered")

        mock_app.reset_mock()
        mock_app.SCREENS = {"remote": mock.Mock()}
        self.commands.dispatch(mock_app, "/remote screen")
        mock_app.show.assert_called_once_with("remote")

    def test_remote_connect_explicit_url_success(self):
        mock_app = mock.Mock()
        mock_app.is_running = False
        mock_app.SCREENS = {"remote": mock.Mock()}
        session = self.RemoteSession(app=mock_app)
        mock_app.remote_session = session

        with mock.patch.object(session, "connect", return_value=True):
            self.commands.dispatch(mock_app, "/remote http://192.168.1.10:8000")
            session.connect.assert_called_once_with("http://192.168.1.10:8000")
            mock_app.show.assert_called_once_with("remote")

    def test_remote_connect_explicit_url_failure(self):
        mock_app = mock.Mock()
        mock_app.is_running = False
        session = self.RemoteSession(app=mock_app)
        session.last_error = "connection refused"
        mock_app.remote_session = session

        with mock.patch.object(session, "connect", return_value=False):
            self.commands.dispatch(mock_app, "/remote http://bad-host:8000")
            mock_app.say.assert_any_call("remote error: connection refused", error=True)

    def test_bare_remote_connects_default_remote(self):
        current = self.settings.load()
        current["default_remote"] = "http://cluster.internal:8000"
        self.settings.save(current)

        mock_app = mock.Mock()
        mock_app.is_running = False
        mock_app.SCREENS = {}
        session = self.RemoteSession(app=mock_app)
        mock_app.remote_session = session

        with mock.patch.object(session, "connect", return_value=True):
            self.commands.dispatch(mock_app, "/remote")
            session.connect.assert_called_once_with("http://cluster.internal:8000")

    def test_bare_remote_prompts_when_unset_and_cancelled(self):
        mock_app = mock.Mock()
        mock_app.is_running = False
        session = self.RemoteSession(app=mock_app)
        mock_app.remote_session = session

        with mock.patch("seine.tui.credentials.tui_prompt", side_effect=Exception("cancelled")):
            self.commands.dispatch(mock_app, "/remote")
            mock_app.say.assert_called_with(
                "remote: no URL specified and default_remote not configured", warning=True
            )

    def test_bare_remote_when_already_connected_shows_screen(self):
        mock_app = mock.Mock()
        mock_app.SCREENS = {"remote": mock.Mock()}
        session = self.RemoteSession(app=mock_app)
        session.connected = True
        mock_app.remote_session = session

        self.commands.dispatch(mock_app, "/remote")
        mock_app.show.assert_called_once_with("remote")

    def _project_session(self, projects, default=None, is_admin=False):
        mock_app = mock.Mock()
        mock_app.is_running = False
        session = self.RemoteSession(app=mock_app)
        session.connected = True
        session.projects = dict(projects)
        session.default_project = default
        session.is_admin = is_admin
        mock_app.remote_session = session
        return mock_app, session

    def test_real_app_knows_the_cockpit_screen(self):
        self.assertIn("remote", self.SeineApp.SCREENS)

    def test_remote_screen_opens_in_the_real_app(self):
        from seine.tui.remote_screen import RemoteScreen
        env = {"SEINE_HISTORY_FILE": os.path.join(self.tmp_dir, "history.json"),
               "SEINE_CACHE_DIR": self.tmp_dir}

        async def scenario():
            app = self.SeineApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                self.commands.dispatch(app, "/remote screen")
                await pilot.pause()
                self.assertIsInstance(app.screen, RemoteScreen)

        with mock.patch.dict(os.environ, env):
            asyncio.run(scenario())

    def test_project_is_registered_with_its_option(self):
        self.assertIn("project", self.commands.REGISTRY)
        self.assertIn("default", self.commands.OPTIONS["project"])
        self.assertIn("insecure", self.commands.OPTIONS["remote"])
        self.assertIn("ca-cert=", self.commands.OPTIONS["remote"])

    def test_project_needs_a_connection_and_sane_arguments(self):
        mock_app = mock.Mock()
        mock_app.remote_session = self.RemoteSession(app=mock_app)
        with self.assertRaises(self.commands.CommandError):
            self.commands.dispatch(mock_app, "/project core")
        mock_app, _ = self._project_session({"core": "developer"})
        for line in ("/project a b", "/project --default", "/project --bogus core"):
            with self.assertRaises(self.commands.CommandError):
                self.commands.dispatch(mock_app, line)

    def test_project_by_name_switches_and_can_save_the_default(self):
        mock_app, session = self._project_session(
            {"core": "developer", "web": "releaser"})
        self.commands.dispatch(mock_app, "/project web")
        self.assertEqual(session.active_project, "web")
        mock_app.say.assert_any_call("project: web")

        with mock.patch.object(session, "set_default_project", return_value=None) as save:
            self.commands.dispatch(mock_app, "/project --default core")
        save.assert_called_once_with("core")
        self.assertEqual(session.active_project, "core")
        mock_app.say.assert_any_call("project: core is now your default")

        with mock.patch.object(session, "set_default_project", return_value="HTTP 403"):
            self.commands.dispatch(mock_app, "/project core --default")
        mock_app.say.assert_any_call(
            "project: could not save the default: HTTP 403", warning=True)

    def test_project_unknown_name_lists_the_choices(self):
        mock_app, session = self._project_session({"core": "developer", "web": "developer"})
        self.commands.dispatch(mock_app, "/project ghost")
        self.assertIsNone(session.active_project)
        mock_app.say.assert_any_call(
            "project: unknown project 'ghost' -- you can use: core, web", error=True)

    def test_project_without_a_name_opens_the_picker(self):
        from seine.tui.project_picker import ProjectPicker
        mock_app, session = self._project_session({"core": "developer", "web": "developer"})
        self.commands.dispatch(mock_app, "/project")
        picker, callback = mock_app.push_screen.call_args[0]
        self.assertIsInstance(picker, ProjectPicker)
        self.assertTrue(picker._offer_default)

        callback(None)
        self.assertIsNone(session.active_project)
        with mock.patch.object(session, "set_default_project", return_value=None) as save:
            callback(("web", True))
        self.assertEqual(session.active_project, "web")
        save.assert_called_once_with("web")

    def test_picker_does_not_offer_a_default_when_one_exists(self):
        mock_app, _ = self._project_session({"core": "developer"}, default="core")
        self.commands.dispatch(mock_app, "/project")
        self.assertFalse(mock_app.push_screen.call_args[0][0]._offer_default)

    def test_project_picker_screen(self):
        from seine.tui.project_picker import ProjectPicker
        projects = {"core": "developer", "web": "releaser"}

        def pick(keys, **kwargs):
            results = []
            picker = ProjectPicker(projects, **kwargs)

            class Host(self.App):
                def on_mount(self):
                    self.push_screen(picker, results.append)

            async def scenario():
                async with Host().run_test() as pilot:
                    await pilot.pause()
                    for key in keys:
                        await pilot.press(key)
                    await pilot.pause()

            asyncio.run(scenario())
            return results

        self.assertEqual(pick(["enter"]), [("core", False)])
        self.assertEqual(pick(["down", "enter"]), [("web", False)])
        self.assertEqual(pick(["enter"], current="web"), [("web", False)])
        self.assertEqual(pick(["enter"], offer_default=True), [("core", True)])
        self.assertEqual(pick(["escape"]), [None])

    def test_cli_remote_parsing_in_run(self):
        with mock.patch.object(self.SeineApp, "run") as mock_app_run:
            with mock.patch("seine.tui.app.SeineApp.__init__", return_value=None) as mock_init:
                self.run(["--remote", "http://10.0.0.1:8000", "spec.yaml"])
                mock_init.assert_called_once_with(
                    files=["spec.yaml"],
                    interaction_socket=None,
                    remote="http://10.0.0.1:8000",
                    connect_remote=True,
                    remote_insecure=None,
                    remote_ca_cert=None,
                )

        with mock.patch.object(self.SeineApp, "run") as mock_app_run:
            with mock.patch("seine.tui.app.SeineApp.__init__", return_value=None) as mock_init:
                self.run(["--remote=http://cluster:8000"])
                mock_init.assert_called_once_with(
                    files=None,
                    interaction_socket=None,
                    remote="http://cluster:8000",
                    connect_remote=True,
                    remote_insecure=None,
                    remote_ca_cert=None,
                )

        with mock.patch.object(self.SeineApp, "run") as mock_app_run:
            with mock.patch("seine.tui.app.SeineApp.__init__", return_value=None) as mock_init:
                self.run(["--remote", "spec.yaml"])
                mock_init.assert_called_once_with(
                    files=["spec.yaml"],
                    interaction_socket=None,
                    remote=None,
                    connect_remote=True,
                    remote_insecure=None,
                    remote_ca_cert=None,
                )

    def test_cli_tls_flags_in_run(self):
        cert = os.path.join(self.tmp_dir, "ca.pem")
        with open(cert, "w") as f:
            f.write("pem")
        with mock.patch.object(self.SeineApp, "run"):
            with mock.patch("seine.tui.app.SeineApp.__init__", return_value=None) as mock_init:
                self.run(["--remote=https://srv", "--insecure", "--ca-cert", cert, "spec.yaml"])
                mock_init.assert_called_once_with(
                    files=["spec.yaml"],
                    interaction_socket=None,
                    remote="https://srv",
                    connect_remote=True,
                    remote_insecure=True,
                    remote_ca_cert=cert,
                )
        with self.assertRaises(ValueError):
            self.run(["--ca-cert=/no/such/ca.pem"])

    def test_app_auto_connect_passes_flags_and_prints_the_warning(self):
        app = self.SeineApp(remote="http://10.0.0.1:8000", connect_remote=True,
                            remote_insecure=True, remote_ca_cert="/ca.pem")
        app.say = mock.Mock()
        app.call_from_thread = lambda fn, *a, **kw: fn(*a, **kw)

        def connect(target, **kwargs):
            app.remote_session.warning = "plain http"
            return True
        with mock.patch.object(app.remote_session, "connect", side_effect=connect) as c:
            app._auto_connect("http://10.0.0.1:8000")
        c.assert_called_once_with("http://10.0.0.1:8000", insecure=True, ca_cert="/ca.pem")
        app.say.assert_called_once_with("remote: warning: plain http", warning=True)

    def test_remote_flags_override_the_settings_for_one_connection(self):
        cert = os.path.join(self.tmp_dir, "ca.pem")
        with open(cert, "w") as f:
            f.write("pem")
        mock_app = mock.Mock()
        mock_app.is_running = False
        mock_app.SCREENS = {}
        session = self.RemoteSession(app=mock_app)
        mock_app.remote_session = session
        with mock.patch.object(session, "connect", return_value=True):
            self.commands.dispatch(mock_app, "/remote --insecure http://10.0.0.1:8000")
            session.connect.assert_called_with("http://10.0.0.1:8000", insecure=True)
            self.commands.dispatch(mock_app, "/remote http://srv --ca-cert %s" % cert)
            session.connect.assert_called_with("http://srv", ca_cert=cert)

    def test_remote_flags_without_url_use_default_remote(self):
        current = self.settings.load()
        current["default_remote"] = "http://cluster.internal:8000"
        self.settings.save(current)
        mock_app = mock.Mock()
        mock_app.is_running = False
        mock_app.SCREENS = {}
        session = self.RemoteSession(app=mock_app)
        session.connected = True
        mock_app.remote_session = session
        with mock.patch.object(session, "connect", return_value=True):
            self.commands.dispatch(mock_app, "/remote --insecure")
            session.connect.assert_called_once_with(
                "http://cluster.internal:8000", insecure=True)

    def test_remote_flag_errors(self):
        mock_app = mock.Mock()
        for line in ("/remote --bogus", "/remote --ca-cert", "/remote --ca-cert /no/such.pem",
                     "/remote --insecure http://a http://b"):
            with self.assertRaises(self.commands.CommandError):
                self.commands.dispatch(mock_app, line)

    def test_remote_connect_prints_the_plain_http_warning(self):
        mock_app = mock.Mock()
        mock_app.is_running = False
        mock_app.SCREENS = {}
        session = self.RemoteSession(app=mock_app)
        mock_app.remote_session = session

        def connect(target, **kwargs):
            session.warning = "http://10.0.0.1:8000 is plain http: the token travels unencrypted"
            return True
        with mock.patch.object(session, "connect", side_effect=connect):
            self.commands.dispatch(mock_app, "/remote --insecure http://10.0.0.1:8000")
        mock_app.say.assert_any_call(
            "remote: warning: http://10.0.0.1:8000 is plain http: "
            "the token travels unencrypted", warning=True)

    def test_app_on_mount_with_connect_remote_flag(self):
        app = self.SeineApp(remote="http://cluster:8000", connect_remote=True)
        app.run_worker = mock.Mock()
        app.push_screen = mock.Mock()
        app.call_after_refresh = mock.Mock()

        app.on_mount()
        app.run_worker.assert_called_once()
