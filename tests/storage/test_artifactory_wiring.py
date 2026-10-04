#!/usr/bin/env python3
# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import os
import sys
from unittest import mock
import avocado

path_to_self = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.insert(0, path_to_sources)

from seine import storage
from seine.build import BuildCmd
from seine.doctor import check_artifactory
from seine.storage.artifactory import ArtifactoryStorageProvider
from seine.storage.base import StorageError
from seine.storage.local import LocalStorageProvider
from seine.storage.s3 import S3StorageProvider


def token_creds():
    src = mock.MagicMock()
    src.get.return_value = {"token": "tok"}
    return src


class ArtifactoryFactoryTest(avocado.Test):
    def test_spec_storage_artifactory_selects_provider(self):
        spec = {"storage": {"artifactory": {
            "endpoint": "http://arti:8081", "repo": "spec-repo"}}}
        with mock.patch("seine.credentials.artifactory_credential_source",
                         return_value=token_creds()) as mock_src:
            provider = storage.for_build(spec=spec)
            self.assertIsInstance(provider, ArtifactoryStorageProvider)
            self.assertEqual(provider.repo, "spec-repo")
            self.assertEqual(provider.bucket, "spec-repo")
            mock_src.assert_called_once_with(auth=None)

    def test_options_endpoint_selects_provider(self):
        options = {"shared_cache": True,
                   "artifactory_endpoint": "http://arti:8081",
                   "artifactory_repo": "opt-repo"}
        with mock.patch("seine.credentials.artifactory_credential_source",
                         return_value=token_creds()):
            provider = storage.for_build(options)
            self.assertIsInstance(provider, ArtifactoryStorageProvider)
            self.assertEqual(provider.repo, "opt-repo")

    def test_storage_backend_flag_selects_provider(self):
        options = {"shared_cache": True, "storage_backend": "artifactory",
                   "artifactory_endpoint": "http://arti:8081"}
        with mock.patch("seine.credentials.artifactory_credential_source",
                         return_value=token_creds()):
            provider = storage.for_build(options)
            self.assertIsInstance(provider, ArtifactoryStorageProvider)
            self.assertEqual(provider.repo, "seine-shared")

    def test_user_password_auth(self):
        options = {"shared_cache": True,
                   "artifactory_endpoint": "http://arti:8081"}
        src = mock.MagicMock()
        src.get.return_value = {"user": "u", "password": "p"}
        with mock.patch("seine.credentials.artifactory_credential_source",
                         return_value=src):
            provider = storage.for_build(options)
            self.assertEqual(provider.client._session.auth, ("u", "p"))

    def test_missing_endpoint_raises_storage_error(self):
        with mock.patch("seine.credentials.artifactory_credential_source",
                         return_value=token_creds()):
            with self.assertRaises(StorageError):
                storage.for_build({"shared_cache": True, "storage_backend": "artifactory"},
                                  spec={})

    def test_no_shared_cache_stays_local(self):
        spec = {"storage": {"artifactory": {"endpoint": "http://arti:8081"}}}
        provider = storage.for_build({"shared_cache": False}, spec)
        self.assertIsInstance(provider, LocalStorageProvider)

    def test_s3_untouched_by_default(self):
        spec = {"storage": {"s3": {"endpoint": "http://g:3900"}}}
        with mock.patch("seine.credentials.s3_credential_source") as mock_src:
            mock_src.return_value.get.return_value = {"access_key": "a", "secret_key": "s"}
            provider = storage.for_build(spec=spec)
            self.assertIsInstance(provider, S3StorageProvider)


class BuildCmdArtifactoryOptionsTest(avocado.Test):
    def test_build_cmd_artifactory_options_parsing(self):
        cmd = BuildCmd()
        argv = [
            "--shared-cache",
            "--storage-backend=artifactory",
            "--artifactory-endpoint=http://arti:8081",
            "--artifactory-repo=my-repo",
            "dummy.yaml",
        ]
        with mock.patch.object(cmd, "load_all"), \
             mock.patch.object(cmd, "parse", return_value={}), \
             mock.patch.object(cmd, "build", return_value=0), \
             mock.patch("seine.build.cli.collect_credentials"), \
             mock.patch("seine.build.cli.locked"), \
             mock.patch("seine.build.cli.remember"):
            with self.assertRaises(SystemExit) as cm:
                cmd.main(argv)
            self.assertEqual(cm.exception.code, 0)
            self.assertEqual(cmd.options["storage_backend"], "artifactory")
            self.assertEqual(cmd.options["artifactory_endpoint"], "http://arti:8081")
            self.assertEqual(cmd.options["artifactory_repo"], "my-repo")

    def test_storage_backend_rejects_other(self):
        cmd = BuildCmd()
        with mock.patch.object(cmd, "load_all"), \
             mock.patch.object(cmd, "parse", return_value={}), \
             mock.patch.object(cmd, "build", return_value=0), \
             mock.patch("seine.build.cli.collect_credentials"), \
             mock.patch("seine.build.cli.locked"), \
             mock.patch("seine.build.cli.remember"):
            with self.assertRaises(SystemExit) as cm:
                cmd.main(["--storage-backend=ftp", "dummy.yaml"])
            self.assertNotEqual(cm.exception.code, 0)


class DoctorArtifactoryTest(avocado.Test):
    def test_unconfigured_returns_none(self):
        self.assertIsNone(check_artifactory({}))
        self.assertIsNone(check_artifactory({"spec": {}}))

    def test_reachable_is_ok(self):
        options = {"shared_cache": True, "artifactory_endpoint": "http://arti:8081"}
        src = mock.MagicMock()
        src.get.return_value = {"token": "tok"}
        with mock.patch("seine.credentials.artifactory_credential_source",
                         return_value=src), \
             mock.patch("seine.credentials.probe_artifactory", return_value=True) as probe:
            check = check_artifactory(options)
            self.assertEqual(check.status, "ok")
            probe.assert_called_once_with("http://arti:8081", "seine-shared",
                                          user=None, password=None, token="tok", timeout=5)

    def test_unreachable_is_warn(self):
        options = {"shared_cache": True, "artifactory_endpoint": "http://arti:8081"}
        src = mock.MagicMock()
        src.get.return_value = {"token": "tok"}
        with mock.patch("seine.credentials.artifactory_credential_source",
                         return_value=src), \
             mock.patch("seine.credentials.probe_artifactory", return_value=False):
            check = check_artifactory(options)
            self.assertEqual(check.status, "warn")


if __name__ == "__main__":
    avocado.main()
