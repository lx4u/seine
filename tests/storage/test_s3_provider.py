#!/usr/bin/env python3
# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import avocado
import hashlib
import io
import os
import sys
import tarfile
import tempfile
from unittest import mock

path_to_self = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.insert(0, path_to_sources)

from seine import storage
from seine.storage.base import StorageError, StorageOfflineError
from seine.storage.s3 import provider as s3_provider
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


class FakeS3:
    """In-memory client; get_object is only allowed for small sidecars."""

    def __init__(self):
        self.objects = {}
        self.meta = {}
        self.uploads = []
        self.put_keys = []
        self.get_keys = []
        self.fail = None

    def head_object(self, bucket, key):
        if self.fail:
            raise self.fail
        if key not in self.objects:
            return None
        headers = {"content-length": str(len(self.objects[key]))}
        for k, v in self.meta.get(key, {}).items():
            headers[f"x-amz-meta-{k}"] = v
        return headers

    def upload_file(self, bucket, key, path, metadata=None):
        if self.fail:
            raise self.fail
        with open(path, "rb") as f:
            data = f.read()
        self.uploads.append(key)
        self.objects[key] = data
        self.meta[key] = {"sha256": hashlib.sha256(data).hexdigest()}

    def download_file(self, bucket, key, path):
        with open(path, "wb") as f:
            f.write(self.objects[key])

    def put_object(self, bucket, key, data, metadata=None, if_none_match=False):
        if self.fail:
            raise self.fail
        self.put_keys.append(key)
        self.objects[key] = bytes(data)

    def get_object(self, bucket, key):
        assert key.endswith(".recipe"), f"get_object on {key}"
        self.get_keys.append(key)
        if key not in self.objects:
            raise S3NotFoundError("missing")
        return self.objects[key]


    def list_all_objects(self, bucket, prefix=""):
        for key in sorted(self.objects):
            if key.startswith(prefix):
                yield {"key": key, "size": len(self.objects[key])}

    def refresh_object(self, bucket, key):
        self.refreshed = key
        return key in self.objects

    def delete_objects(self, bucket, keys):
        self.deleted = list(keys)
        for key in self.deleted:
            self.objects.pop(key, None)


def make_tar(members):
    """Build a tar from (TarInfo, bytes|None) pairs."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for info, data in members:
            tar.addfile(info, io.BytesIO(data) if data is not None else None)
    return buf.getvalue()


def zst(data):
    return s3_provider._compress_zstd(data)


def unzst(data):
    """Decompress a streamed frame (it carries no content size)."""
    with tempfile.NamedTemporaryFile() as f:
        f.write(data)
        f.flush()
        with s3_provider._zstd_reader(f.name) as reader:
            return reader.read()


class S3ProviderOperations(avocado.Test):
    def setUp(self):
        self.client = FakeS3()
        self.provider = S3StorageProvider(self.client, "test-bucket")

    def _file(self, name, data):
        path = os.path.join(self.workdir, name)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def _store(self, key, data, sha256=True):
        self.client.objects[key] = data
        if sha256:
            self.client.meta[key] = {"sha256": hashlib.sha256(data).hexdigest()}

    def test_push_compresses_and_uploads_with_recipe_and_touch(self):
        source_file = self._file("package.tar", b"tar package content")
        recipe = [("revision", "1"), ("spec", "cafe1234")]
        res = self.provider.push("packages", "trixie/amd64/mypkg", source_file, recipe=recipe)
        self.assertTrue(res)

        obj = "cache/packages/trixie/amd64/mypkg.tar.zst"
        self.assertEqual(self.client.uploads, [obj])
        self.assertEqual(self.client.meta[obj]["sha256"],
                         hashlib.sha256(self.client.objects[obj]).hexdigest())
        self.assertEqual(unzst(self.client.objects[obj]), b"tar package content")
        self.assertEqual(self.client.put_keys, [
            "cache/packages/trixie/amd64/mypkg.recipe",
            "cache/packages/trixie/amd64/mypkg.touch"])
        self.assertEqual(self.client.objects["cache/packages/trixie/amd64/mypkg.recipe"],
                         b"revision\t1\nspec\tcafe1234\n")

    def test_push_existing_digest_key_is_not_uploaded_again(self):
        digest = "0123456789abcdef"
        obj = f"cache/packages/trixie/{digest}.tar.zst"
        self._store(obj, b"first")
        source_file = self._file("again.tar", b"second")
        self.assertTrue(self.provider.push("packages", f"trixie/{digest}", source_file,
                                           recipe=[("revision", "1")]))
        self.assertEqual(self.client.uploads, [])
        self.assertEqual(self.client.objects[obj], b"first")
        self.assertIn(f"cache/packages/trixie/{digest}.recipe", self.client.put_keys)
        self.assertIn(f"cache/packages/trixie/{digest}.touch", self.client.put_keys)

    def test_push_alias_key_is_overwritten(self):
        obj = "cache/packages/mypkg.tar.zst"
        self.assertTrue(self.provider.push("packages", "mypkg",
                                           self._file("a.tar", b"first")))
        self.assertTrue(self.provider.push("packages", "mypkg",
                                           self._file("b.tar", b"second")))
        self.assertEqual(self.client.uploads, [obj, obj])
        self.assertEqual(unzst(self.client.objects[obj]), b"second")

    def test_push_prebuilt_zst_is_uploaded_as_is(self):
        archive = self._file("chroot.tar.zst", zst(b"chroot"))
        self.assertTrue(self.provider.push("chroots", "trixie", archive))
        with open(archive, "rb") as f:
            self.assertEqual(self.client.objects["cache/chroots/trixie.tar.zst"], f.read())

    def test_push_enforces_clean_chroot_gate(self):
        dirty_tar = os.path.join(self.workdir, "dirty.tar")
        with tarfile.open(dirty_tar, "w") as tar:
            ti = tarfile.TarInfo("etc/ssl/private/cakey.pem")
            ti.size = 0
            tar.addfile(ti)

        with self.assertRaises(CleanChrootViolation):
            self.provider.push("chroot", "bookworm-amd64", dirty_tar)

        self.assertEqual(self.client.uploads, [])
        self.assertEqual(self.client.put_keys, [])

    def test_explain_parses_remote_recipe(self):
        recipe_data = b"revision\t1\nspec\tbeef5678\nbase_image\tsha256:1122\n"
        self.client.objects["cache/chroots/trixie-amd64.recipe"] = recipe_data

        recipe = self.provider.explain("chroots", "trixie-amd64")
        self.assertEqual(recipe, [
            ("revision", "1"),
            ("spec", "beef5678"),
            ("base_image", "sha256:1122"),
        ])

    def test_explain_missing_returns_none(self):
        self.assertIsNone(self.provider.explain("chroots", "missing-key"))

    def test_strict_offline_mode_raises_on_failure(self):
        strict_provider = S3StorageProvider(
            self.client, "test-bucket", offline_mode="strict")
        self.client.fail = Exception("network down")
        source_file = self._file("data.txt", b"data")

        with self.assertRaises(StorageOfflineError):
            strict_provider.push("packages", "item", source_file)

    def test_push_clean_chroot_violation_logs_refused_under_verbose(self):
        provider = S3StorageProvider(
            self.client, "test-bucket", options={"verbose": True})
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
            self.client, "test-bucket", options={"verbose": True})
        self.client.fail = Exception("network timeout")
        source_file = self._file("data_verbose.txt", b"data")

        with mock.patch("seine.cache_index.say") as mock_say:
            res = provider.push("packages", "mypkg", source_file)
            self.assertFalse(res)
            mock_say.assert_called_once()
            args, _ = mock_say.call_args
            self.assertEqual(args[0], {"verbose": True})
            self.assertEqual(args[1], "push packages mypkg failed: network timeout")


class S3ProviderHousekeeping(avocado.Test):
    def setUp(self):
        self.client = FakeS3()
        self.provider = S3StorageProvider(self.client, "test-bucket")
        self.client.objects.update({
            "artifacts/p/1/a.img": b"x" * 100,
            "artifacts/p/1/b.img": b"y" * 50,
            "artifacts/p/2/a.img": b"z" * 7,
            "cache/packages/k.tar.zst": b"w" * 10,
            "cache/packages/k.recipe": b"r",
            "cache/packages/k.touch": b"t",
        })

    def test_usage_counts_everything_under_the_prefix(self):
        self.assertEqual(self.provider.usage("artifacts/p/1/"), 150)
        self.assertEqual(self.provider.usage("cache/"), 12)

    def test_usage_without_prefix_is_the_whole_bucket(self):
        self.assertEqual(self.provider.usage(), 169)

    def test_delete_prefix_reports_and_spares_other_prefixes(self):
        self.assertEqual(self.provider.delete_prefix("artifacts/p/1/"), (2, 150))
        self.assertEqual(self.client.deleted,
                         ["artifacts/p/1/a.img", "artifacts/p/1/b.img"])
        self.assertIn("artifacts/p/2/a.img", self.client.objects)
        self.assertIn("cache/packages/k.touch", self.client.objects)

    def test_refresh_worktree_targets_the_worktree_key(self):
        self.client.objects["worktrees/p/d1.tar.zst"] = b"w"
        self.assertTrue(self.provider.refresh_worktree("p", "d1"))
        self.assertEqual(self.client.refreshed, "worktrees/p/d1.tar.zst")
        self.assertFalse(self.provider.refresh_worktree("p", "d2"))

    def test_delete_prefix_refuses_an_empty_prefix(self):
        with self.assertRaises(ValueError):
            self.provider.delete_prefix("")
        self.assertEqual(len(self.client.objects), 6)


class S3ProviderPull(avocado.Test):
    def setUp(self):
        self.client = FakeS3()
        self.provider = S3StorageProvider(self.client, "test-bucket")
        self.obj = "cache/packages/mypkg.tar.zst"

    def _store(self, data, sha256=True):
        self.client.objects[self.obj] = data
        if sha256:
            self.client.meta[self.obj] = {"sha256": hashlib.sha256(data).hexdigest()}

    def _pull_dir(self, tarball):
        self._store(zst(tarball))
        dest = os.path.join(self.workdir, "repo")
        os.makedirs(dest)
        return dest

    def test_directory_round_trip_streams_without_get_object(self):
        src = os.path.join(self.workdir, "src")
        os.makedirs(os.path.join(src, "sub"))
        with open(os.path.join(src, "sub", "a.deb"), "wb") as f:
            f.write(os.urandom(4096))
        self.assertTrue(self.provider.push("packages", "mypkg", src,
                                           recipe=[("revision", "1")]))
        dest = os.path.join(self.workdir, "out")
        os.makedirs(dest)
        self.assertEqual(self.provider.pull("packages", "mypkg", dest), dest)
        with open(os.path.join(src, "sub", "a.deb"), "rb") as a, \
                open(os.path.join(dest, "sub", "a.deb"), "rb") as b:
            self.assertEqual(a.read(), b.read())
        self.assertTrue(os.path.exists(os.path.join(dest, "Packages")))
        self.assertEqual(self.client.get_keys, ["cache/packages/mypkg.recipe"])

    def test_file_and_bytes_round_trip(self):
        self.provider.push("bootstraps", "img", b"image bytes")
        self.assertEqual(self.provider.pull("bootstraps", "img"), b"image bytes")
        dest = os.path.join(self.workdir, "image.tar")
        self.assertEqual(self.provider.pull("bootstraps", "img", dest), dest)
        with open(dest, "rb") as f:
            self.assertEqual(f.read(), b"image bytes")

    def test_zst_dest_keeps_compressed_archive(self):
        archive = os.path.join(self.workdir, "c.tar.zst")
        with open(archive, "wb") as f:
            f.write(zst(b"chroot"))
        self.provider.push("chroots", "trixie", archive)
        dest = os.path.join(self.workdir, "pulled.tar.zst")
        self.assertEqual(self.provider.pull("chroots", "trixie", dest), dest)
        with open(dest, "rb") as f:
            self.assertEqual(s3_provider._decompress_zstd(f.read()), b"chroot")

    def test_missing_object_returns_none(self):
        self.assertIsNone(self.provider.pull("packages", "nope", self.workdir))

    def test_corrupted_object_is_rejected(self):
        self.provider.push("bootstraps", "img", b"image bytes")
        obj = "cache/bootstraps/img.tar.zst"
        self.client.objects[obj] = zst(b"tampered")
        dest = os.path.join(self.workdir, "image.tar")
        with self.assertRaises(StorageError) as ctx:
            self.provider.pull("bootstraps", "img", dest)
        self.assertIn("does not match", str(ctx.exception))
        self.assertFalse(os.path.exists(dest))

    def test_missing_sha256_metadata_is_rejected(self):
        self._store(zst(b"data"), sha256=False)
        dest = os.path.join(self.workdir, "x.tar")
        with self.assertRaises(StorageError) as ctx:
            self.provider.pull("packages", "mypkg", dest)
        self.assertIn("no sha256", str(ctx.exception))
        self.assertFalse(os.path.exists(dest))

    def test_rejection_is_reported_under_verbose(self):
        provider = S3StorageProvider(self.client, "test-bucket", options={"verbose": True})
        self._store(zst(b"data"), sha256=False)
        with mock.patch("seine.cache_index.say") as mock_say:
            with self.assertRaises(StorageError):
                provider.pull("packages", "mypkg")
            self.assertIn("pull packages mypkg refused:", mock_say.call_args[0][1])

    def test_transport_error_maps_to_offline_in_strict_mode(self):
        strict = S3StorageProvider(self.client, "test-bucket", offline_mode="strict")
        self.client.fail = Exception("network down")
        with self.assertRaises(StorageOfflineError):
            strict.pull("packages", "mypkg", self.workdir)
        self.assertIsNone(self.provider.pull("packages", "mypkg", self.workdir))

    def test_path_traversal_member_is_rejected(self):
        info = tarfile.TarInfo("../escape.txt")
        info.size = 3
        dest = self._pull_dir(make_tar([(info, b"bad")]))
        with self.assertRaises(StorageError):
            self.provider.pull("packages", "mypkg", dest)
        self.assertFalse(os.path.exists(os.path.join(self.workdir, "escape.txt")))

    def test_absolute_path_member_stays_inside_dest(self):
        target = os.path.join(self.workdir, "abs.txt")
        info = tarfile.TarInfo(target)
        info.size = 3
        dest = self._pull_dir(make_tar([(info, b"bad")]))
        self.provider.pull("packages", "mypkg", dest)
        self.assertFalse(os.path.exists(target))
        self.assertTrue(os.path.exists(os.path.join(dest, target.lstrip("/"))))

    def test_symlink_out_of_dest_is_rejected(self):
        link = tarfile.TarInfo("link")
        link.type = tarfile.SYMTYPE
        link.linkname = "../../outside"
        dest = self._pull_dir(make_tar([(link, None)]))
        with self.assertRaises(StorageError):
            self.provider.pull("packages", "mypkg", dest)
        self.assertFalse(os.path.lexists(os.path.join(dest, "link")))

    def test_device_member_is_rejected(self):
        dev = tarfile.TarInfo("null")
        dev.type = tarfile.CHRTYPE
        dev.devmajor, dev.devminor = 1, 3
        dest = self._pull_dir(make_tar([(dev, None)]))
        with self.assertRaises(StorageError):
            self.provider.pull("packages", "mypkg", dest)
