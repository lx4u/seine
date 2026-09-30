# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0
"""Tests for a build that is handed its feed credentials instead of resolving them."""

import json
import os
import shutil
import tempfile
from unittest import mock

from avocado import Test

from seine import credentials, vault
from seine.build import collect_credentials
from seine.credentials import CredentialError, CredentialNotFound, FEED_AUTH_ENV

DISTRO = {"source": "debian", "release": "bookworm", "architecture": "amd64",
          "uri": "https://repo.example/debian"}
AUTH = {"login": "env:LOGIN", "password": "env:PASS"}


class FakeBuild:
    def __init__(self, feeds):
        self.spec = {"distribution": dict(DISTRO, feeds=feeds)}
        self._vault_lookup = mock.Mock(side_effect=AssertionError("vault must not be read"))


class DelegatedTest(Test):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="seine-test-delegated-")
        self.path = os.path.join(self.tmp_dir, "feeds.json")
        self.patches = []

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        vault.clear_secrets()
        credentials.clear_resolved()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def delegate(self, data):
        with open(self.path, "w") as f:
            json.dump(data, f)
        patch = mock.patch.dict(os.environ, {FEED_AUTH_ENV: self.path})
        patch.start()
        self.patches.append(patch)

    def feeds(self, *uris):
        return FakeBuild([{"suite": f"s{i}", "uri": uri, "auth": dict(AUTH)}
                          for i, uri in enumerate(uris)])

    def test_the_delegated_pair_is_used_without_any_chain_or_prompt(self):
        self.delegate({"https://corp.example/apt": {"login": "alice", "password": "hunter2"}})
        prompt = mock.Mock()
        env = {k: v for k, v in os.environ.items() if k not in ("LOGIN", "PASS")}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch("seine.credentials._resolve_keyring") as keyring, \
                mock.patch("seine.credentials.probe", return_value=True) as probe:
            os.environ[FEED_AUTH_ENV] = self.path
            collect_credentials([self.feeds("https://corp.example/apt/")], prompt=prompt)
        prompt.assert_not_called()
        keyring.assert_not_called()
        self.assertEqual(probe.call_args.args[2:4], ("alice", "hunter2"))
        self.assertEqual(credentials.resolved_for("https://corp.example/apt"), ("alice", "hunter2"))
        self.assertIn("hunter2", vault.secrets())

    def test_every_feed_gets_its_own_pair(self):
        self.delegate({
            "https://a.example/apt": {"login": "a", "password": "pa"},
            "https://b.example/apt": {"login": "b", "password": "pb"},
        })
        with mock.patch("seine.credentials.probe", return_value=True) as probe:
            collect_credentials([self.feeds("https://a.example/apt", "https://b.example/apt")])
        self.assertEqual(probe.call_count, 2)
        self.assertEqual(credentials.resolved_for("https://b.example/apt"), ("b", "pb"))

    def test_a_feed_with_no_entry_fails_clearly_and_never_prompts(self):
        self.delegate({"https://other.example/apt": {"login": "a", "password": "b"}})
        prompt = mock.Mock()
        with self.assertRaises(CredentialNotFound) as ctx:
            collect_credentials([self.feeds("https://corp.example/apt")], prompt=prompt)
        self.assertIn("no credential was delegated", str(ctx.exception))
        prompt.assert_not_called()

    def test_a_rejected_pair_is_not_asked_again(self):
        self.delegate({"https://corp.example/apt": {"login": "a", "password": "b"}})
        prompt = mock.Mock()
        with mock.patch("seine.credentials.probe", return_value=False):
            with self.assertRaises(CredentialNotFound):
                collect_credentials([self.feeds("https://corp.example/apt")], prompt=prompt)
        prompt.assert_not_called()

    def test_an_empty_delegation_still_never_resolves_locally(self):
        self.delegate({})
        with mock.patch.dict(os.environ, {"LOGIN": "x", "PASS": "y"}):
            with self.assertRaises(CredentialNotFound):
                collect_credentials([self.feeds("https://corp.example/apt")])

    def test_a_build_without_authenticated_feeds_needs_no_entry(self):
        self.delegate({})
        collect_credentials([FakeBuild([{"suite": "bookworm"}])])

    def test_the_file_is_checked(self):
        for content in ("not json", "[]", '{"u": {"login": "a"}}', '{"u": {"login": 1, "password": "p"}}'):
            with open(self.path, "w") as f:
                f.write(content)
            with self.assertRaises(CredentialError, msg=content):
                credentials.load_feed_auth(self.path)
        with self.assertRaises(CredentialError):
            credentials.load_feed_auth(os.path.join(self.tmp_dir, "missing.json"))
