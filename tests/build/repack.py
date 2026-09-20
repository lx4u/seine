#!/usr/bin/env python3
# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import avocado
import hashlib
import os
import shutil
import sys
import tempfile

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine.deb import repack


class RepackFixture(avocado.Test):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="repack-test-")
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)


class ArReadWriteRoundTrips(RepackFixture):
    def test(self):
        path = os.path.join(self.tmpdir, "example.deb")
        members = [
            ["debian-binary", "0", "0", "0", "100644", b"2.0\n"],
            # Odd length, to exercise ar's 2-byte member alignment.
            ["data.tar", "0", "0", "0", "100644", b"odd"],
        ]
        repack.ar_write(path, members)
        self.assertEqual(repack.ar_read(path), members)


class TarMemberMissingRaises(RepackFixture):
    def test(self):
        members = [["debian-binary", "0", "0", "0", "100644", b"2.0\n"]]
        with self.assertRaises(ValueError):
            repack.tar_member(members, "data.tar")


class PatchChangesRewritesOnlyChangedEntries(RepackFixture):
    def test(self):
        data = b"whatever bytes a repacked .deb ended up with\n"
        path = os.path.join(self.tmpdir, "example.deb")
        with open(path, "wb") as f:
            f.write(data)

        other = os.path.join(self.tmpdir, "other.deb")
        with open(other, "wb") as f:
            f.write(b"unrelated, unchanged .deb bytes\n")

        changes = os.path.join(self.tmpdir, "example.changes")
        with open(changes, "w") as f:
            f.write(
                "Checksums-Sha1:\n"
                " 0000000000000000000000000000000000000000 99 example.deb\n"
                " 1111111111111111111111111111111111111111 32 other.deb\n"
                "Checksums-Sha256:\n"
                " %s 99 example.deb\n"
                " %s 32 other.deb\n"
                "Files:\n"
                " %s 99 kernel optional example.deb\n"
                " %s 32 kernel optional other.deb\n"
                % ("0" * 64, "1" * 64, "0" * 32, "1" * 32))

        repack.patch_changes(changes, self.tmpdir, ["example.deb"])
        with open(changes) as f:
            patched = f.read()

        self.assertIn(" %s %d example.deb\n"
                      % (hashlib.sha1(data).hexdigest(), len(data)), patched)
        self.assertIn(" %s %d example.deb\n"
                      % (hashlib.sha256(data).hexdigest(), len(data)), patched)
        self.assertIn(" %s %d kernel optional example.deb\n"
                      % (hashlib.md5(data).hexdigest(), len(data)), patched)
        # other.deb was never touched -- its stale-on-purpose lines survive.
        self.assertIn("1111111111111111111111111111111111111111 32 other.deb",
                      patched)
        self.assertIn("1" * 64 + " 32 other.deb", patched)
        self.assertIn("1" * 32 + " 32 kernel optional other.deb", patched)


if __name__ == "__main__":
    avocado.main()
