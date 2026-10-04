#!/usr/bin/env python3
# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import avocado
import datetime
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

from seine.storage.artifactory import (
    ArtifactoryStorageProvider,
    ArtifactoryNotFoundError,
)
from seine.storage.object_base import _compress_zstd
from seine.storage.base import StorageError, StorageOfflineError, StorageNotFoundError


class FakeArtifactory:
    """In-memory client speaking the ArtifactoryClient verbs."""

    def __init__(self, repos=("test-repo",)):
        self.objects = {}
        self.repos = set(repos)
        self.fail = None
        self.pinned_sha = {}
        self.endpoint = "http://arti:8081/artifactory"

    def _check(self):
        if self.fail:
            raise self.fail

    def object_url(self, repo, path):
        return f"{self.endpoint}/{repo}/{path}"

    def head_object(self, repo, path):
        self._check()
        if path not in self.objects:
            return None
        sha = self.pinned_sha.get(path, hashlib.sha256(self.objects[path]).hexdigest())
        return {"sha256": sha, "size": len(self.objects[path])}

    def get_object(self, repo, path):
        self._check()
        if path not in self.objects:
            raise ArtifactoryNotFoundError("missing")
        return self.objects[path]

    def download_file(self, repo, path, target):
        self._check()
        if path not in self.objects:
            raise ArtifactoryNotFoundError("missing")
        with open(target, "wb") as f:
            f.write(self.objects[path])

    def put_object(self, repo, path, data, sha256=None):
        self._check()
        body = data.encode("utf-8") if isinstance(data, str) else bytes(data)
        self.objects[path] = body
        return hashlib.sha256(body).hexdigest()

    def upload_file(self, repo, path, file_path, sha256=None):
        self._check()
        with open(file_path, "rb") as f:
            self.objects[path] = f.read()
        return hashlib.sha256(self.objects[path]).hexdigest()

    def delete(self, repo, path):
        self.objects.pop(path, None)

    def delete_paths(self, repo, paths):
        for path in paths:
            self.objects.pop(path, None)

    def repo_info(self, repo):
        if repo not in self.repos:
            return None
        return {"key": repo, "packageType": "generic"}

    def list_all_objects(self, repo, prefix=""):
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        for key in sorted(self.objects):
            if key.startswith(prefix):
                yield {"key": key, "size": len(self.objects[key]),
                       "last_modified": now}


def zst(data):
    return _compress_zstd(data)


class ArtifactoryProviderOperations(avocado.Test):
    def setUp(self):
        self.client = FakeArtifactory()
        self.provider = ArtifactoryStorageProvider(self.client, "test-repo")

    def _file(self, name, data):
        path = os.path.join(self.workdir, name)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def test_push_pull_bytes_round_trip(self):
        self.assertTrue(self.provider.push("packages", "mypkg", b"hello"))
        self.assertEqual(self.provider.pull("packages", "mypkg"), b"hello")

    def test_push_pull_file_round_trip(self):
        src = self._file("a.bin", b"file-bytes")
        dest = os.path.join(self.workdir, "out.bin")
        self.assertTrue(self.provider.push("packages", "f1", src))
        self.assertEqual(self.provider.pull("packages", "f1", dest), dest)
        with open(dest, "rb") as f:
            self.assertEqual(f.read(), b"file-bytes")

    def test_push_pull_directory_round_trip(self):
        src = os.path.join(self.workdir, "tree")
        os.makedirs(os.path.join(src, "sub"))
        with open(os.path.join(src, "sub", "f.txt"), "wb") as f:
            f.write(b"nested")
        self.assertTrue(self.provider.push("chroot", "t1", src))
        dest = os.path.join(self.workdir, "restored")
        os.makedirs(dest)
        self.provider.pull("chroot", "t1", dest)
        with open(os.path.join(dest, "sub", "f.txt"), "rb") as f:
            self.assertEqual(f.read(), b"nested")

    def test_pull_missing_returns_none(self):
        self.assertIsNone(self.provider.pull("packages", "nope", os.path.join(self.workdir, "x")))

    def test_pull_tampered_is_refused(self):
        self.provider.push("packages", "t", b"good")
        key = "cache/packages/t.tar.zst"
        self.client.pinned_sha[key] = self.client.head_object("test-repo", key)["sha256"]
        self.client.objects[key] = zst(b"evil")
        with self.assertRaises(StorageError):
            self.provider.pull("packages", "t")

    def test_touch_and_explain(self):
        self.provider.push("packages", "k", b"v", recipe=[("base", "abc")])
        self.provider.touch("packages", "k")
        self.assertIn("cache/packages/k.touch", self.client.objects)
        self.assertEqual(self.provider.explain("packages", "k"), [("base", "abc")])
        self.assertIsNone(self.provider.explain("packages", "unknown"))

    def test_offline_modes(self):
        self.provider.push("packages", "k", b"v")
        self.client.fail = Exception("network down")
        strict = ArtifactoryStorageProvider(self.client, "test-repo", offline_mode="strict")
        with self.assertRaises(StorageOfflineError):
            strict.pull("packages", "k", os.path.join(self.workdir, "x"))
        self.assertIsNone(self.provider.pull("packages", "k", os.path.join(self.workdir, "x")))
        self.assertFalse(self.provider.push("packages", "k2", b"v2"))

    def test_worktree_round_trip(self):
        src = os.path.join(self.workdir, "wt")
        os.makedirs(src)
        with open(os.path.join(src, "f.txt"), "wb") as f:
            f.write(b"work")
        with tempfile.TemporaryDirectory() as tmp:
            archive = ArtifactoryStorageProvider._worktree_archive(src, tmp)
            with open(archive, "rb") as f:
                digest = hashlib.sha256(f.read()).hexdigest()
            self.assertTrue(self.provider.push_worktree("myproj", digest, archive))
        self.assertTrue(self.provider.has_worktree("myproj", digest))
        dest = os.path.join(self.workdir, "wt-out")
        self.provider.pull_worktree("myproj", digest, dest)
        with open(os.path.join(dest, "f.txt"), "rb") as f:
            self.assertEqual(f.read(), b"work")

    def test_worktree_missing_and_wrong_digest(self):
        self.assertIsNone(self.provider.pull_worktree(
            "myproj", "0" * 64, os.path.join(self.workdir, "x")))
        self.assertFalse(self.provider.has_worktree("myproj", "0" * 64))
        arc = self._file("w.tar.zst", zst(b"data"))
        with self.assertRaises(StorageError):
            self.provider.push_worktree("myproj", "1" * 64, arc)

    def test_artifact_round_trip(self):
        src = self._file("pc.img", b"image-bytes")
        info = self.provider.push_artifact_info("proj", "b1", src)
        self.assertEqual(info["key"], "artifacts/proj/b1/pc.img")
        self.assertEqual(info["sha256"], hashlib.sha256(b"image-bytes").hexdigest())
        dest = os.path.join(self.workdir, "got.img")
        self.assertEqual(self.provider.pull_artifact("proj", "b1", "pc.img", dest), dest)
        with open(dest, "rb") as f:
            self.assertEqual(f.read(), b"image-bytes")

    def test_pull_artifact_missing_raises_not_found(self):
        with self.assertRaises(StorageNotFoundError):
            self.provider.pull_artifact("proj", "b1", "nope.img",
                                        os.path.join(self.workdir, "x"))

    def test_usage_delete_prefix_purge(self):
        self.provider.push("packages", "a", b"1111")
        self.provider.push("packages", "b", b"22")
        before = self.provider.usage("cache/packages/")
        self.assertGreater(before, 0)
        count, freed = self.provider.delete_prefix("cache/packages/a")
        self.assertEqual(count, 2)
        self.assertEqual(self.provider.usage("cache/packages/"), before - freed)
        with self.assertRaises(ValueError):
            self.provider.delete_prefix("")
        count, _ = self.provider.purge()
        self.assertGreaterEqual(count, 1)
        self.assertEqual(self.provider.usage(), 0)

    def test_ensure_bucket(self):
        self.provider.ensure_bucket()
        missing = ArtifactoryStorageProvider(self.client, "no-such-repo")
        with self.assertRaises(StorageError) as ctx:
            missing.ensure_bucket()
        self.assertIn("no-such-repo", str(ctx.exception))

    def test_generate_download_url(self):
        url = self.provider.generate_download_url("proj", "pc.img")
        self.assertEqual(url, "http://arti:8081/artifactory/test-repo/artifacts/proj/pc.img")
        url = self.provider.generate_download_url("proj", "artifacts/proj/b1/pc.img")
        self.assertEqual(url, "http://arti:8081/artifactory/test-repo/artifacts/proj/b1/pc.img")

    def test_open_artifact_maps_names_and_keys_like_download_urls(self):
        self.client.open_object = mock.MagicMock(return_value="stream")
        self.assertEqual(self.provider.open_artifact("proj", "pc.img"), "stream")
        self.client.open_object.assert_called_with("test-repo", "artifacts/proj/pc.img")
        self.provider.open_artifact("proj", "artifacts/proj/b1/pc.img")
        self.client.open_object.assert_called_with("test-repo", "artifacts/proj/b1/pc.img")

    def test_no_lifecycle_support(self):
        self.assertFalse(self.provider.supports_lifecycle)


if __name__ == "__main__":
    avocado.main()
