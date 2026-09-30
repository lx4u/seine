#!/usr/bin/env python3
# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import avocado
import os
import stat
import sys
import tempfile

from unittest import mock

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine import credentials
from seine.utils import feed_auth_entries, netrc_for

DISTRO = {"source": "debian", "release": "bookworm", "architecture": "amd64",
          "uri": "http://example.com/debian"}


def distro(feed_entries):
    return dict(DISTRO, feeds=feed_entries)


class FeedAuthEntriesOnlyIncludesResolvedFeeds(avocado.Test):
    def setUp(self):
        credentials.clear_resolved()

    def tearDown(self):
        credentials.clear_resolved()

    def test(self):
        spec = distro([
            {"suite": "bookworm"},
            {"suite": "bookworm-security",
             "uri": "https://s.example.com/debian",
             "auth": {"login": "env:A", "password": "env:B"}},
        ])
        credentials.remember_resolved("https://s.example.com/debian",
                                      "alice", "s3cr3t")
        entries = feed_auth_entries(spec)
        self.assertEqual(entries,
                         [("https://s.example.com/debian", "alice", "s3cr3t")])

    def test_unresolved_auth_feed_is_skipped(self):
        spec = distro([
            {"suite": "bookworm",
             "auth": {"login": "env:A", "password": "env:B"}},
        ])
        self.assertEqual(feed_auth_entries(spec), [])


class NetrcForWritesOneMachineLinePerEntry(avocado.Test):
    def test_https_needs_no_annotation(self):
        with netrc_for([("https://s.example.com/debian/repo", "alice", "s3cr3t")]) as path:
            with open(path) as f:
                text = f.read()
        self.assertIn("machine s.example.com/debian/repo\n", text)
        self.assertIn("\tlogin alice\n", text)
        self.assertIn("\tpassword s3cr3t\n", text)

    def test_http_is_annotated(self):
        with netrc_for([("http://s.example.com/debian", "alice", "s3cr3t")]) as path:
            with open(path) as f:
                text = f.read()
        self.assertIn("machine http://s.example.com/debian\n", text)

    def test_port_is_kept_path_trailing_slash_dropped(self):
        with netrc_for([("http://s.example.com:8080/debian/", "a", "b")]) as path:
            with open(path) as f:
                text = f.read()
        self.assertIn("machine http://s.example.com:8080/debian\n", text)

    def test_no_entries_yields_none(self):
        with netrc_for([]) as path:
            self.assertIsNone(path)

    def test_file_is_0644_and_removed_after(self):
        with netrc_for([("https://s.example.com/debian", "a", "b")]) as path:
            self.assertTrue(os.path.isfile(path))
            mode = stat.S_IMODE(os.stat(path).st_mode)
            self.assertEqual(mode, 0o644)
            directory = os.path.dirname(path)
            self.assertEqual(stat.S_IMODE(os.stat(directory).st_mode), 0o755)
        self.assertFalse(os.path.isdir(directory))

    def test_removed_even_on_exception(self):
        directory_seen = []
        try:
            with netrc_for([("https://s.example.com/debian", "a", "b")]) as path:
                directory_seen.append(os.path.dirname(path))
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        self.assertFalse(os.path.isdir(directory_seen[0]))

    def test_uses_xdg_runtime_dir_when_set(self):
        with tempfile.TemporaryDirectory() as runtime_dir:
            with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": runtime_dir}):
                with netrc_for([("https://s.example.com/debian", "a", "b")]) as path:
                    self.assertTrue(path.startswith(
                        os.path.join(runtime_dir, "seine")))


class TargetBootstrapDockerfileGetsTheMountAndAptopt(avocado.Test):
    def setUp(self):
        credentials.clear_resolved()

    def tearDown(self):
        credentials.clear_resolved()

    def test_no_auth_no_mount(self):
        from seine.bootstrap import HostBootstrap, TargetBootstrap
        spec = distro([{"suite": "bookworm"}])
        target = TargetBootstrap(spec, {})
        target.hostBootstrap = HostBootstrap(spec, {})
        text = target.dockerfile()
        self.assertNotIn("seine-netrc", text)
        self.assertNotIn("Dir::Etc::netrc", text)

    def test_resolved_auth_adds_mount_and_aptopt(self):
        from seine.bootstrap import HostBootstrap, TargetBootstrap
        spec = distro([{"suite": "bookworm",
                        "auth": {"login": "env:A", "password": "env:B"}}])
        credentials.remember_resolved(spec["uri"], "alice", "s3cr3t")
        target = TargetBootstrap(spec, {})
        target.hostBootstrap = HostBootstrap(spec, {})
        text = target.dockerfile()
        self.assertIn(
            "--mount=type=secret,id=seine-netrc,target=/run/seine/netrc", text)
        self.assertIn('Dir::Etc::netrc "/run/seine/netrc"', text)


class BuilderImageDockerfileGetsTheMountAndAptopt(avocado.Test):
    def setUp(self):
        credentials.clear_resolved()

    def tearDown(self):
        credentials.clear_resolved()

    def test_no_auth_no_mount(self):
        from seine.sbuild import BuilderImage
        spec = distro([{"suite": "bookworm"}])
        host = type("Host", (), {"name": "bootstrap/host"})()
        text = BuilderImage(spec, {}).dockerfile(host)
        self.assertNotIn("seine-netrc", text)
        self.assertNotIn("Dir::Etc::netrc", text)

    def test_resolved_auth_adds_mount_and_aptopt(self):
        from seine.sbuild import BuilderImage
        spec = distro([{"suite": "bookworm",
                        "auth": {"login": "env:A", "password": "env:B"}}])
        credentials.remember_resolved(spec["uri"], "alice", "s3cr3t")
        host = type("Host", (), {"name": "bootstrap/host"})()
        text = BuilderImage(spec, {}).dockerfile(host)
        self.assertIn(
            "--mount=type=secret,id=seine-netrc,target=/run/seine/netrc", text)
        self.assertIn('Dir::Etc::netrc="/run/seine/netrc"', text)


class TransportBootstrapDockerfileGetsTheMountAndAptopt(avocado.Test):
    def setUp(self):
        credentials.clear_resolved()

    def tearDown(self):
        credentials.clear_resolved()

    def test_no_auth_no_mount(self):
        from seine.transport_bootstrap import TransportBootstrap
        spec = distro([{"suite": "bookworm"}])
        text = TransportBootstrap("debian:bookworm", spec, {}).dockerfile("", "")
        self.assertNotIn("seine-netrc", text)
        self.assertNotIn("Dir::Etc::netrc", text)

    def test_resolved_auth_adds_mount_and_aptopt(self):
        from seine.transport_bootstrap import TransportBootstrap
        spec = distro([{"suite": "bookworm",
                        "auth": {"login": "env:A", "password": "env:B"}}])
        credentials.remember_resolved(spec["uri"], "alice", "s3cr3t")
        text = TransportBootstrap("debian:bookworm", spec, {}).dockerfile("", "")
        self.assertIn(
            "--mount=type=secret,id=seine-netrc,target=/run/seine/netrc", text)
        self.assertIn('Dir::Etc::netrc="/run/seine/netrc"', text)


if __name__ == "__main__":
    avocado.main()
