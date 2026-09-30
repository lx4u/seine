#!/usr/bin/env python3
# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import avocado
import os
import sys

from unittest import mock

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine.build import collect_credentials
from seine.credentials import CredentialNotFound


class _FakeBuild:
    def __init__(self, spec, vault_reader=None):
        self.spec = spec
        self._vault_lookup = vault_reader or (lambda ref: None)


DISTRO = {"source": "debian", "release": "bookworm", "architecture": "amd64",
          "uri": "http://example.com/debian"}
AUTH = {"login": "env:LOGIN", "password": "env:PASS"}


def _spec(feed_entries):
    return {"distribution": dict(DISTRO, feeds=feed_entries)}


def _no_env():
    env = dict(os.environ)
    env.pop("LOGIN", None)
    env.pop("PASS", None)
    return env


class OnlyAuthenticatedFeedsAreResolved(avocado.Test):
    def test(self):
        build = _FakeBuild(_spec([
            {"suite": "bookworm"},
            {"suite": "bookworm-security", "uri": "http://s.example.com/debian",
             "auth": dict(AUTH)},
        ]))
        with mock.patch.dict(os.environ, {"LOGIN": "a", "PASS": "b"}):
            with mock.patch("seine.credentials.probe", return_value=True) as probe:
                collect_credentials([build])
        probe.assert_called_once()
        self.assertEqual(probe.call_args[0][0], "http://s.example.com/debian")


class SharedChainIsPromptedAndProbedOnce(avocado.Test):
    def test(self):
        build = _FakeBuild(_spec([
            {"suite": "bookworm", "auth": dict(AUTH)},
            {"suite": "bookworm-updates", "auth": dict(AUTH)},
        ]))
        prompt = mock.Mock(return_value={"login": "a", "password": "b"})
        with mock.patch.dict(os.environ, _no_env(), clear=True):
            with mock.patch("seine.credentials.probe", return_value=True) as probe:
                collect_credentials([build], prompt=prompt)
        prompt.assert_called_once()
        probe.assert_called_once()  # "first suite only" (decision 13)


class RejectionRepromptsOnlyThatFeed(avocado.Test):
    def test(self):
        build = _FakeBuild(_spec([{"suite": "bookworm", "auth": dict(AUTH)}]))
        prompt = mock.Mock(side_effect=[
            {"login": "a", "password": "wrong"},
            {"login": "a", "password": "right"},
        ])
        with mock.patch.dict(os.environ, _no_env(), clear=True):
            with mock.patch("seine.credentials.probe",
                            side_effect=[False, True]) as probe:
                collect_credentials([build], prompt=prompt)
        self.assertEqual(probe.call_count, 2)
        self.assertEqual(prompt.call_count, 2)


class CommitRunsOnceProbeSucceeds(avocado.Test):
    def test(self):
        build = _FakeBuild(_spec([{"suite": "bookworm", "auth": dict(AUTH)}]))
        prompt = mock.Mock(return_value={"login": "a", "password": "b"})
        with mock.patch.dict(os.environ, _no_env(), clear=True):
            with mock.patch("seine.credentials.probe", return_value=True):
                with mock.patch(
                        "seine.credentials.CredentialSource.commit") as commit:
                    collect_credentials([build], prompt=prompt)
        commit.assert_called_once()


class NonInteractiveMissWithNoPromptIsAClearError(avocado.Test):
    def test(self):
        build = _FakeBuild(_spec([{"suite": "bookworm", "auth": dict(AUTH)}]))
        with mock.patch.dict(os.environ, _no_env(), clear=True):
            with self.assertRaises(CredentialNotFound):
                collect_credentials([build], prompt=None)


class ProbeFalseSkipsTheNetworkCheck(avocado.Test):
    def test(self):
        build = _FakeBuild(_spec([
            {"suite": "bookworm", "auth": dict(AUTH, probe=False)}]))
        with mock.patch.dict(os.environ, {"LOGIN": "a", "PASS": "b"}):
            with mock.patch("seine.credentials.probe") as probe:
                collect_credentials([build])
        probe.assert_not_called()


class SecretRedaction(avocado.Test):
    def test_password_always_hidden_login_only_when_asked(self):
        from seine import vault
        vault.clear_secrets()
        try:
            build = _FakeBuild(_spec([{"suite": "bookworm", "auth": dict(AUTH)}]))
            with mock.patch.dict(os.environ, {"LOGIN": "alice", "PASS": "s3cr3t"}):
                with mock.patch("seine.credentials.probe", return_value=True):
                    collect_credentials([build])
            self.assertIn("s3cr3t", vault.secrets())
            self.assertNotIn("alice", vault.secrets())
        finally:
            vault.clear_secrets()

    def test_login_hidden_when_redact_asks(self):
        from seine import vault
        vault.clear_secrets()
        try:
            spec = _spec([{"suite": "bookworm", "auth": dict(AUTH)}])
            spec["redact"] = [{"path": "distribution.feeds.auth.login"}]
            build = _FakeBuild(spec)
            with mock.patch.dict(os.environ, {"LOGIN": "alice", "PASS": "s3cr3t"}):
                with mock.patch("seine.credentials.probe", return_value=True):
                    collect_credentials([build])
            self.assertIn("alice", vault.secrets())
        finally:
            vault.clear_secrets()


if __name__ == "__main__":
    avocado.main()
