#!/usr/bin/env python3
# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for server storage backend dispatch (S3 vs Artifactory)."""

import os
import sys
from unittest import mock
import avocado

path_to_self = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.insert(0, path_to_sources)

from seine.distributed.server import storage as server_storage
from seine.distributed.server.settings import Settings
from seine.storage.artifactory import ArtifactoryStorageProvider
from seine.storage.s3 import S3StorageProvider


def artifactory_settings(**kw):
    values = dict(
        storage_type="artifactory",
        artifactory_endpoint="https://arti.lan:8081",
        artifactory_projects={"core": {"dev": {"token": "dev-tok"}}},
    )
    values.update(kw)
    return Settings(**values)


class ProviderDispatchTest(avocado.Test):
    def test_artifactory_selected_by_storage_type(self):
        provider = server_storage.provider_for(artifactory_settings(), "core", "core-dev", "dev")
        self.assertIsInstance(provider, ArtifactoryStorageProvider)
        self.assertEqual(provider.repo, "core-dev")
        self.assertEqual(provider.client.endpoint, "https://arti.lan:8081")
        self.assertEqual(provider.client._session.headers["Authorization"], "Bearer dev-tok")

    def test_artifactory_user_password(self):
        settings = artifactory_settings(
            artifactory_projects={"core": {"dev": {"user": "u", "password": "p"}}})
        provider = server_storage.provider_for(settings, "core", "core-dev", "dev")
        self.assertEqual(provider.client._session.auth, ("u", "p"))

    def test_artifactory_missing_keys_raise(self):
        settings = artifactory_settings(artifactory_projects={})
        with self.assertRaises(server_storage.StorageCredentialsError):
            server_storage.provider_for(settings, "core", "core-dev", "dev")

    def test_s3_default_unchanged(self):
        settings = Settings(s3_endpoint="https://s3.lan:3900",
                            s3_projects={"core": {"dev": {"access_key": "a", "secret_key": "s"}}})
        with mock.patch("seine.distributed.server.storage.provider_from") as provider_from:
            server_storage.provider_for(settings, "core", "core-dev", "dev")
            job = provider_from.call_args.args[0]
            self.assertEqual((job.endpoint, job.bucket), ("https://s3.lan:3900", "core-dev"))


if __name__ == "__main__":
    avocado.main()
