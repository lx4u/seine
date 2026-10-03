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


class RemoteSessionTest(avocado.Test):
    """
    :avocado: tags=tui
    """

    def setUp(self):
        with _tui_required(self):
            from textual.app import App
            from seine import settings
            from seine.tui.app import SeineApp
            from seine.tui.base import BaseScreen, RemoteIndicator
            from seine.tui.remote_session import RemoteSession
        self.App = App
        self.settings = settings
        self.SeineApp = SeineApp
        self.BaseScreen = BaseScreen
        self.RemoteIndicator = RemoteIndicator
        self.RemoteSession = RemoteSession

        self.tmp_dir = tempfile.mkdtemp(prefix="seine-test-remote-session-")
        os.environ["XDG_CONFIG_HOME"] = self.tmp_dir
        self._patches = []
        # Keep the developer's real token, keyring and CA out of the tests.
        self.creds = os.path.join(self.tmp_dir, "credentials.json")
        self._start(mock.patch.dict(os.environ, {
            "SEINE_CREDENTIALS_FILE": self.creds, "SEINE_TOKEN": "", "SEINE_CA_CERT": ""}))
        from seine.credentials import CredentialNotFound
        self._start(mock.patch("seine.credentials._resolve_keyring",
                               side_effect=CredentialNotFound("none")))
        self._start(mock.patch("seine.credentials._keyring_reachable", return_value=False))
        # No real connection to a server that is not there.
        self.followers = []
        self._start(mock.patch("seine.tui.remote_session.EventFollower",
                               side_effect=lambda *args, **kw: self.followers.append(
                                   mock.Mock(args=args)) or self.followers[-1]))

    # avocado never runs addCleanup callbacks: stop everything in tearDown.
    def _start(self, patch):
        patch.start()
        self._patches.append(patch)

    def tearDown(self):
        for patch in reversed(self._patches):
            patch.stop()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _reply(self, code, body=None):
        resp = mock.Mock()
        resp.status_code = code
        resp.json.return_value = body or {"id": "alice", "is_admin": False, "projects": {}}
        resp.raise_for_status.side_effect = (
            None if code < 400 else Exception("HTTP %d" % code))
        return resp

    def _prompting(self, *tokens, save=True):
        """A prompt handing out *tokens*; /me accepts only 'good'."""
        given = iter(tokens)
        calls = []

        def prompt(context, fields, offer_save=False):
            calls.append(context)
            return {"token": next(given), "_save": save}

        def get(url, headers=None, **kwargs):
            ok = (headers or {}).get("Authorization") == "Bearer good"
            return self._reply(200 if ok else 401)

        self._start(mock.patch("seine.tui.credentials.tui_prompt", return_value=prompt))
        return calls, get

    def _save_token(self, token):
        with open(self.creds, "w") as f:
            f.write('{"seine-token": "%s"}' % token)

    def test_session_init_state(self):
        session = self.RemoteSession()
        self.assertFalse(session.connected)
        self.assertIsNone(session.url)
        self.assertIsNone(session.token)
        self.assertIsNone(session.user_id)
        self.assertFalse(session.is_admin)
        self.assertEqual(session.projects, {})
        self.assertIsNone(session.active_project)
        self.assertIsNone(session.ping_ms)
        self.assertEqual(session.auth_headers, {})

    @mock.patch("requests.get")
    def test_session_connect_success(self, mock_get):
        mock_resp = mock.Mock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "id": "alice",
            "is_admin": True,
            "projects": {"core": "admin", "web": "developer"},
        }
        mock_get.return_value = mock_resp

        mock_app = mock.Mock()
        session = self.RemoteSession(app=mock_app)
        ok = session.connect("127.0.0.1:8000", token="secret-token")

        self.assertTrue(ok)
        self.assertTrue(session.connected)
        self.assertEqual(session.url, "http://127.0.0.1:8000")
        self.assertEqual(session.token, "secret-token")
        self.assertEqual(session.user_id, "alice")
        self.assertTrue(session.is_admin)
        self.assertIsNone(session.active_project)  # an administrator chooses
        self.assertEqual(session.projects, {"core": "admin", "web": "developer"})
        self.assertEqual(session.auth_headers, {"Authorization": "Bearer secret-token"})
        self.assertIsNotNone(session.ping_ms)
        mock_app.refresh_indicators.assert_called()

    @mock.patch("requests.get")
    def test_session_connect_failure(self, mock_get):
        mock_get.side_effect = ConnectionError("server offline")
        mock_app = mock.Mock()
        session = self.RemoteSession(app=mock_app)
        ok = session.connect("https://bad-host:8000", token="secret")

        self.assertFalse(ok)
        self.assertFalse(session.connected)
        self.assertIn("server offline", session.last_error)
        mock_app.refresh_indicators.assert_called()

    @mock.patch("requests.get")
    def test_session_disconnect(self, mock_get):
        mock_resp = mock.Mock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"id": "bob", "is_admin": False, "projects": {}}
        mock_get.return_value = mock_resp

        mock_app = mock.Mock()
        session = self.RemoteSession(app=mock_app)
        session.connect("https://remote.server", token="tok")
        self.assertTrue(session.connected)

        session.disconnect()
        self.assertFalse(session.connected)
        self.assertIsNone(session.url)
        self.assertIsNone(session.token)
        self.assertIsNone(session.user_id)
        self.assertFalse(session.is_admin)
        self.assertIsNone(session.ping_ms)

    @mock.patch("requests.get")
    def test_session_ping_probe(self, mock_get):
        session = self.RemoteSession()
        self.assertIsNone(session.ping())

        session.connected = True
        session.url = "http://localhost:8000"
        mock_resp = mock.Mock()
        mock_resp.status_code = 200
        mock_get.return_value = mock_resp

        latency = session.ping()
        self.assertIsNotNone(latency)
        self.assertEqual(session.ping_ms, latency)

        mock_resp.status_code = 500
        self.assertIsNone(session.ping())
        self.assertIsNone(session.ping_ms)

    @mock.patch("requests.get")
    def test_bare_host_defaults_to_https_except_loopback(self, mock_get):
        mock_get.return_value = self._reply(200)
        for given, expected in (("example.org:8000", "https://example.org:8000"),
                                ("localhost:8000", "http://localhost:8000")):
            session = self.RemoteSession()
            self.assertTrue(session.connect(given, token="t"))
            self.assertEqual(session.url, expected)

    @mock.patch("requests.get")
    def test_plain_http_to_a_remote_host_needs_insecure(self, mock_get):
        mock_get.return_value = self._reply(200)
        session = self.RemoteSession()
        self.assertFalse(session.connect("http://10.0.0.1:8000", token="t"))
        self.assertIn("remote_insecure", session.last_error)
        mock_get.assert_not_called()

        self.assertTrue(session.connect("http://10.0.0.1:8000", token="t", insecure=True))
        self.assertIn("unencrypted", session.warning)

        current = self.settings.load()
        current["remote_insecure"] = True
        self.settings.save(current)
        self.assertTrue(self.RemoteSession().connect("http://10.0.0.1:8000", token="t"))

    @mock.patch("requests.get")
    def test_ca_cert_from_settings_or_argument_is_the_verify_value(self, mock_get):
        mock_get.return_value = self._reply(200)
        current = self.settings.load()
        current["remote_ca_cert"] = "/etc/seine/ca.pem"
        self.settings.save(current)
        session = self.RemoteSession()
        self.assertTrue(session.connect("https://srv", token="t"))
        self.assertEqual(mock_get.call_args[1]["verify"], "/etc/seine/ca.pem")
        self.assertEqual(session.verify, "/etc/seine/ca.pem")

        session.request("get", "/api/v1/workers")
        self.assertEqual(mock_get.call_args[1]["verify"], "/etc/seine/ca.pem")
        self.assertEqual(mock_get.call_args[1]["headers"], {"Authorization": "Bearer t"})

        self.assertTrue(self.RemoteSession().connect("https://srv", token="t", ca_cert="/x.pem"))
        self.assertEqual(mock_get.call_args[1]["verify"], "/x.pem")

    @mock.patch("requests.get")
    def test_tls_settings_changed_after_connecting_apply_at_once(self, mock_get):
        mock_get.return_value = self._reply(200)
        session = self.RemoteSession()
        self.assertTrue(session.connect("https://srv", token="t"))
        self.assertFalse(session.insecure)
        self.assertIs(session.verify, True)

        current = self.settings.load()
        current["remote_insecure"] = True
        current["remote_ca_cert"] = "/etc/seine/ca.pem"
        self.settings.save(current)
        self.assertTrue(session.insecure)
        self.assertEqual(session.verify, "/etc/seine/ca.pem")
        session.request("get", "/api/v1/workers")
        self.assertEqual(mock_get.call_args[1]["verify"], "/etc/seine/ca.pem")

    @mock.patch("requests.get")
    def test_options_given_to_connect_win_over_the_settings(self, mock_get):
        mock_get.return_value = self._reply(200)
        session = self.RemoteSession()
        self.assertTrue(session.connect(
            "http://10.0.0.1:8000", token="t", insecure=True, ca_cert="/mine.pem"))
        current = self.settings.load()
        current["remote_insecure"] = False
        current["remote_ca_cert"] = "/etc/seine/ca.pem"
        self.settings.save(current)
        self.assertTrue(session.insecure)
        self.assertEqual(session.verify, "/mine.pem")

        session.disconnect()
        self.assertFalse(session.insecure)
        self.assertEqual(session.verify, "/etc/seine/ca.pem")

    @mock.patch("requests.get")
    def test_tls_failure_hints_at_the_ca_setting(self, mock_get):
        import requests
        mock_get.side_effect = requests.exceptions.SSLError("self-signed certificate")
        session = self.RemoteSession()
        self.assertFalse(session.connect("https://srv", token="t"))
        self.assertIn("remote_ca_cert", session.last_error)

    @mock.patch("requests.get")
    def test_prompted_token_is_saved_once_accepted(self, mock_get):
        calls, mock_get.side_effect = self._prompting("good")
        session = self.RemoteSession(app=mock.Mock())
        self.assertTrue(session.connect("https://srv"))
        self.assertEqual(session.token, "good")
        self.assertEqual(len(calls), 1)
        with open(self.creds) as f:
            self.assertIn('"good"', f.read())

    @mock.patch("requests.get")
    def test_environment_token_wins_over_a_saved_one(self, mock_get):
        self._save_token("stale")
        calls, mock_get.side_effect = self._prompting()
        with mock.patch.dict(os.environ, {"SEINE_TOKEN": "good"}):
            session = self.RemoteSession(app=mock.Mock())
            self.assertTrue(session.connect("https://srv"))
        self.assertEqual(calls, [])
        with open(self.creds) as f:
            self.assertIn("stale", f.read())

    @mock.patch("requests.get")
    def test_empty_environment_token_falls_back_to_the_saved_one(self, mock_get):
        self._save_token("good")
        calls, mock_get.side_effect = self._prompting()
        session = self.RemoteSession(app=mock.Mock())  # SEINE_TOKEN is "" in setUp
        self.assertTrue(session.connect("https://srv"))
        self.assertEqual(calls, [])

    @mock.patch("requests.get")
    def test_rejected_saved_token_is_asked_again(self, mock_get):
        self._save_token("stale")
        calls, mock_get.side_effect = self._prompting("good")
        session = self.RemoteSession(app=mock.Mock())
        self.assertTrue(session.connect("https://srv"))
        self.assertEqual(len(calls), 1)
        self.assertIn("wrong credentials", calls[0])
        with open(self.creds) as f:
            self.assertIn('"good"', f.read())

    @mock.patch("requests.get")
    def test_gives_up_after_three_rejected_tokens_until_next_connect(self, mock_get):
        self._save_token("stale")
        calls, mock_get.side_effect = self._prompting("bad1", "bad2", "good")
        session = self.RemoteSession(app=mock.Mock())
        self.assertFalse(session.connect("https://srv"))
        self.assertIn("3 tokens rejected", session.last_error)
        self.assertEqual(len(calls), 2)
        with open(self.creds) as f:
            self.assertIn("stale", f.read())
        # A new /remote starts again, the saved token being the first try.
        self.assertTrue(session.connect("https://srv"))
        self.assertEqual(len(calls), 3)

    @mock.patch("requests.get")
    def test_cancelled_prompt_stops_with_a_clear_error(self, mock_get):
        from seine.credentials import CredentialNotFound
        self._save_token("stale")
        _, mock_get.side_effect = self._prompting()
        session = self.RemoteSession(app=mock.Mock())
        closed = mock.Mock(side_effect=CredentialNotFound("closed"))
        with mock.patch("seine.tui.credentials.tui_prompt", return_value=closed):
            self.assertFalse(session.connect("https://srv"))
        self.assertIn("cancelled", session.last_error)

    @mock.patch("requests.get")
    def test_declined_save_writes_nothing(self, mock_get):
        calls, mock_get.side_effect = self._prompting("good", save=False)
        session = self.RemoteSession(app=mock.Mock())
        self.assertTrue(session.connect("https://srv"))
        self.assertFalse(os.path.exists(self.creds))

    @mock.patch("requests.get")
    def test_explicit_token_is_never_retried(self, mock_get):
        calls, mock_get.side_effect = self._prompting("good")
        session = self.RemoteSession(app=mock.Mock())
        self.assertFalse(session.connect("https://srv", token="wrong"))
        self.assertEqual(calls, [])
        self.assertIn("rejected the token", session.last_error)
        self.assertEqual(mock_get.call_count, 1)

    @mock.patch("requests.get")
    def test_ping_401_disconnects_without_prompting(self, mock_get):
        calls, mock_get.side_effect = self._prompting("good")
        session = self.RemoteSession(app=mock.Mock())
        self.assertTrue(session.connect("https://srv", token="good"))
        mock_get.side_effect = lambda *a, **kw: self._reply(401)
        self.assertIsNone(session.ping())
        self.assertFalse(session.connected)
        self.assertIn("/remote", session.last_error)
        self.assertEqual(calls, [])

    def _connect_as(self, mock_get, profile):
        mock_get.return_value = self._reply(200, profile)
        session = self.RemoteSession(app=mock.Mock())
        self.assertTrue(session.connect("https://srv", token="t"))
        return session

    @mock.patch("requests.get")
    def test_project_is_decided_only_when_nothing_needs_asking(self, mock_get):
        both = {"core": "developer", "web": "developer"}
        cases = (
            ({"projects": both, "default_project": "web"}, "web"),
            ({"projects": {"core": "developer"}}, "core"),
            ({"projects": both}, None),
            ({"projects": {}}, None),
            ({"is_admin": True, "projects": {"core": "admin"}}, None),
            ({"is_admin": True, "projects": {}, "default_project": "tools"}, "tools"),
        )
        for profile, expected in cases:
            session = self._connect_as(mock_get, {"id": "alice", **profile})
            self.assertEqual(session.active_project, expected, profile)
            self.assertEqual(session.default_project, profile.get("default_project"))

    @mock.patch("requests.get")
    def test_disconnect_forgets_the_default_project(self, mock_get):
        session = self._connect_as(mock_get, {"projects": {"core": "developer"},
                                              "default_project": "core"})
        session.disconnect()
        self.assertIsNone(session.default_project)
        self.assertIsNone(session.active_project)

    @mock.patch("requests.get")
    def test_all_projects_lists_everything_for_an_administrator_only(self, mock_get):
        member = self._connect_as(mock_get, {"projects": {"core": "developer"}})
        self.assertEqual(member.all_projects(), {"core": "developer"})

        admin = self._connect_as(mock_get, {"is_admin": True, "projects": {"core": "admin"}})

        def get(url, **kwargs):
            if url.endswith("/api/v1/projects"):
                return self._reply(200, [{"name": "core"}, {"name": "web"}])
            return self._reply(200)
        mock_get.side_effect = get
        self.assertEqual(admin.all_projects(), {"core": "admin", "web": "admin"})

    @mock.patch("requests.patch")
    @mock.patch("requests.get")
    def test_default_project_is_saved_on_the_server(self, mock_get, mock_patch):
        session = self._connect_as(mock_get, {"projects": {"core": "developer", "web": "developer"}})
        mock_patch.return_value = self._reply(200)
        self.assertIsNone(session.set_default_project("web"))
        self.assertEqual(mock_patch.call_args[1]["json"], {"default_project": "web"})
        self.assertEqual(session.default_project, "web")

        mock_patch.return_value = self._reply(404)
        self.assertIn("cannot store", session.set_default_project("core"))
        mock_patch.return_value = self._reply(403)
        self.assertEqual(session.set_default_project("core"), "HTTP 403")
        self.assertEqual(session.default_project, "web")

    @mock.patch("requests.get")
    def test_use_project_switches_the_active_project(self, mock_get):
        session = self._connect_as(mock_get, {"projects": {"core": "developer", "web": "developer"}})
        session.use_project("web")
        self.assertEqual(session.active_project, "web")
        session.app.refresh_indicators.assert_called()

    @mock.patch("requests.get")
    def test_events_are_followed_for_the_active_project_only(self, mock_get):
        session = self._connect_as(mock_get, {"projects": {"core": "developer", "web": "developer"}})
        self.assertEqual(self.followers, [])
        session.use_project("web")
        self.assertEqual(self.followers[0].args[:3], ("https://srv", "web", "t"))
        self.followers[0].start.assert_called_once_with()
        session.use_project("core")
        self.followers[0].stop.assert_called_once_with()
        self.assertEqual(self.followers[1].args[1], "core")

    @mock.patch("requests.get")
    def test_disconnect_stops_following(self, mock_get):
        session = self._connect_as(mock_get, {"projects": {"core": "developer"}})
        session.disconnect()
        self.followers[0].stop.assert_called_once_with()
        self.assertEqual(len(self.followers), 1)

    @mock.patch("requests.get")
    def test_a_pushed_event_is_handled_on_the_ui_thread(self, mock_get):
        session = self._connect_as(mock_get, {"projects": {"core": "developer"}})
        with mock.patch("seine.tui.remote_match.on_event") as handle:
            self.followers[0].args[3]({"type": "build_status"})
        handle.assert_called_once_with(session.app, {"type": "build_status"})

    def test_indicator_text_formatting(self):
        indicator = self.RemoteIndicator()
        mock_app = mock.Mock()
        mock_app.is_running = False
        session = self.RemoteSession(app=mock_app)
        mock_app.remote_session = session

        with mock.patch.object(self.RemoteIndicator, "app", new_callable=mock.PropertyMock, return_value=mock_app):
            indicator.refresh_text()
            self.assertFalse(indicator.display)

            session.connected = True
            session.url = "http://127.0.0.1:8000"
            session.active_project = "distro-arm64"
            session.is_admin = False

            indicator.refresh_text()
            self.assertTrue(indicator.display)
            self.assertEqual(str(indicator.renderable), "[remote: distro-arm64]")

            session.is_admin = True
            indicator.refresh_text()
            self.assertEqual(str(indicator.renderable), "[remote: distro-arm64] [admin]")

            session.active_project = None
            indicator.refresh_text()
            self.assertEqual(str(indicator.renderable), "[remote: no project] [admin]")

    def test_indicator_click_dispatches(self):
        indicator = self.RemoteIndicator()
        mock_app = mock.Mock()
        mock_app.SCREENS = {"remote": mock.Mock()}
        with mock.patch.object(self.RemoteIndicator, "app", new_callable=mock.PropertyMock, return_value=mock_app):
            indicator.on_click(mock.Mock())
            mock_app.show.assert_called_once_with("remote")

    def test_base_screen_footer_mounts_indicator(self):
        mock_app = mock.Mock()
        mock_app.history = mock.Mock()
        mock_app.history.entries.return_value = []
        screen = self.BaseScreen()
        with mock.patch.object(self.BaseScreen, "app", new_callable=mock.PropertyMock, return_value=mock_app):
            footer_widgets = list(screen.footer())
            infobar = next(w for w in footer_widgets if getattr(w, "id", None) == "infobar")
            child_ids = [getattr(c, "id", None) for c in getattr(infobar, "_pending_children", [])]
            self.assertIn("remote-indicator", child_ids)

    def test_app_initializes_remote_session(self):
        app = self.SeineApp()
        self.assertTrue(hasattr(app, "remote_session"))
        self.assertIsInstance(app.remote_session, self.RemoteSession)

    def test_app_auto_connects_remote_on_mount(self):
        current = self.settings.load()
        current["auto_connect_remote"] = True
        current["default_remote"] = "http://10.0.0.1:8000"
        self.settings.save(current)

        app = self.SeineApp()
        app.run_worker = mock.Mock()
        app.push_screen = mock.Mock()
        app.call_after_refresh = mock.Mock()

        app.on_mount()
        app.run_worker.assert_called_once()

