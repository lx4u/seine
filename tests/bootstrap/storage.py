#!/usr/bin/env python3
# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import avocado
import os
import sys
from unittest.mock import MagicMock, patch

path_to_self = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine.bootstrap import HostBootstrap, TargetBootstrap
from seine.storage.base import StorageProvider
from seine.storage.local import LocalStorageProvider

DISTRO = {
    "source": "debian",
    "release": "trixie",
    "architecture": "amd64",
    "uri": "http://deb.debian.org/debian",
}


class MockRemoteStorage(StorageProvider):
    def __init__(self, offline_mode="fallback"):
        self.offline_mode = offline_mode
        self.pulled = []
        self.pushed = []

    def pull(self, kind, key, dest=None):
        self.pulled.append((kind, key, dest))
        if dest:
            with open(dest, "wb") as f:
                f.write(b"mock-oci-tar")
            return dest
        return b"mock-oci-tar"

    def push(self, kind, key, path, spec=None, recipe=None):
        self.pushed.append((kind, key, path, recipe))
        return True

    def touch(self, kind, key):
        pass

    def explain(self, kind, key):
        return None


class BootstrapStorageTest(avocado.Test):
    def test_bootstrap_storage_key_and_recipe(self):
        storage = MockRemoteStorage()
        hb = HostBootstrap(DISTRO, {"shared_cache": True}, storage_provider=storage)
        dockerfile = "FROM debian:trixie\nRUN true\n"
        key = hb.storage_key(dockerfile)
        self.assertTrue(key.startswith("bootstrap/debian/trixie/online/"))
        self.assertEqual(len(key.split("/")[-1]), 16)

        recipe = hb.recipe(dockerfile)
        recipe_dict = dict(recipe)
        self.assertIn("dockerfile", recipe_dict)
        self.assertEqual(recipe_dict["kind"], hb.kind)

    def test_bootstrap_target_storage_key_includes_base(self):
        storage = MockRemoteStorage()
        tb = TargetBootstrap(DISTRO, {"shared_cache": True}, storage_provider=storage)
        dockerfile = "FROM scratch\nCOPY rootfs/ /\n"
        with patch("seine.container.ContainerEngine.imageLabel", return_value="base1234"):
            key = tb.storage_key(dockerfile, base="bootstrap/debian/trixie/online")
            recipe = dict(tb.recipe(dockerfile, base="bootstrap/debian/trixie/online"))

        self.assertTrue(key.startswith("bootstrap/debian/trixie/amd64/"))
        self.assertEqual(recipe.get("base"), "bootstrap/debian/trixie/online")
        self.assertEqual(recipe.get("base-inputs"), "base1234")

    @patch("seine.container.ContainerEngine.loadImage")
    @patch("seine.container.ContainerEngine.run")
    def test_bootstrap_pull_hits_storage_and_loads_image(self, mock_run, mock_load):
        storage = MockRemoteStorage()
        hb = HostBootstrap(DISTRO, {"shared_cache": True}, storage_provider=storage)
        dockerfile = "FROM debian:trixie\nRUN echo hi\n"

        # Calls to current():
        # 1. Before import_bundled
        # 2. After import_bundled
        # 3. Inside lock before remote pull
        # 4. After loadImage
        with patch.object(HostBootstrap, "current", side_effect=[False, False, False, True]):
            hb.build(dockerfile)

        self.assertEqual(len(storage.pulled), 1)
        self.assertEqual(storage.pulled[0][0], "bootstraps")
        mock_load.assert_called_once()
        # Container build was avoided because remote pull satisfied it
        mock_run.assert_not_called()

    @patch("seine.container.ContainerEngine.saveImage")
    @patch("seine.container.ContainerEngine.run")
    def test_bootstrap_push_saves_and_uploads_on_build(self, mock_run, mock_save):
        class EmptyStorage(MockRemoteStorage):
            def pull(self, kind, key, dest=None):
                self.pulled.append((kind, key, dest))
                return None

        storage = EmptyStorage()
        hb = HostBootstrap(DISTRO, {"shared_cache": True}, storage_provider=storage)
        dockerfile = "FROM debian:trixie\nRUN echo hi\n"

        with patch.object(HostBootstrap, "current", return_value=False):
            hb.build(dockerfile)

        self.assertEqual(len(storage.pulled), 1)
        mock_run.assert_called_once()
        mock_save.assert_called_once()
        self.assertEqual(len(storage.pushed), 1)
        self.assertEqual(storage.pushed[0][0], "bootstraps")

    def test_bootstrap_no_cache_bootstraps_skips_remote_storage(self):
        storage = MockRemoteStorage()
        hb = HostBootstrap(DISTRO, {"shared_cache": True, "cache_bootstraps": False},
                           storage_provider=storage)
        self.assertFalse(hb.is_remote())

    def test_bootstrap_local_storage_is_not_remote(self):
        hb = HostBootstrap(DISTRO, {}, storage_provider=LocalStorageProvider())
        self.assertFalse(hb.is_remote())


if __name__ == "__main__":
    avocado.main()
