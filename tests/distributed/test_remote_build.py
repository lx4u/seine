# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import io
import json
import os
import shutil
import tempfile
import time
from unittest import mock

import requests
from avocado import Test

from seine.distributed.client import remote
from seine.distributed.client.remote import (
    RemoteBuild,
    _print_artifacts,
    build_remote,
    spec_architecture,
    upload_worktree,
)
from seine.distributed.common.wsclient import WsClosed

SERVER = "http://localhost:8000"
BUILD = "bld-test1"


class FakeWS:
    """A websocket client: replays items, raising the exceptions among them."""

    def __init__(self, items=()):
        self.items = list(items)
        self.sent = []
        self.closes = 0
        self.connect = mock.MagicMock()

    def close(self):
        self.closes += 1

    def send(self, message):
        self.sent.append(message)

    def recv(self, timeout=None):
        if not self.items:
            time.sleep(0.01)
            raise TimeoutError()
        item = self.items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def log(text):
    return json.dumps({"text": text})


def closed(code):
    return WsClosed(code)


class RemoteBuildTest(Test):
    """Unit tests for the remote build client."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="seine-test-remote-")
        self.spec = os.path.join(self.tmp_dir, "main.yaml")
        self.write_spec("distribution:\n  architecture: arm64\n")
        self.archive = os.path.join(self.tmp_dir, "bundle.tar.zst")
        with open(self.archive, "wb") as f:
            f.write(b"zst")
        self.statuses = ["completed"]
        self.build_extra = {}
        self.get_urls = []
        self.posts = []
        self.cancel_status = 202
        self.patches = [
            mock.patch("seine.distributed.client.worktree.pack_worktree",
                       side_effect=self.pack),
            mock.patch.object(remote, "upload_worktree",
                              return_value={"digest": "dig-1"}),
            mock.patch("requests.get", side_effect=self.http_get),
            mock.patch("requests.post", side_effect=self.http_post),
            mock.patch.dict(os.environ, {"SEINE_TOKEN": "", "SEINE_CA_CERT": ""}),
        ]
        (self.pack_mock, self.upload_mock, self.get_mock, self.post_mock, _) = [
            p.start() for p in self.patches]
        self.ws = FakeWS()
        ws_patch = mock.patch.object(remote, "WsClient", return_value=self.ws)
        ws_patch.start()
        self.patches.append(ws_patch)
        # Keep the developer's real keyring and credentials file out of it.
        from seine.credentials import CredentialNotFound
        for extra in (
            mock.patch("seine.credentials._resolve_keyring",
                       side_effect=CredentialNotFound("none")),
            mock.patch.dict(os.environ, {
                "SEINE_CREDENTIALS_FILE": os.path.join(self.tmp_dir, "none.json")}),
        ):
            extra.start()
            self.patches.append(extra)

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def write_spec(self, text):
        with open(self.spec, "w", encoding="utf-8") as f:
            f.write(text)

    def pack(self, root):
        # Packing removes the archive after upload, so make a fresh one.
        with open(self.archive, "wb") as f:
            f.write(b"zst")
        return self.archive, "local-digest"

    def response(self, code=200, body=None):
        resp = mock.MagicMock()
        resp.status_code = code
        resp.json.return_value = body if body is not None else {}
        return resp

    def http_get(self, url, **kwargs):
        self.get_urls.append(url)
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        if isinstance(status, Exception):
            raise status
        if isinstance(status, int):
            return self.response(status, {"detail": "nope"})
        body = {"id": BUILD, "status": status}
        body.update(self.build_extra)
        return self.response(200, body)

    def http_post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        if url.endswith("/cancel"):
            if isinstance(self.cancel_status, Exception):
                raise self.cancel_status
            return self.response(self.cancel_status, {"status": "cancelling"})
        return self.response(200, {
            "build_id": BUILD, "status": "queued",
            "project": "proj", "target_arch": "arm64",
        })

    def new(self, **kwargs):
        kwargs.setdefault("token", "pat-test")
        kwargs.setdefault("root_dir", self.tmp_dir)
        kwargs.setdefault("sleep", lambda seconds: time.sleep(0.05))
        kwargs.setdefault("drain_grace", 0.3)
        options = kwargs.pop("options", {})
        return RemoteBuild(kwargs.pop("server_url", SERVER), kwargs.pop("project", "proj"),
                           self.spec, options=options, **kwargs)

    def run_build(self, **kwargs):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            code = self.new(**kwargs).run()
        return code, out.getvalue(), err.getvalue()

    def submitted(self):
        return [kw for url, kw in self.posts if url.endswith("/api/v1/builds")][0]

    def network_calls(self):
        return (self.get_mock.call_count + self.post_mock.call_count
                + self.upload_mock.call_count + self.ws.connect.call_count)

    def test_upload_worktree(self):
        resp = self.response(200, {"digest": "abc123"})
        self.post_mock.side_effect = None
        self.post_mock.return_value = resp
        res = upload_worktree(SERVER, "myproj", self.archive, token="tok-123", verify="/ca.pem")
        self.assertEqual(res["digest"], "abc123")
        args, kwargs = self.post_mock.call_args
        self.assertEqual(args[0], f"{SERVER}/api/v1/projects/myproj/worktrees")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer tok-123")
        self.assertEqual(kwargs["headers"]["Content-Type"], "application/octet-stream")
        self.assertEqual(kwargs["verify"], "/ca.pem")
        connect, read = kwargs["timeout"]
        self.assertGreater(read, 300)
        self.assertLess(connect, 60)

    def test_upload_worktree_env_defaults_to_dev(self):
        self.post_mock.side_effect = None
        self.post_mock.return_value = self.response(200, {"digest": "abc"})
        upload_worktree(SERVER, "myproj", self.archive)
        self.assertEqual(self.post_mock.call_args[1]["params"], {"env": "dev"})

    def test_release_build_uploads_the_worktree_for_prod(self):
        self.run_build(is_release=True)
        self.assertEqual(self.upload_mock.call_args[1]["env"], "prod")

    def test_dev_build_uploads_the_worktree_for_dev(self):
        self.run_build()
        self.assertEqual(self.upload_mock.call_args[1]["env"], "dev")

    def test_upload_worktree_refused(self):
        self.post_mock.side_effect = None
        self.post_mock.return_value = self.response(403, {"detail": "not a member"})
        with self.assertRaises(remote.RemoteError) as ctx:
            upload_worktree(SERVER, "myproj", self.archive, token="t")
        self.assertIn("not allowed", str(ctx.exception))
        self.assertIn("not a member", str(ctx.exception))

    def test_completed_build_exits_zero(self):
        code, out, _ = self.run_build()
        self.assertEqual(code, 0)
        self.assertIn(f"Build {BUILD} finished with status: COMPLETED", out)
        self.upload_mock.assert_called_once()
        self.assertEqual(self.upload_mock.call_args[1]["token"], "pat-test")
        self.assertEqual(self.submitted()["json"]["worktree_digest"], "dig-1")
        self.assertEqual(self.submitted()["headers"]["Authorization"], "Bearer pat-test")

    def test_status_is_polled_not_fetched_per_log_line(self):
        self.statuses = ["queued", "running", "running", "completed"]
        self.ws.items = [log(f"line {i}\n") for i in range(60)]
        sleeps = []
        code, out, _ = self.run_build(
            poll_interval=2.0, sleep=lambda s: (sleeps.append(s), time.sleep(0.05)))
        self.assertEqual(code, 0)
        for i in range(60):
            self.assertIn(f"line {i}\n", out)
        self.assertEqual(len(self.get_urls), 4)
        self.assertEqual(sleeps, [2.0, 2.0, 2.0])

    def test_stream_authenticates_first_and_only_once(self):
        self.run_build()
        self.assertEqual(self.ws.sent, [json.dumps({"auth": "pat-test"})])
        url = self.ws.connect.call_args[0][0]
        self.assertEqual(url, f"ws://localhost:8000/api/v1/builds/{BUILD}/stream")

    def test_https_uses_wss_and_ca_cert(self):
        ctx = object()
        with mock.patch.object(remote, "ws_ssl_context", return_value=ctx) as ssl_ctx:
            code, _, _ = self.run_build(
                server_url="https://server.example:8443",
                options={"ca_cert": "/etc/ca.pem"})
        self.assertEqual(code, 0)
        url = self.ws.connect.call_args[0][0]
        self.assertTrue(url.startswith("wss://server.example:8443/"))
        ssl_ctx.assert_called_once_with(url, "/etc/ca.pem")
        self.assertIs(self.ws.connect.call_args[0][1], ctx)
        for kw in [kw for _, kw in self.posts] + [self.upload_mock.call_args[1]]:
            self.assertEqual(kw["verify"], "/etc/ca.pem")
        self.assertEqual(self.get_mock.call_args[1]["verify"], "/etc/ca.pem")

    def test_ca_cert_from_environment(self):
        with mock.patch.dict(os.environ, {"SEINE_CA_CERT": "/env/ca.pem"}):
            self.run_build()
        self.assertEqual(self.submitted()["verify"], "/env/ca.pem")

    def test_default_verifies_certificates(self):
        self.run_build()
        self.assertIs(self.submitted()["verify"], True)

    def test_every_request_has_a_timeout(self):
        self.run_build()
        for _, kw in self.posts:
            self.assertIn("timeout", kw)
        for call in self.get_mock.call_args_list:
            self.assertIn("timeout", call[1])

    def test_stream_close_codes(self):
        expected = {
            4401: "authentication failed",
            4403: "not allowed to follow this build in this project",
            4404: "build not found",
        }
        for code, text in expected.items():
            self.ws.items = [closed(code)]
            self.statuses = ["running", "completed"]
            ret, _, err = self.run_build()
            self.assertIn(text, err, code)
            self.assertEqual(ret, 0, code)

    def test_events_go_to_on_event_and_logs_to_out(self):
        events = []
        self.ws.items = [
            json.dumps({"type": "task_plan", "tasks": [{"name": "a", "needs": []}]}),
            log("hello\n"),
            json.dumps({"type": "task_started", "task": "a"}),
            "not json",
            json.dumps([1]),
        ]
        ret, out, _ = self.run_build(on_event=events.append)
        self.assertEqual(ret, 0)
        self.assertEqual([e["type"] for e in events], ["task_plan", "task_started"])
        self.assertIn("hello", out)
        self.assertNotIn("task_plan", out)

    def test_log_dir_gets_one_file_per_task(self):
        logs = os.path.join(self.tmp_dir, "logs")
        self.ws.items = [
            json.dumps({"text": "one\n", "task": "rootfs"}),
            json.dumps({"text": "two\n", "task": "package:amd64:g++"}),
            json.dumps({"text": "three\n", "task": "rootfs"}),
            json.dumps({"text": "system\n"}),
            json.dumps({"text": "evil\n", "task": "../escape"}),
        ]
        ret, out, _ = self.run_build(log_dir=logs)
        self.assertEqual(ret, 0)
        self.assertNotIn("one", out)

        def read(name):
            with open(os.path.join(logs, name)) as f:
                return f.read()
        self.assertEqual(read("rootfs.log"), "one\nthree\n")
        self.assertEqual(read("package:amd64:g++.log"), "two\n")
        self.assertEqual(read("build.log"), "system\n")
        self.assertEqual(read(".._escape.log"), "evil\n")
        self.assertFalse(os.path.exists(os.path.join(self.tmp_dir, "escape.log")))

    def test_no_log_dir_writes_no_files(self):
        self.ws.items = [json.dumps({"text": "x\n", "task": "rootfs"})]
        self.run_build()
        self.assertFalse(os.path.exists(os.path.join(self.tmp_dir, "rootfs.log")))

    def test_events_are_ignored_without_on_event(self):
        self.ws.items = [json.dumps({"type": "say", "text": "x"}), log("hello\n")]
        ret, out, _ = self.run_build()
        self.assertEqual(ret, 0)
        self.assertIn("hello", out)

    def test_stream_unreachable_still_follows_build(self):
        self.ws.connect.side_effect = OSError("connection refused")
        ret, _, err = self.run_build()
        self.assertEqual(ret, 0)
        self.assertIn("cannot open the log stream", err)

    def test_no_token_fails_before_any_network_call(self):
        code, _, err = self.run_build(token=None)
        self.assertEqual(code, 2)
        self.assertIn("--token", err)
        self.assertEqual(self.network_calls(), 0)
        self.pack_mock.assert_not_called()

    def test_token_from_environment(self):
        with mock.patch.dict(os.environ, {"SEINE_TOKEN": "env-token"}):
            code, _, _ = self.run_build(token=None)
        self.assertEqual(code, 0)
        self.assertEqual(self.submitted()["headers"]["Authorization"], "Bearer env-token")

    def prompting(self, *tokens, save=True):
        """A prompt handing out *tokens* one by one; /me accepts only 'good'."""
        given = iter(tokens)
        calls = []

        def prompt(context, fields, offer_save=False):
            calls.append((context, fields, offer_save))
            return {"token": next(given), "_save": save}

        def get(url, **kwargs):
            if url.endswith("/api/v1/me"):
                self.get_urls.append(url)
                ok = kwargs["headers"]["Authorization"] == "Bearer good"
                return self.response(200 if ok else 401, {"detail": "nope"})
            return self.http_get(url, **kwargs)

        self.get_mock.side_effect = get
        return prompt, calls

    def saved_token(self, token):
        with open(os.environ["SEINE_CREDENTIALS_FILE"], "w") as f:
            json.dump({"seine-token": token}, f)

    def test_token_chain_prefers_the_environment_over_saved_tokens(self):
        from seine.credentials import token_source
        self.saved_token("saved")
        with mock.patch.dict(os.environ, {"SEINE_TOKEN": "from-env"}):
            self.assertEqual(token_source(SERVER).get(), {"token": "from-env"})

    def test_token_chain_skips_an_empty_environment_variable(self):
        from seine.credentials import token_source
        self.saved_token("saved")
        with mock.patch.dict(os.environ, {"SEINE_TOKEN": ""}):
            self.assertEqual(token_source(SERVER).get(), {"token": "saved"})

    def test_saved_token_is_used_without_prompting(self):
        self.saved_token("good")
        prompt, calls = self.prompting()
        code, _, _ = self.run_build(token=None, prompt=prompt)
        self.assertEqual(code, 0)
        self.assertEqual(calls, [])
        self.assertEqual(self.submitted()["headers"]["Authorization"], "Bearer good")

    def test_prompted_token_is_checked_saved_and_used(self):
        prompt, calls = self.prompting("good")
        with mock.patch("seine.credentials._keyring_reachable", return_value=True), \
                mock.patch("seine.credentials._save_to_keyring") as saved:
            code, _, _ = self.run_build(token=None, prompt=prompt)
        self.assertEqual(code, 0)
        self.assertTrue(calls[0][2])
        saved.assert_called_once_with("seine-token", "good")
        self.assertEqual(self.submitted()["headers"]["Authorization"], "Bearer good")

    def test_rejected_token_is_asked_again(self):
        prompt, calls = self.prompting("bad", "good")
        code, _, _ = self.run_build(token=None, prompt=prompt)
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 2)

    def test_gives_up_after_three_rejected_tokens(self):
        prompt, calls = self.prompting("bad1", "bad2", "bad3", "good")
        with mock.patch("seine.credentials._save_to_keyring") as saved:
            code, _, err = self.run_build(token=None, prompt=prompt)
        self.assertEqual(code, 2)
        self.assertIn("3 tokens rejected", err)
        self.assertEqual(len(calls), 3)
        saved.assert_not_called()
        self.pack_mock.assert_not_called()

    def test_declined_save_writes_nothing(self):
        prompt, _ = self.prompting("good", save=False)
        with mock.patch("seine.credentials._keyring_reachable", return_value=True), \
                mock.patch("seine.credentials._save_to_keyring") as saved:
            code, _, _ = self.run_build(token=None, prompt=prompt)
        self.assertEqual(code, 0)
        saved.assert_not_called()

    def test_explicit_token_is_not_probed_or_prompted(self):
        prompt = mock.Mock()
        self.run_build(prompt=prompt)
        prompt.assert_not_called()
        self.assertFalse([u for u in self.get_urls if u.endswith("/api/v1/me")])

    def serving(self, profile, projects=()):
        """Answer /me and /projects with these; any other GET as usual."""
        self.me_calls = 0

        def get(url, **kwargs):
            if url.endswith("/api/v1/me"):
                self.me_calls += 1
                return self.response(200, profile)
            if url.endswith("/api/v1/projects"):
                return self.response(200, [{"name": n} for n in projects])
            return self.http_get(url, **kwargs)

        self.get_mock.side_effect = get

    def project_build(self, ask=None, **kwargs):
        with mock.patch("requests.patch") as patch:
            patch.return_value = self.response(200, {})
            code, out, err = self.run_build(project=None, ask_project=ask, **kwargs)
        return code, out, err, patch

    def test_given_project_is_used_without_asking_the_server(self):
        self.serving({})
        self.run_build(project="given")
        self.assertEqual(self.me_calls, 0)
        self.assertEqual(self.submitted()["json"]["project"], "given")

    def test_server_default_project_is_used(self):
        self.serving({"default_project": "core", "projects": {"core": "developer", "web": "developer"}})
        code, out, _, _ = self.project_build()
        self.assertEqual(code, 0)
        self.assertEqual(self.submitted()["json"]["project"], "core")
        self.assertIn("Project: core (your default project)", out)

    def test_only_project_of_a_member_is_used(self):
        self.serving({"projects": {"core": "developer"}})
        code, _, _, _ = self.project_build()
        self.assertEqual(code, 0)
        self.assertEqual(self.submitted()["json"]["project"], "core")

    def test_member_of_several_projects_is_asked_and_can_keep_the_choice(self):
        roles = {"core": "developer", "web": "releaser"}
        self.serving({"projects": roles})
        asked = []

        def ask(projects):
            asked.append(dict(projects))
            return "web", True

        code, _, _, patch = self.project_build(ask)
        self.assertEqual(code, 0)
        self.assertEqual(asked, [roles])
        self.assertEqual(self.submitted()["json"]["project"], "web")
        self.assertEqual(patch.call_args[1]["json"], {"default_project": "web"})

    def test_choice_is_not_saved_unless_asked_to(self):
        self.serving({"projects": {"core": "developer", "web": "developer"}})
        code, _, _, patch = self.project_build(lambda projects: ("core", False))
        self.assertEqual(code, 0)
        patch.assert_not_called()

    def test_no_default_and_nobody_to_ask_stops_before_uploading(self):
        self.serving({"projects": {"core": "developer", "web": "developer"}})
        code, _, err, _ = self.project_build()
        self.assertEqual(code, 2)
        self.assertIn("--project", err)
        self.pack_mock.assert_not_called()

    def test_administrator_is_asked_even_with_a_single_project(self):
        self.serving({"is_admin": True, "projects": {"core": "admin"}},
                     projects=["core", "web", "tools"])
        asked = []

        def ask(projects):
            asked.append(dict(projects))
            return "tools", False

        code, _, _, _ = self.project_build(ask)
        self.assertEqual(code, 0)
        self.assertEqual(asked, [{"core": "admin", "web": "admin", "tools": "admin"}])
        self.assertEqual(self.submitted()["json"]["project"], "tools")

    def test_member_of_no_project_cannot_build(self):
        self.serving({"projects": {}})
        code, _, err, _ = self.project_build(lambda projects: ("x", False))
        self.assertEqual(code, 2)
        self.assertIn("not a member of any project", err)

    def test_server_without_profile_endpoint_asks_for_an_explicit_project(self):
        self.get_mock.side_effect = lambda url, **kw: self.response(404, {})
        code, _, err, _ = self.project_build()
        self.assertEqual(code, 2)
        self.assertIn("--project NAME", err)

    def test_server_that_does_not_know_defaults_still_works(self):
        self.serving({"id": "alice", "is_admin": False, "projects": {"core": "developer"}})
        code, _, _, _ = self.project_build()
        self.assertEqual(code, 0)

    def test_failing_to_save_the_default_only_warns(self):
        self.serving({"projects": {"core": "developer", "web": "developer"}})
        with mock.patch("requests.patch") as patch:
            patch.return_value = self.response(404, {})
            code, _, err = self.run_build(
                project=None, ask_project=lambda projects: ("core", True))
        self.assertEqual(code, 0)
        self.assertIn("could not save the default project", err)

    def test_closed_input_while_asking_is_an_error(self):
        self.serving({"projects": {"core": "developer", "web": "developer"}})

        def ask(projects):
            raise EOFError

        code, _, err, _ = self.project_build(ask)
        self.assertEqual(code, 2)
        self.assertIn("no project chosen", err)

    def test_terminal_project_prompt(self):
        projects = {"web": "releaser", "core": "developer"}
        for answers, expected in (
            (["1", "n"], ("core", False)),
            (["web", "y"], ("web", True)),
            (["9", "nope", "2", ""], ("web", False)),
        ):
            with mock.patch("builtins.input", side_effect=answers), \
                    mock.patch("sys.stderr", io.StringIO()) as err:
                self.assertEqual(remote._tty_project_prompt(projects), expected)
            self.assertIn("1) core (developer)", err.getvalue())

    def test_architecture_from_spec(self):
        code, out, _ = self.run_build()
        self.assertEqual(self.submitted()["json"]["target_arch"], "arm64")
        self.assertIn("Target architecture: arm64 (from main.yaml)", out)

    def test_architecture_from_required_fragment(self):
        with open(os.path.join(self.tmp_dir, "arch.yaml"), "w") as f:
            f.write("distribution:\n  architecture: armhf\n")
        self.write_spec("requires:\n  - arch\n")
        self.run_build()
        self.assertEqual(self.submitted()["json"]["target_arch"], "armhf")

    def test_architecture_override(self):
        _, out, _ = self.run_build(options={"target_arch": "amd64"})
        self.assertEqual(self.submitted()["json"]["target_arch"], "amd64")
        self.assertIn("(from the command line)", out)

    def test_architecture_falls_back_to_host(self):
        self.write_spec("distribution:\n  release: bookworm\n")
        with mock.patch("seine.utils.HOST_ARCH", "riscv64"):
            _, out, _ = self.run_build()
        self.assertEqual(self.submitted()["json"]["target_arch"], "riscv64")
        self.assertIn("riscv64 (host architecture", out)

    def test_requires_text_does_not_pick_architecture(self):
        with open(os.path.join(self.tmp_dir, "cross-arm64-tools.yaml"), "w") as f:
            f.write("distribution:\n  release: bookworm\n")
        self.write_spec("requires:\n  - cross-arm64-tools\n")
        with mock.patch("seine.utils.HOST_ARCH", "amd64"):
            self.run_build()
        self.assertEqual(self.submitted()["json"]["target_arch"], "amd64")

    def test_unreadable_spec_is_an_error(self):
        for text in ("distribution: [unclosed\n", "- just\n- a list\n"):
            self.write_spec(text)
            code, _, err = self.run_build()
            self.assertEqual(code, 2)
            self.assertIn("cannot read specification", err)
        os.remove(self.spec)
        code, _, err = self.run_build()
        self.assertEqual(code, 2)
        self.assertIn("cannot read specification", err)
        self.assertEqual(self.network_calls(), 0)

    def test_spec_architecture_none_when_unset(self):
        self.write_spec("distribution:\n  release: bookworm\n")
        self.assertIsNone(spec_architecture(self.spec))

    def test_plain_http_to_remote_host_refused(self):
        code, _, err = self.run_build(server_url="http://server.example:8000")
        self.assertEqual(code, 2)
        self.assertIn("--insecure", err)
        self.assertEqual(self.network_calls(), 0)

    def test_plain_http_allowed_with_insecure(self):
        code, _, _ = self.run_build(
            server_url="http://server.example:8000", options={"insecure": True})
        self.assertEqual(code, 0)

    def test_no_worktree_digest_from_server_is_an_error(self):
        self.upload_mock.return_value = {"status": "staged"}
        code, _, err = self.run_build()
        self.assertEqual(code, 2)
        self.assertIn("no digest", err)
        self.assertEqual(self.posts, [])

    def test_upload_refused_exits_two(self):
        self.upload_mock.side_effect = remote.RemoteError("worktree upload: authentication failed")
        code, _, err = self.run_build()
        self.assertEqual(code, 2)
        self.assertIn("authentication failed", err)

    def test_upload_unreachable_exits_two(self):
        self.upload_mock.side_effect = requests.ConnectionError("refused")
        code, _, err = self.run_build()
        self.assertEqual(code, 2)
        self.assertIn("cannot reach", err)

    def test_client_options_are_not_sent_to_server(self):
        self.run_build(options={"token": "secret", "ca_cert": "/ca", "insecure": True,
                                "dest_dir": "/d", "min_arch_score": 0.5})
        options = self.submitted()["json"]["options"]
        self.assertEqual(options, {"min_arch_score": 0.5})

    def test_only_options_the_server_reads_are_sent(self):
        self.run_build(options={"packages_only": True, "s3_cache": True, "verbose": True,
                                "sign_key": "k", "project": "demo", "build": True})
        options = self.submitted()["json"]["options"]
        self.assertEqual(options, {"packages_only": True, "s3_cache": True, "verbose": True})

    def test_verbose_off_is_not_sent(self):
        self.run_build(options={"verbose": False})
        self.assertEqual(self.submitted()["json"]["options"], {})

    def test_failed_build_exits_one_and_lists_artifacts(self):
        self.statuses = ["running", "failed"]
        self.build_extra = {"artifact_urls": ["artifacts/proj/log.txt"]}
        code, out, _ = self.run_build()
        self.assertEqual(code, 1)
        self.assertIn("finished with status: FAILED", out)
        self.assertEqual(out.count("Artifacts:"), 1)
        self.assertIn("  - artifacts/proj/log.txt", out)

    def test_failed_build_prints_the_servers_reason(self):
        self.statuses = ["running", "failed"]
        self.build_extra = {"error_message": "no S3 dev credentials configured for project 'p'"}
        code, out, _ = self.run_build()
        self.assertEqual(code, 1)
        self.assertIn("no S3 dev credentials configured for project 'p'", out)

    def test_cancelled_by_someone_else_exits_one(self):
        self.statuses = ["running", "cancelled"]
        code, out, _ = self.run_build()
        self.assertEqual(code, 1)
        self.assertIn("CANCELLED", out)

    def test_completed_build_lists_artifacts_without_download(self):
        self.build_extra = {"artifact_urls": ["artifacts/proj/img.raw"]}
        code, out, _ = self.run_build(options={"no_download": True})
        self.assertEqual(code, 0)
        self.assertEqual(out.count("Artifacts:"), 1)
        self.assertIn("  - artifacts/proj/img.raw", out)

    def test_print_artifacts(self):
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            _print_artifacts([])
            _print_artifacts(None)
        self.assertEqual(out.getvalue(), "")
        with mock.patch("sys.stdout", out):
            _print_artifacts(["a", "b"])
        self.assertEqual(out.getvalue(), "\nArtifacts:\n  - a\n  - b\n")

    def test_authentication_error_while_polling_exits_two(self):
        for status in (401, 403, 404):
            self.statuses = [status]
            code, _, err = self.run_build()
            self.assertEqual(code, 2, status)
            self.assertTrue(err.startswith("error:"), status)

    def test_poll_failures_are_retried(self):
        self.statuses = [requests.ConnectionError("blip"), 502, "completed"]
        code, _, _ = self.run_build()
        self.assertEqual(code, 0)

    def test_poll_gives_up_when_server_stays_away(self):
        self.statuses = [requests.ConnectionError("down")]
        with mock.patch.object(remote, "MAX_POLL_FAILURES", 3):
            code, _, err = self.run_build(sleep=lambda s: None)
        self.assertEqual(code, 2)
        self.assertIn("lost contact", err)

    def interrupting_sleep(self, times=1):
        calls = []

        def sleep(seconds):
            calls.append(seconds)
            if len(calls) <= times:
                raise KeyboardInterrupt()
            time.sleep(0.01)
        return sleep

    def test_ctrl_c_cancels_and_exits_130(self):
        self.statuses = ["running", "running", "cancelled"]
        code, out, _ = self.run_build(sleep=self.interrupting_sleep())
        self.assertEqual(code, 130)
        cancel_url, kw = self.posts[-1]
        self.assertEqual(cancel_url, f"{SERVER}/api/v1/builds/{BUILD}/cancel")
        self.assertEqual(kw["headers"]["Authorization"], "Bearer pat-test")
        self.assertIn("requesting cancellation", out)
        self.assertIn("finished with status: CANCELLED", out)

    def test_ctrl_c_waits_at_most_cancel_wait(self):
        self.statuses = ["running"]
        now = [0.0]

        def clock():
            now[0] += 10.0
            return now[0]

        code, out, _ = self.run_build(
            sleep=self.interrupting_sleep(), clock=clock, cancel_wait=30.0)
        self.assertEqual(code, 130)
        self.assertIn("has not stopped yet", out)

    def test_ctrl_c_with_failed_cancel_request(self):
        self.statuses = ["running"]
        self.cancel_status = requests.ConnectionError("refused")
        code, _, err = self.run_build(sleep=self.interrupting_sleep())
        self.assertEqual(code, 130)
        self.assertIn("cancel request failed", err)

    def test_ctrl_c_with_rejected_cancel_request(self):
        self.statuses = ["running"]
        self.cancel_status = 403
        code, _, err = self.run_build(sleep=self.interrupting_sleep())
        self.assertEqual(code, 130)
        self.assertIn("cancel request failed", err)

    def test_second_ctrl_c_exits_immediately(self):
        self.statuses = ["running"]
        code, out, _ = self.run_build(sleep=self.interrupting_sleep(times=99))
        self.assertEqual(code, 130)
        self.assertIn("Not waiting", out)

    def test_ctrl_c_before_submission_does_not_cancel(self):
        self.upload_mock.side_effect = KeyboardInterrupt()
        code, _, _ = self.run_build()
        self.assertEqual(code, 130)
        self.assertEqual(self.posts, [])

    def test_build_remote_needs_a_spec(self):
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            self.assertEqual(build_remote(SERVER, spec_files=[]), 2)
        self.assertEqual(self.network_calls(), 0)

    def test_build_remote_entry_point(self):
        with mock.patch("sys.stdout", io.StringIO()):
            code = build_remote(
                SERVER, project="proj", spec_files=[self.spec], token="pat-test",
                is_release=True, root_dir=self.tmp_dir)
        self.assertEqual(code, 0)
        self.assertTrue(self.submitted()["json"]["is_release"])

    @mock.patch("seine.distributed.client.remote.build_remote")
    def test_build_cmd_cli_remote_dispatch(self, mock_build):
        from seine.build import BuildCmd

        mock_build.return_value = 0
        with self.assertRaises(SystemExit) as ctx:
            BuildCmd().main([
                "--remote", "http://server:8000",
                "--project", "clicmd-proj",
                "--token", "secret-token",
                "--release",
                self.spec,
            ])
        self.assertEqual(ctx.exception.code, 0)
        kwargs = mock_build.call_args[1]
        self.assertEqual(kwargs["server_url"], "http://server:8000")
        self.assertEqual(kwargs["project"], "clicmd-proj")
        self.assertEqual(kwargs["token"], "secret-token")
        self.assertEqual(kwargs["is_release"], True)

    @mock.patch("seine.distributed.client.remote.build_remote")
    def test_build_cmd_cli_leaves_the_project_to_the_server_default(self, mock_build):
        from seine.build import BuildCmd

        mock_build.return_value = 0
        with mock.patch.dict(os.environ):
            os.environ.pop("SEINE_PROJECT", None)
            with self.assertRaises(SystemExit):
                BuildCmd().main(["--remote", "http://server:8000", self.spec])
        self.assertIsNone(mock_build.call_args[1]["project"])

    @mock.patch("seine.distributed.client.remote.build_remote")
    def test_build_cmd_cli_project_from_the_environment(self, mock_build):
        from seine.build import BuildCmd

        mock_build.return_value = 0
        with mock.patch.dict(os.environ, {"SEINE_PROJECT": "from-env"}):
            with self.assertRaises(SystemExit):
                BuildCmd().main(["--remote", "http://server:8000", self.spec])
            self.assertEqual(mock_build.call_args[1]["project"], "from-env")
            with self.assertRaises(SystemExit):
                BuildCmd().main(["--remote", "http://server:8000",
                                 "--project", "flag", self.spec])
            self.assertEqual(mock_build.call_args[1]["project"], "flag")

    @mock.patch("seine.distributed.client.remote.build_remote")
    def test_build_cmd_cli_tls_options(self, mock_build):
        from seine.build import BuildCmd

        mock_build.return_value = 0
        with self.assertRaises(SystemExit):
            BuildCmd().main([
                "--remote", "http://server:8000",
                "--ca-cert", "/etc/ca.pem", "--insecure", self.spec,
            ])
        options = mock_build.call_args[1]["options"]
        self.assertEqual(options["ca_cert"], "/etc/ca.pem")
        self.assertIs(options["insecure"], True)
