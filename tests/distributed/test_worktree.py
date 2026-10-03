#!/usr/bin/env python3
# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0
"""Tests for worktree packing, unpacking, ignore filtering, and S3 staging."""

import avocado
import hashlib
import io
import os
import shutil
import sys
import tarfile
import tempfile
from unittest import mock

path_to_self = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.insert(0, path_to_sources)

from seine.distributed.client.remote import upload_worktree
from seine.distributed.client.worktree import (
    DEFAULT_EXCLUDES,
    OutsideRootError,
    PathTraversalError,
    pack_sparse_worktree,
    pack_worktree,
    unpack_worktree,
)
from seine.storage.base import StorageError, StorageOfflineError
from seine.storage.s3.client import S3Client, S3NotFoundError
from seine.storage.s3.provider import S3StorageProvider, _compress_zstd, _file_sha256, _zstd_reader


def _create_malicious_archive(member_name: str, linkname: str = "", is_sym: bool = False) -> str:
    tar_buf = io.BytesIO()
    with tarfile.open(fileobj=tar_buf, mode="w") as tar:
        ti = tarfile.TarInfo(name=member_name)
        if is_sym:
            ti.type = tarfile.SYMTYPE
            ti.linkname = linkname
            tar.addfile(ti)
        else:
            data = b"malicious content"
            ti.size = len(data)
            tar.addfile(ti, io.BytesIO(data))

    compressed = _compress_zstd(tar_buf.getvalue())
    fd, path = tempfile.mkstemp(suffix=".tar.zst", prefix="evil-")
    os.close(fd)
    with open(path, "wb") as f:
        f.write(compressed)
    return path


class TestWorktreeRoundtrip(avocado.Test):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        tempfile.tempdir = self.temp_dir.name
        self.src_dir = os.path.join(self.temp_dir.name, "src")
        self.dest_dir = os.path.join(self.temp_dir.name, "dest")
        os.makedirs(self.src_dir)

    def tearDown(self):
        tempfile.tempdir = None
        self.temp_dir.cleanup()

    def test_pack_and_unpack_roundtrip(self):
        with open(os.path.join(self.src_dir, "file1.txt"), "w") as f:
            f.write("content 1")
        sub_dir = os.path.join(self.src_dir, "subdir")
        os.makedirs(sub_dir)
        with open(os.path.join(sub_dir, "file2.txt"), "w") as f:
            f.write("content 2")

        archive_path, digest = pack_worktree(self.src_dir)
        self.assertTrue(os.path.isfile(archive_path))
        self.assertEqual(len(digest), 64)

        with open(archive_path, "rb") as f:
            expected_digest = hashlib.sha256(f.read()).hexdigest()
        self.assertEqual(digest, expected_digest)

        result = unpack_worktree(archive_path, self.dest_dir)
        self.assertEqual(result, os.path.abspath(self.dest_dir))

        dest_file1 = os.path.join(self.dest_dir, "file1.txt")
        dest_file2 = os.path.join(self.dest_dir, "subdir", "file2.txt")
        self.assertTrue(os.path.isfile(dest_file1))
        self.assertTrue(os.path.isfile(dest_file2))
        with open(dest_file1) as f:
            self.assertEqual(f.read(), "content 1")
        with open(dest_file2) as f:
            self.assertEqual(f.read(), "content 2")

        if os.path.exists(archive_path):
            os.unlink(archive_path)

    def test_pack_with_explicit_out_path(self):
        with open(os.path.join(self.src_dir, "test.txt"), "w") as f:
            f.write("test")

        custom_out = os.path.join(self.temp_dir.name, "custom.tar.zst")
        archive_path, digest = pack_worktree(self.src_dir, out_path=custom_out)
        self.assertEqual(archive_path, custom_out)
        self.assertTrue(os.path.isfile(custom_out))


class TestWorktreeIgnorePatterns(avocado.Test):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        tempfile.tempdir = self.temp_dir.name
        self.src_dir = os.path.join(self.temp_dir.name, "src")
        self.dest_dir = os.path.join(self.temp_dir.name, "dest")
        os.makedirs(self.src_dir)

    def tearDown(self):
        tempfile.tempdir = None
        self.temp_dir.cleanup()

    def test_default_excludes(self):
        os.makedirs(os.path.join(self.src_dir, ".git"))
        with open(os.path.join(self.src_dir, ".git", "config"), "w") as f:
            f.write("git config")

        os.makedirs(os.path.join(self.src_dir, "__pycache__"))
        with open(os.path.join(self.src_dir, "__pycache__", "mod.cpython-313.pyc"), "w") as f:
            f.write("pyc")

        os.makedirs(os.path.join(self.src_dir, ".venv", "bin"))
        with open(os.path.join(self.src_dir, ".venv", "bin", "activate"), "w") as f:
            f.write("venv")

        os.makedirs(os.path.join(self.src_dir, "build"))
        with open(os.path.join(self.src_dir, "build", "output.bin"), "w") as f:
            f.write("bin")

        with open(os.path.join(self.src_dir, "top.pyc"), "w") as f:
            f.write("pyc")

        with open(os.path.join(self.src_dir, "keep.py"), "w") as f:
            f.write("print('hello')")

        for rel in ("tests/build/x.py", "docs/build/y.md"):
            full = os.path.join(self.src_dir, rel)
            os.makedirs(os.path.dirname(full))
            with open(full, "w") as f:
                f.write("x")

        archive_path, _ = pack_worktree(self.src_dir)
        unpack_worktree(archive_path, self.dest_dir)

        self.assertTrue(os.path.isfile(os.path.join(self.dest_dir, "keep.py")))
        self.assertTrue(os.path.isfile(os.path.join(self.dest_dir, "tests", "build", "x.py")))
        self.assertTrue(os.path.isfile(os.path.join(self.dest_dir, "docs", "build", "y.md")))
        self.assertFalse(os.path.exists(os.path.join(self.dest_dir, ".git")))
        self.assertFalse(os.path.exists(os.path.join(self.dest_dir, "__pycache__")))
        self.assertFalse(os.path.exists(os.path.join(self.dest_dir, ".venv")))
        self.assertFalse(os.path.exists(os.path.join(self.dest_dir, "build")))
        self.assertFalse(os.path.exists(os.path.join(self.dest_dir, "top.pyc")))

        if os.path.exists(archive_path):
            os.unlink(archive_path)

    def test_gitignore_and_seineignore_and_negation(self):
        with open(os.path.join(self.src_dir, ".gitignore"), "w") as f:
            f.write("*.tmp\nsecret_dir/\n/root_only.txt\n!keep.tmp\n")

        with open(os.path.join(self.src_dir, ".seineignore"), "w") as f:
            f.write("seine_scratch/\n*.bak\n")

        with open(os.path.join(self.src_dir, "drop.tmp"), "w") as f:
            f.write("temp")
        with open(os.path.join(self.src_dir, "keep.tmp"), "w") as f:
            f.write("important temp")
        os.makedirs(os.path.join(self.src_dir, "secret_dir"))
        with open(os.path.join(self.src_dir, "secret_dir", "secret.key"), "w") as f:
            f.write("key")
        with open(os.path.join(self.src_dir, "root_only.txt"), "w") as f:
            f.write("root")

        os.makedirs(os.path.join(self.src_dir, "seine_scratch"))
        with open(os.path.join(self.src_dir, "seine_scratch", "notes.md"), "w") as f:
            f.write("notes")
        with open(os.path.join(self.src_dir, "old.bak"), "w") as f:
            f.write("backup")

        # Anchored /root_only.txt should not match in subdirectories
        sub = os.path.join(self.src_dir, "sub")
        os.makedirs(sub)
        with open(os.path.join(sub, "root_only.txt"), "w") as f:
            f.write("sub root_only")
        with open(os.path.join(sub, "app.py"), "w") as f:
            f.write("app")

        with open(os.path.join(self.src_dir, "extra_ignored.json"), "w") as f:
            f.write("{}")

        archive_path, _ = pack_worktree(
            self.src_dir, ignore_rules=["extra_ignored.json"]
        )
        unpack_worktree(archive_path, self.dest_dir)

        self.assertTrue(os.path.isfile(os.path.join(self.dest_dir, "keep.tmp")))
        self.assertTrue(os.path.isfile(os.path.join(self.dest_dir, "sub", "root_only.txt")))
        self.assertTrue(os.path.isfile(os.path.join(self.dest_dir, "sub", "app.py")))
        self.assertTrue(os.path.isfile(os.path.join(self.dest_dir, ".gitignore")))
        self.assertTrue(os.path.isfile(os.path.join(self.dest_dir, ".seineignore")))

        self.assertFalse(os.path.exists(os.path.join(self.dest_dir, "drop.tmp")))
        self.assertFalse(os.path.exists(os.path.join(self.dest_dir, "secret_dir")))
        self.assertFalse(os.path.exists(os.path.join(self.dest_dir, "root_only.txt")))
        self.assertFalse(os.path.exists(os.path.join(self.dest_dir, "seine_scratch")))
        self.assertFalse(os.path.exists(os.path.join(self.dest_dir, "old.bak")))
        self.assertFalse(os.path.exists(os.path.join(self.dest_dir, "extra_ignored.json")))

        if os.path.exists(archive_path):
            os.unlink(archive_path)


class TestWorktreeSecurity(avocado.Test):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        tempfile.tempdir = self.temp_dir.name
        self.dest_dir = os.path.join(self.temp_dir.name, "dest")
        os.makedirs(self.dest_dir)

    def tearDown(self):
        tempfile.tempdir = None
        self.temp_dir.cleanup()

    def test_relative_path_traversal_rejected(self):
        evil_archive = _create_malicious_archive("../evil.txt")
        try:
            with self.assertRaises(PathTraversalError):
                unpack_worktree(evil_archive, self.dest_dir)
        finally:
            if os.path.exists(evil_archive):
                os.unlink(evil_archive)

    def test_absolute_path_traversal_rejected(self):
        evil_archive = _create_malicious_archive("/etc/evil.txt")
        try:
            with self.assertRaises(PathTraversalError):
                unpack_worktree(evil_archive, self.dest_dir)
        finally:
            if os.path.exists(evil_archive):
                os.unlink(evil_archive)

    def test_absolute_symlink_traversal_rejected(self):
        evil_archive = _create_malicious_archive("bad_link", linkname="/etc/passwd", is_sym=True)
        try:
            with self.assertRaises(PathTraversalError):
                unpack_worktree(evil_archive, self.dest_dir)
        finally:
            if os.path.exists(evil_archive):
                os.unlink(evil_archive)

    def test_relative_symlink_traversal_rejected(self):
        evil_archive = _create_malicious_archive("bad_link", linkname="../../etc/passwd", is_sym=True)
        try:
            with self.assertRaises(PathTraversalError):
                unpack_worktree(evil_archive, self.dest_dir)
        finally:
            if os.path.exists(evil_archive):
                os.unlink(evil_archive)

    def test_safe_symlink_allowed(self):
        tar_buf = io.BytesIO()
        with tarfile.open(fileobj=tar_buf, mode="w") as tar:
            data = b"target data"
            ti_file = tarfile.TarInfo(name="target.txt")
            ti_file.size = len(data)
            tar.addfile(ti_file, io.BytesIO(data))

            ti_sym = tarfile.TarInfo(name="symlink.txt")
            ti_sym.type = tarfile.SYMTYPE
            ti_sym.linkname = "target.txt"
            tar.addfile(ti_sym)

        compressed = _compress_zstd(tar_buf.getvalue())
        fd, path = tempfile.mkstemp(suffix=".tar.zst")
        os.close(fd)
        with open(path, "wb") as f:
            f.write(compressed)

        try:
            result = unpack_worktree(path, self.dest_dir)
            self.assertEqual(result, os.path.abspath(self.dest_dir))
            target_path = os.path.join(self.dest_dir, "target.txt")
            link_path = os.path.join(self.dest_dir, "symlink.txt")
            self.assertTrue(os.path.isfile(target_path))
            self.assertTrue(os.path.islink(link_path))
            with open(link_path) as f:
                self.assertEqual(f.read(), "target data")
        finally:
            if os.path.exists(path):
                os.unlink(path)


class TestWorktreeDeterminism(avocado.Test):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        tempfile.tempdir = self.temp_dir.name
        self.src_dir = os.path.join(self.temp_dir.name, "src")
        os.makedirs(os.path.join(self.src_dir, "sub"))
        for name, mode in (("a.txt", 0o600), ("sub/b.sh", 0o700), ("sub/c.txt", 0o664)):
            full = os.path.join(self.src_dir, name)
            with open(full, "w") as f:
                f.write(name)
            os.chmod(full, mode)
        os.symlink("a.txt", os.path.join(self.src_dir, "link"))

    def tearDown(self):
        tempfile.tempdir = None
        self.temp_dir.cleanup()

    def _pack(self):
        path, digest = pack_worktree(self.src_dir)
        with open(path, "rb") as f:
            data = f.read()
        os.unlink(path)
        return data, digest

    def test_same_tree_same_bytes(self):
        first, d1 = self._pack()
        second, d2 = self._pack()
        self.assertEqual(first, second)
        self.assertEqual(d1, d2)
        self.assertEqual(d1, hashlib.sha256(first).hexdigest())

    def test_mode_noise_does_not_change_digest(self):
        _, before = self._pack()
        os.chmod(os.path.join(self.src_dir, "a.txt"), 0o640)
        _, after = self._pack()
        self.assertEqual(before, after)

    def test_mtimes_are_part_of_the_bundle_on_purpose(self):
        _, before = self._pack()
        os.utime(os.path.join(self.src_dir, "a.txt"), (12345, 67890))
        _, after = self._pack()
        self.assertNotEqual(before, after)

    def test_mtimes_survive_roundtrip_and_feed_signing_epoch(self):
        from seine.signing import _epoch
        stamp = 1700000000
        for dirpath, dirs, files in os.walk(self.src_dir, topdown=False):
            for name in files + dirs:
                full = os.path.join(dirpath, name)
                if not os.path.islink(full):
                    os.utime(full, (stamp, stamp))
        os.utime(self.src_dir, (stamp, stamp))
        path, _ = pack_worktree(self.src_dir)
        out = os.path.join(self.temp_dir.name, "out")
        unpack_worktree(path, out)
        os.unlink(path)
        for rel in ("a.txt", "sub", "sub/b.sh"):
            self.assertEqual(int(os.stat(os.path.join(out, rel)).st_mtime), stamp, rel)
        spec = os.path.join(out, "sub", "c.txt")
        self.assertEqual(_epoch({"files": [spec]}), stamp)

    def test_entries_are_normalised(self):
        path, _ = pack_worktree(self.src_dir)
        try:
            with _zstd_reader(path) as reader, tarfile.open(fileobj=reader, mode="r|") as tar:
                members = list(tar)
        finally:
            os.unlink(path)
        names = [m.name for m in members]
        self.assertEqual(names, ["a.txt", "link", "sub", "sub/b.sh", "sub/c.txt"])
        for m in members:
            self.assertEqual((m.uid, m.gid, m.uname, m.gname), (0, 0, "", ""))
            self.assertGreater(m.mtime, 0)
        modes = {m.name: m.mode for m in members}
        self.assertEqual(modes["a.txt"], 0o644)
        self.assertEqual(modes["sub/b.sh"], 0o755)
        self.assertEqual(modes["sub"], 0o755)

    def test_no_whole_tree_in_memory(self):
        with mock.patch("io.BytesIO", side_effect=AssertionError("BytesIO used")):
            path, _ = pack_worktree(self.src_dir)
            unpack_worktree(path, os.path.join(self.temp_dir.name, "out"))
        os.unlink(path)


class TestWorktreeSecretExcludes(avocado.Test):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        tempfile.tempdir = self.temp_dir.name
        self.src_dir = os.path.join(self.temp_dir.name, "src")
        self.dest_dir = os.path.join(self.temp_dir.name, "dest")
        os.makedirs(self.src_dir)

    def tearDown(self):
        tempfile.tempdir = None
        self.temp_dir.cleanup()

    def _touch(self, rel):
        full = os.path.join(self.src_dir, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            f.write("x")

    def test_secret_files_are_not_packed(self):
        dropped = ["seine.db", "seine.db-shm", "seine.db-wal", ".env", ".env.prod",
                   "tls.key", "id_rsa", "id_rsa.pub", "id_ed25519",
                   "deploy/hosts", "home/user/.bashrc", "sub/data.db"]
        for rel in dropped + ["keep.py", "sub/deploy.py"]:
            self._touch(rel)
        path, _ = pack_worktree(self.src_dir)
        unpack_worktree(path, self.dest_dir)
        os.unlink(path)
        for rel in dropped:
            self.assertFalse(os.path.exists(os.path.join(self.dest_dir, rel)), rel)
        self.assertTrue(os.path.isfile(os.path.join(self.dest_dir, "keep.py")))
        self.assertTrue(os.path.isfile(os.path.join(self.dest_dir, "sub", "deploy.py")))

    def test_defaults_cover_the_documented_names(self):
        for pattern in ("*.db", "*.db-shm", "*.db-wal", ".env", ".env.*",
                        "*.key", "id_rsa*", "id_ed25519*"):
            self.assertIn(pattern, DEFAULT_EXCLUDES)

    def test_warns_once_about_secret_looking_names(self):
        self._touch("my_token.txt")
        self._touch("credentials.json")
        self._touch("ok.txt")
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            path, _ = pack_worktree(self.src_dir)
        os.unlink(path)
        out = err.getvalue()
        self.assertEqual(out.count("warning:"), 1)
        self.assertIn("my_token.txt", out)
        self.assertIn("credentials.json", out)
        self.assertNotIn("ok.txt", out)

    def test_pem_files_are_packed_and_listed_in_the_warning(self):
        self._touch("testdata/db.cert.pem")
        self._touch("tls.key")
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            path, _ = pack_worktree(self.src_dir)
        unpack_worktree(path, self.dest_dir)
        os.unlink(path)
        self.assertTrue(os.path.isfile(os.path.join(self.dest_dir, "testdata", "db.cert.pem")))
        self.assertFalse(os.path.exists(os.path.join(self.dest_dir, "tls.key")))
        self.assertNotIn("*.pem", DEFAULT_EXCLUDES)
        self.assertIn("testdata/db.cert.pem", err.getvalue())


class TestS3WorktreeStorage(avocado.Test):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        tempfile.tempdir = self.temp_dir.name
        self.mock_client = mock.MagicMock(spec=S3Client)
        self.mock_client.head_object.return_value = None
        self.provider = S3StorageProvider(self.mock_client, "seine-test-bucket")

    def tearDown(self):
        tempfile.tempdir = None
        self.temp_dir.cleanup()

    def _bundle(self, text="hello from worktree"):
        src = os.path.join(self.temp_dir.name, "src")
        os.makedirs(src, exist_ok=True)
        with open(os.path.join(src, "main.txt"), "w") as f:
            f.write(text)
        return pack_worktree(src)

    def _stub_download(self, archive, sha=None, meta=True):
        def download(bucket, key, target):
            shutil.copyfile(archive, target)
        self.mock_client.download_file.side_effect = download
        sha = sha or _file_sha256(archive)
        self.mock_client.head_object.return_value = (
            {"x-amz-meta-sha256": sha} if meta else {"content-length": "1"})

    def test_push_worktree_archive_file_streams_from_disk(self):
        arc, digest = self._bundle()
        uploaded = []
        self.mock_client.upload_file.side_effect = \
            lambda bucket, key, path, **kw: uploaded.append((bucket, key, path))

        self.assertTrue(self.provider.push_worktree("myproject", digest, arc))

        self.assertEqual(uploaded, [("seine-test-bucket",
                                     f"worktrees/myproject/{digest}.tar.zst", arc)])
        self.mock_client.put_object.assert_not_called()

    def test_push_worktree_bytes_and_directory(self):
        arc, digest = self._bundle()
        with open(arc, "rb") as f:
            payload = f.read()
        self.assertTrue(self.provider.push_worktree("p", digest, payload))
        self.assertTrue(self.provider.push_worktree(
            "p", digest, os.path.join(self.temp_dir.name, "src")))
        self.assertEqual(self.mock_client.upload_file.call_count, 2)

    def test_has_worktree_checks_the_object_key(self):
        self.mock_client.head_object.return_value = {"content-length": "1"}
        self.assertTrue(self.provider.has_worktree("p", "dig"))
        self.mock_client.head_object.assert_called_with("seine-test-bucket", "worktrees/p/dig.tar.zst")
        self.mock_client.head_object.return_value = None
        self.assertFalse(self.provider.has_worktree("p", "dig"))

    def test_push_worktree_skips_existing_key(self):
        arc, digest = self._bundle()
        self.mock_client.head_object.return_value = {"x-amz-meta-sha256": digest}
        self.assertTrue(self.provider.push_worktree("p", digest, arc))
        self.mock_client.head_object.assert_called_once_with(
            "seine-test-bucket", f"worktrees/p/{digest}.tar.zst")
        self.mock_client.upload_file.assert_not_called()

    def test_push_worktree_rejects_wrong_digest(self):
        arc, _ = self._bundle()
        with self.assertRaises(StorageError):
            self.provider.push_worktree("p", "0" * 64, arc)
        self.mock_client.upload_file.assert_not_called()

    def test_push_worktree_missing_path(self):
        with self.assertRaises(StorageError):
            self.provider.push_worktree("p", "d", os.path.join(self.temp_dir.name, "nope"))

    def test_push_worktree_offline_handling(self):
        arc, digest = self._bundle()
        self.mock_client.upload_file.side_effect = Exception("network down")

        strict_provider = S3StorageProvider(
            self.mock_client, "seine-test-bucket", offline_mode="strict"
        )
        with self.assertRaises(StorageOfflineError):
            strict_provider.push_worktree("p", digest, arc)

        fallback_provider = S3StorageProvider(
            self.mock_client, "seine-test-bucket", offline_mode="fallback"
        )
        self.assertFalse(fallback_provider.push_worktree("p", digest, arc))

    def test_pull_worktree_success(self):
        arc, digest = self._bundle()
        self._stub_download(arc)

        dest = os.path.join(self.temp_dir.name, "extracted")
        res = self.provider.pull_worktree("myproject", digest, dest)
        self.assertEqual(res, os.path.abspath(dest))

        expected_key = f"worktrees/myproject/{digest}.tar.zst"
        self.mock_client.head_object.assert_called_once_with("seine-test-bucket", expected_key)
        self.assertEqual(self.mock_client.download_file.call_args[0][:2],
                         ("seine-test-bucket", expected_key))
        with open(os.path.join(dest, "main.txt")) as f:
            self.assertEqual(f.read(), "hello from worktree")
        self.assertEqual([n for n in os.listdir(self.temp_dir.name) if n.startswith(".pull-")], [])

    def test_pull_worktree_rejects_wrong_digest_argument(self):
        arc, _ = self._bundle()
        self._stub_download(arc)
        dest = os.path.join(self.temp_dir.name, "extracted")
        with self.assertRaises(StorageError):
            self.provider.pull_worktree("p", "f" * 64, dest)
        self.assertFalse(os.path.exists(dest))

    def test_pull_worktree_rejects_metadata_mismatch_or_absence(self):
        arc, digest = self._bundle()
        dest = os.path.join(self.temp_dir.name, "extracted")
        self._stub_download(arc, sha="0" * 64)
        with self.assertRaises(StorageError):
            self.provider.pull_worktree("p", digest, dest)
        self._stub_download(arc, meta=False)
        with self.assertRaises(StorageError):
            self.provider.pull_worktree("p", digest, dest)
        self.assertFalse(os.path.exists(dest))

    def test_pull_worktree_rejects_tampered_archive(self):
        arc, digest = self._bundle()
        self._stub_download(arc)
        with open(arc, "ab") as f:
            f.write(b"tampered")
        dest = os.path.join(self.temp_dir.name, "extracted")
        # head metadata still carries the original sha256
        self.mock_client.head_object.return_value = {"x-amz-meta-sha256": digest}
        with self.assertRaises(StorageError):
            self.provider.pull_worktree("p", digest, dest)
        self.assertFalse(os.path.exists(dest))

    def test_pull_worktree_not_found(self):
        self.mock_client.head_object.return_value = None
        dest = os.path.join(self.temp_dir.name, "dest")
        self.assertIsNone(self.provider.pull_worktree("myproject", "missing", dest))

        self.mock_client.head_object.side_effect = S3NotFoundError("not found")
        self.assertIsNone(self.provider.pull_worktree("myproject", "missing", dest))


class TestRemoteClientUpload(avocado.Test):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        tempfile.tempdir = self.temp_dir.name

    def tearDown(self):
        tempfile.tempdir = None
        self.temp_dir.cleanup()

    @mock.patch("requests.post")
    def test_upload_worktree(self, mock_post):
        mock_resp = mock.MagicMock()
        mock_resp.json.return_value = {"digest": "d123", "project": "proj", "bucket": "bkt"}
        mock_resp.status_code = 200
        mock_post.return_value = mock_resp

        arc_path = os.path.join(self.temp_dir.name, "test.tar.zst")
        with open(arc_path, "wb") as f:
            f.write(b"data")

        res = upload_worktree("http://localhost:8000/", "proj", arc_path, token="pat-1234")
        self.assertEqual(res, {"digest": "d123", "project": "proj", "bucket": "bkt"})

        mock_post.assert_called_once()
        url = mock_post.call_args[0][0]
        self.assertEqual(url, "http://localhost:8000/api/v1/projects/proj/worktrees")
        headers = mock_post.call_args[1].get("headers", {})
        self.assertEqual(headers.get("Authorization"), "Bearer pat-1234")
        self.assertEqual(headers.get("Content-Type"), "application/octet-stream")
        self.assertNotIn("files", mock_post.call_args[1])
        self.assertIn("data", mock_post.call_args[1])


class TestSparseWorktree(avocado.Test):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.temp_dir.name, "src")
        for name in ("main.yaml", "README.md", "pkg/debian/control",
                     "pkg/debian/id_rsa", "other/x.yaml"):
            full = os.path.join(self.root, name)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w") as f:
                f.write(name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _pack(self, names):
        out = os.path.join(self.temp_dir.name, "w.tar.zst")
        paths = [os.path.join(self.root, n) for n in names]
        _, digest = pack_sparse_worktree(self.root, paths, out_path=out)
        with _zstd_reader(out) as reader, tarfile.open(fileobj=reader, mode="r|*") as tar:
            return digest, sorted(m.name for m in tar if m.isfile())

    def test_only_the_named_files_and_trees_are_packed(self):
        _, files = self._pack(["main.yaml", "pkg"])
        self.assertEqual(files, ["main.yaml", "pkg/debian/control"])

    def test_unrelated_edits_keep_the_digest(self):
        before, _ = self._pack(["main.yaml"])
        with open(os.path.join(self.root, "README.md"), "w") as f:
            f.write("changed")
        self.assertEqual(self._pack(["main.yaml"])[0], before)

    def test_a_path_outside_the_root_is_refused(self):
        with self.assertRaises(ValueError):
            pack_sparse_worktree(self.root, ["/etc/hostname"])

    def test_a_staged_file_travels_under_its_archive_name(self):
        outside = os.path.join(self.temp_dir.name, "gist.yaml")
        with open(outside, "w") as f:
            f.write("a: 1\n")
        out = os.path.join(self.temp_dir.name, "w.tar.zst")
        pack_sparse_worktree(
            self.root, [os.path.join(self.root, "main.yaml"), outside], out_path=out,
            staged={".seine-sideload/0-gist.yaml": outside})
        dest = os.path.join(self.temp_dir.name, "dest")
        unpack_worktree(out, dest)
        with open(os.path.join(dest, ".seine-sideload/0-gist.yaml")) as f:
            self.assertEqual(f.read(), "a: 1\n")
        self.assertTrue(os.path.isfile(os.path.join(dest, "main.yaml")))

    def test_the_full_bundle_carries_staged_files_too(self):
        outside = os.path.join(self.temp_dir.name, "gist.yaml")
        with open(outside, "w") as f:
            f.write("a: 1\n")
        out = os.path.join(self.temp_dir.name, "w.tar.zst")
        pack_worktree(self.root, out_path=out, staged={".seine-sideload/0-gist.yaml": outside})
        with _zstd_reader(out) as reader, tarfile.open(fileobj=reader, mode="r|*") as tar:
            self.assertIn(".seine-sideload/0-gist.yaml", [m.name for m in tar])

    def test_the_bundle_unpacks(self):
        out = os.path.join(self.temp_dir.name, "w.tar.zst")
        pack_sparse_worktree(self.root, [os.path.join(self.root, "pkg")], out_path=out)
        dest = os.path.join(self.temp_dir.name, "dest")
        unpack_worktree(out, dest)
        self.assertTrue(os.path.isfile(os.path.join(dest, "pkg/debian/control")))

    def test_a_symlink_is_kept_with_its_target(self):
        os.makedirs(os.path.join(self.root, "shared"))
        with open(os.path.join(self.root, "shared/a.patch"), "w") as f:
            f.write("p")
        os.makedirs(os.path.join(self.root, "patches"))
        os.symlink("../shared/a.patch", os.path.join(self.root, "patches/a.patch"))
        digest, files = self._pack(["patches/a.patch"])
        self.assertEqual(files, ["shared/a.patch"])
        out = os.path.join(self.temp_dir.name, "w.tar.zst")
        with _zstd_reader(out) as reader, tarfile.open(fileobj=reader, mode="r|*") as tar:
            self.assertIn("patches/a.patch", [m.name for m in tar if m.issym()])

    def test_a_symlinked_directory_in_a_tree_is_kept(self):
        os.symlink("debian", os.path.join(self.root, "pkg/alias"))
        out = os.path.join(self.temp_dir.name, "w.tar.zst")
        pack_sparse_worktree(self.root, [os.path.join(self.root, "pkg")], out_path=out)
        with _zstd_reader(out) as reader, tarfile.open(fileobj=reader, mode="r|*") as tar:
            self.assertIn("pkg/alias", [m.name for m in tar if m.issym()])

    def test_a_dir_mtime_does_not_change_the_digest(self):
        before, _ = self._pack(["pkg"])
        os.utime(os.path.join(self.root, "pkg/debian"), (1, 1))
        os.utime(os.path.join(self.root, "pkg"), (2, 2))
        self.assertEqual(self._pack(["pkg"])[0], before)

    def test_the_root_can_be_a_tree(self):
        _, files = self._pack(["."])
        self.assertIn("main.yaml", files)
        self.assertNotIn("./main.yaml", files)

    def test_a_missing_path_is_refused(self):
        with self.assertRaises(ValueError):
            pack_sparse_worktree(self.root, [os.path.join(self.root, "nope")])

    def test_outside_the_root_has_its_own_error(self):
        with self.assertRaises(OutsideRootError):
            pack_sparse_worktree(self.root, ["/etc/hostname"])

    def test_secret_looking_names_are_warned_about(self):
        with open(os.path.join(self.root, "pkg/token.txt"), "w") as f:
            f.write("t")
        with mock.patch("sys.stderr", io.StringIO()) as err:
            self._pack(["pkg"])
        self.assertIn("token.txt", err.getvalue())
