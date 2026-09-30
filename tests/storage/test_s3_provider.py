#!/usr/bin/env python3
# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import avocado
import os
import sys
import tarfile
from unittest import mock

path_to_self = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.insert(0, path_to_sources)

from seine import storage
from seine.storage.base import StorageError, StorageOfflineError
from seine.storage.local import LocalStorageProvider
from seine.storage.s3 import S3StorageProvider
from seine.storage.s3.client import S3NotFoundError
from seine.cache import CleanChrootViolation


class StorageFactoryTest(avocado.Test):
    def test_default_is_local_provider(self):
        provider = storage.for_build()
        self.assertIsInstance(provider, LocalStorageProvider)

    def test_s3_cache_option_selects_s3_provider(self):
        options = {
            "s3_cache": True,
            "s3_endpoint": "http://127.0.0.1:3900",
            "s3_bucket": "test-bkt",
        }
        with mock.patch("seine.credentials.s3_credential_source") as mock_src:
            mock_src.return_value.get.return_value = {
                "access_key": "acc", "secret_key": "sec"
            }
            provider = storage.for_build(options)
            self.assertIsInstance(provider, S3StorageProvider)
            self.assertEqual(provider.bucket, "test-bkt")

    def test_spec_storage_s3_selects_s3_provider(self):
        spec = {
            "storage": {
                "s3": {
                    "endpoint": "http://127.0.0.1:3900",
                    "bucket": "spec-bkt",
                }
            }
        }
        with mock.patch("seine.credentials.s3_credential_source") as mock_src:
            mock_src.return_value.get.return_value = {
                "access_key": "acc", "secret_key": "sec"
            }
            provider = storage.for_build(spec=spec)
            self.assertIsInstance(provider, S3StorageProvider)
            self.assertEqual(provider.bucket, "spec-bkt")


class S3ProviderOperations(avocado.Test):
    def setUp(self):
        self.mock_client = mock.MagicMock()
        self.provider = S3StorageProvider(self.mock_client, "test-bucket")

    def test_push_compresses_and_uploads_with_recipe_and_touch(self):
        source_file = os.path.join(self.workdir, "package.tar")
        with open(source_file, "wb") as f:
            f.write(b"tar package content")

        recipe = [("revision", "1"), ("spec", "cafe1234")]
        res = self.provider.push("packages", "trixie/amd64/mypkg", source_file, recipe=recipe)
        self.assertTrue(res)

        self.assertEqual(self.mock_client.put_object.call_count, 3)

        # 1st call: payload
        args1, kwargs1 = self.mock_client.put_object.call_args_list[0]
        self.assertEqual(args1[0], "test-bucket")
        self.assertEqual(args1[1], "cache/packages/trixie/amd64/mypkg.tar.zst")
        self.assertIn("sha256", kwargs1["metadata"])
        self.assertTrue(kwargs1.get("if_none_match"))

        # 2nd call: recipe
        args2, _ = self.mock_client.put_object.call_args_list[1]
        self.assertEqual(args2[1], "cache/packages/trixie/amd64/mypkg.recipe")
        self.assertEqual(args2[2], b"revision\t1\nspec\tcafe1234\n")

        # 3rd call: touch
        args3, _ = self.mock_client.put_object.call_args_list[2]
        self.assertEqual(args3[1], "cache/packages/trixie/amd64/mypkg.touch")

    def test_push_enforces_clean_chroot_gate(self):
        dirty_tar = os.path.join(self.workdir, "dirty.tar")
        with tarfile.open(dirty_tar, "w") as tar:
            ti = tarfile.TarInfo("etc/ssl/private/cakey.pem")
            ti.size = 0
            tar.addfile(ti)

        with self.assertRaises(CleanChrootViolation):
            self.provider.push("chroot", "bookworm-amd64", dirty_tar)

        self.assertEqual(self.mock_client.put_object.call_count, 0)

    def test_explain_parses_remote_recipe(self):
        recipe_data = b"revision\t1\nspec\tbeef5678\nbase_image\tsha256:1122\n"
        self.mock_client.get_object.return_value = recipe_data

        recipe = self.provider.explain("chroots", "trixie-amd64")
        self.assertEqual(recipe, [
            ("revision", "1"),
            ("spec", "beef5678"),
            ("base_image", "sha256:1122"),
        ])
        self.mock_client.get_object.assert_called_once_with(
            "test-bucket", "cache/chroots/trixie-amd64.recipe")

    def test_explain_missing_returns_none(self):
        self.mock_client.get_object.side_effect = S3NotFoundError("missing")
        self.assertIsNone(self.provider.explain("chroots", "missing-key"))

    def test_strict_offline_mode_raises_on_failure(self):
        strict_provider = S3StorageProvider(
            self.mock_client, "test-bucket", offline_mode="strict")
        self.mock_client.put_object.side_effect = Exception("network down")

        source_file = os.path.join(self.workdir, "data.txt")
        with open(source_file, "wb") as f:
            f.write(b"data")

        with self.assertRaises(StorageOfflineError):
            strict_provider.push("packages", "item", source_file)

    def test_push_clean_chroot_violation_logs_refused_under_verbose(self):
        provider = S3StorageProvider(
            self.mock_client, "test-bucket", options={"verbose": True})
        dirty_tar = os.path.join(self.workdir, "dirty_verbose.tar")
        with tarfile.open(dirty_tar, "w") as tar:
            ti = tarfile.TarInfo("etc/ssl/private/cakey.pem")
            ti.size = 0
            tar.addfile(ti)

        with mock.patch("seine.cache_index.say") as mock_say:
            with self.assertRaises(CleanChrootViolation):
                provider.push("chroot", "trixie-amd64", dirty_tar)
            mock_say.assert_called_once()
            args, _ = mock_say.call_args
            self.assertEqual(args[0], {"verbose": True})
            self.assertIn("push chroot trixie-amd64 refused:", args[1])
            self.assertIn("clean-chroot gate rejected archive:", args[1])

    def test_push_failure_logs_failed_under_verbose(self):
        provider = S3StorageProvider(
            self.mock_client, "test-bucket", options={"verbose": True})
        self.mock_client.put_object.side_effect = Exception("network timeout")

        source_file = os.path.join(self.workdir, "data_verbose.txt")
        with open(source_file, "wb") as f:
            f.write(b"data")

        with mock.patch("seine.cache_index.say") as mock_say:
            res = provider.push("packages", "mypkg", source_file)
            self.assertFalse(res)
            mock_say.assert_called_once()
            args, _ = mock_say.call_args
            self.assertEqual(args[0], {"verbose": True})
            self.assertEqual(args[1], "push packages mypkg failed: network timeout")
