# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0
"""Tests for the feed credentials a remote build sends along with the build request."""

import io
import os
import shutil
import tempfile
from unittest import mock

from avocado import Test

from seine import credentials
from seine.distributed.client import remote
from seine.distributed.client.remote import RemoteBuild

SPEC = """\
distribution:
  architecture: arm64
  uri: https://repo.example/debian
  feeds:
    - suite: bookworm
    - suite: corp
      uri: https://corp.example/apt/
      auth:
        login: env:FEED_LOGIN
        password: env:FEED_PASSWORD
"""
PLAIN_SPEC = "distribution:\n  architecture: arm64\n"
ENV = {"FEED_LOGIN": "alice", "FEED_PASSWORD": "hunter2", "SEINE_TOKEN": "", "SEINE_CA_CERT": "",
       # the user's own credentials file must not leak into a test
       "SEINE_CREDENTIALS_FILE": "/nonexistent/seine-credentials.json"}
SENT = {"feeds": {"https://corp.example/apt": {"login": "alice", "password": "hunter2"}}}


class Harness:
    """A remote build against a fake server; the subclasses hold the tests."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="seine-test-feed-client-")
        self.spec = os.path.join(self.tmp_dir, "main.yaml")
        self.write(SPEC)
        self.posts = []
        archive = os.path.join(self.tmp_dir, "bundle.tar.zst")

        def pack(root):
            with open(archive, "wb") as f:
                f.write(b"zst")
            return archive, "digest"

        def post(url, **kwargs):
            self.posts.append((url, kwargs))
            resp = mock.MagicMock(status_code=200)
            resp.json.return_value = {"build_id": "bld-1", "status": "queued",
                                      "project": "proj", "target_arch": "arm64"}
            return resp

        self.patches = [
            mock.patch("seine.distributed.client.worktree.pack_worktree", side_effect=pack),
            mock.patch.object(remote, "upload_worktree", return_value={"digest": "dig"}),
            mock.patch("requests.post", side_effect=post),
            mock.patch.dict(os.environ, ENV),
            mock.patch("seine.credentials.probe", return_value=True),
            mock.patch.object(RemoteBuild, "_start_log_stream"),
            mock.patch.object(RemoteBuild, "_wait", side_effect=KeyboardInterrupt),
            mock.patch.object(RemoteBuild, "_cancel", return_value=130),
        ]
        for p in self.patches:
            p.start()
        self.probe = credentials.probe

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def write(self, text):
        with open(self.spec, "w") as f:
            f.write(text)

    def run_build(self, url="https://server.example", **options):
        build = RemoteBuild(url, "proj", self.spec, options=options, token="pat",
                            root_dir=self.tmp_dir)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            return build.run(), err.getvalue()

    def sent(self):
        return [kw["json"] for url, kw in self.posts if url.endswith("/api/v1/builds")][0]



class ClientTest(Harness, Test):
    def test_credentials_are_resolved_as_locally_and_sent_in_the_expected_shape(self):
        self.run_build()
        self.assertEqual(self.sent()["transient_secrets"], SENT)
        self.probe.assert_called_once()
        self.assertEqual(self.probe.call_args.args[:2], ("https://corp.example/apt/", "corp"))

    def test_the_local_chains_are_used_including_the_prompt(self):
        prompt = mock.Mock(return_value={"login": "bob", "password": "typed"})
        env = {k: v for k, v in os.environ.items() if not k.startswith("FEED_")}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch("seine.build.credentials._default_prompt", return_value=prompt):
            self.run_build()
        prompt.assert_called_once()
        self.assertEqual(self.sent()["transient_secrets"]["feeds"]["https://corp.example/apt"],
                         {"login": "bob", "password": "typed"})

    def test_a_spec_without_authenticated_feeds_sends_nothing(self):
        self.write(PLAIN_SPEC)
        self.run_build()
        self.assertEqual(self.sent()["transient_secrets"], {})
        self.probe.assert_not_called()

    def test_a_rejected_credential_aborts_before_anything_is_sent(self):
        prompt = mock.Mock(return_value={"login": "a", "password": "b"})
        with mock.patch("seine.credentials.probe", return_value=False), \
                mock.patch("seine.build.credentials._default_prompt", return_value=prompt):
            code, err = self.run_build()
        self.assertEqual(code, 3)
        self.assertEqual(self.posts, [])
        self.assertIn("feed credentials", err)
        self.assertNotIn("hunter2", err)

    def test_an_unreachable_feed_aborts_before_anything_is_sent(self):
        with mock.patch("seine.credentials.probe", side_effect=credentials.CredentialError("down")):
            code, err = self.run_build()
        self.assertEqual(code, 3)
        self.assertEqual(self.posts, [])

    def test_a_missing_credential_without_a_prompt_aborts(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("FEED_")}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch("seine.build.credentials._default_prompt", return_value=None):
            code, _ = self.run_build()
        self.assertEqual(code, 3)
        self.assertEqual(self.posts, [])

    def test_secrets_are_never_sent_over_plain_http_to_a_remote_host(self):
        code, _ = self.run_build(url="http://server.example:8000")
        self.assertEqual(code, 2)
        self.assertEqual(self.posts, [])

    def test_insecure_does_not_allow_sending_secrets_over_plain_http(self):
        code, err = self.run_build(url="http://server.example:8000", insecure=True)
        self.assertEqual(code, 2)
        self.assertIn("refusing to send feed credentials", err)
        self.assertEqual(self.posts, [])

    def test_plain_http_to_the_loopback_may_carry_them(self):
        self.run_build(url="http://127.0.0.1:8000")
        self.assertEqual(self.sent()["transient_secrets"], SENT)

    def test_plain_http_is_still_allowed_for_a_build_without_secrets(self):
        self.write(PLAIN_SPEC)
        self.run_build(url="http://server.example:8000", insecure=True)
        self.assertEqual(self.sent()["transient_secrets"], {})

    def test_resolved_values_do_not_outlive_the_request(self):
        self.run_build()
        self.assertIsNone(credentials.resolved_for("https://corp.example/apt"))


class OwnArtifactoryCredentialTest(Harness, Test):
    """The user's own Artifactory credential goes with the build, only to a server that runs Artifactory."""

    def setUp(self):
        super().setUp()
        self.write(PLAIN_SPEC)
        self.profile = {"storage": {"type": "artifactory", "endpoint": "https://arti.lan"}}
        self.gets = []
        self.have = {"token": "user-tok"}

        def get(url, **kwargs):
            self.gets.append(url)
            resp = mock.MagicMock(status_code=200)
            resp.json.return_value = self.profile
            return resp

        def source(auth=None, **kwargs):
            src = mock.Mock()
            wants_pair = auth is not None and "user" in auth
            if self.have and (("user" in self.have) == wants_pair):
                src.get.return_value = dict(self.have)
            else:
                src.get.side_effect = credentials.CredentialNotFound("none")
            return src

        for p in (mock.patch("requests.get", side_effect=get),
                  mock.patch("seine.credentials.artifactory_credential_source", side_effect=source)):
            p.start()
            self.patches.append(p)

    def test_a_token_is_sent_to_a_server_that_runs_artifactory(self):
        self.run_build()
        self.assertEqual(self.sent()["transient_secrets"], {"artifactory": {"token": "user-tok"}})

    def test_a_user_and_password_are_sent_when_there_is_no_token(self):
        self.have = {"user": "alice", "password": "pw"}
        self.run_build()
        self.assertEqual(self.sent()["transient_secrets"], {"artifactory": {"user": "alice", "password": "pw"}})

    def test_it_travels_with_the_feed_credentials(self):
        self.write(SPEC)
        self.run_build()
        self.assertEqual(self.sent()["transient_secrets"], {**SENT, "artifactory": {"token": "user-tok"}})

    def test_an_s3_server_never_gets_it(self):
        self.profile = {"storage": {"type": "s3"}}
        self.run_build()
        self.assertEqual(self.sent()["transient_secrets"], {})

    def test_a_user_without_a_credential_costs_no_request_and_sends_nothing(self):
        self.have = None
        self.run_build()
        self.assertEqual(self.sent()["transient_secrets"], {})
        self.assertFalse([u for u in self.gets if u.endswith("/api/v1/me")])

    def test_it_is_never_sent_over_plain_http(self):
        code, err = self.run_build(url="http://server.example", insecure=True)
        self.assertNotEqual(code, 0)
        self.assertEqual([u for u, _ in self.posts if u.endswith("/api/v1/builds")], [])
        self.assertIn("refusing to send your Artifactory credential", err)
        self.assertNotIn("user-tok", err)
