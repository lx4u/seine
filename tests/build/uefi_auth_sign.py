#!/usr/bin/env python3
# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import avocado
import hashlib
import io
import lzma
import os
import shutil
import sys
import tarfile
import tempfile

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine import uefi_auth_sign
from seine.deb import repack


# Stands in for seine-sbsign's 'sign-var' endpoint: appends a fixed,
# deterministic marker carrying the key/var/guid/timestamp used, enough
# to exercise the .deb repack without needing a real vault container.
class FakeVault:
    def __init__(self):
        self.calls = []

    def sbsign_auth(self, key, var, guid, esl, timestamp):
        self.calls.append((key, var, guid, esl, timestamp))
        return esl + b"<auth:%s:%s:%s:%d:%s>" % (
            key.encode(), var.encode(), guid.encode(), timestamp,
            hashlib.sha256(esl).hexdigest()[:8].encode())


def _ar_member(name, content):
    return [name, "0", "0", "0", "100644", content]

# A minimal but real .deb (control.tar + data.tar) shaped like what
# uefi_auth_sign.py reads.
def _build_deb(path, pk_content, extra_pk=True,
               pk_name="./usr/share/uefi-provision-keys/pk.auth"):
    data_buf = io.BytesIO()
    data_tar = tarfile.open(fileobj=data_buf, mode="w:", format=tarfile.GNU_FORMAT)

    other = b"not a pk\n"
    info = tarfile.TarInfo("./usr/share/doc/example/README")
    info.size = len(other)
    data_tar.addfile(info, io.BytesIO(other))

    if extra_pk:
        info = tarfile.TarInfo(pk_name)
        info.size = len(pk_content)
        data_tar.addfile(info, io.BytesIO(pk_content))
    data_tar.close()
    data_tar_bytes = data_buf.getvalue()

    md5sums = "%s  usr/share/doc/example/README\n" % hashlib.md5(other).hexdigest()
    if extra_pk:
        md5sums += ("%s  %s\n" % (hashlib.md5(pk_content).hexdigest(),
                                 pk_name[2:]))

    control_buf = io.BytesIO()
    control_tar = tarfile.open(fileobj=control_buf, mode="w:", format=tarfile.GNU_FORMAT)
    control = b"Package: example\nVersion: 1\n"
    info = tarfile.TarInfo("control")
    info.size = len(control)
    control_tar.addfile(info, io.BytesIO(control))
    info = tarfile.TarInfo("md5sums")
    info.size = len(md5sums.encode())
    control_tar.addfile(info, io.BytesIO(md5sums.encode()))
    control_tar.close()

    repack.ar_write(path, [
        _ar_member("debian-binary", b"2.0\n"),
        _ar_member("control.tar.xz",
                   lzma.compress(control_buf.getvalue(), preset=6,
                                format=lzma.FORMAT_XZ)),
        _ar_member("data.tar.xz",
                   lzma.compress(data_tar_bytes, preset=6,
                                format=lzma.FORMAT_XZ)),
    ])


class UefiAuthSignFixture(avocado.Test):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="uefi-auth-sign-test-")
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.vault = FakeVault()

    def deb(self, name="example.deb", pk_content=b"unsigned ESL bytes",
           extra_pk=True,
           pk_name="./usr/share/uefi-provision-keys/pk.auth"):
        path = os.path.join(self.tmpdir, name)
        _build_deb(path, pk_content, extra_pk, pk_name)
        return path


class HasPkAuthFindsAndSkips(UefiAuthSignFixture):
    def test_finds_one(self):
        self.assertTrue(uefi_auth_sign.has_pk_auth(self.deb()))

    def test_skips_a_deb_without_one(self):
        self.assertFalse(uefi_auth_sign.has_pk_auth(self.deb(extra_pk=False)))


class ResignSignsAndPatchesMd5sums(UefiAuthSignFixture):
    def test(self):
        path = self.deb()
        changed = uefi_auth_sign.resign(path, self.vault, "uefi-secureboot", 12345)
        self.assertEqual(
            changed, {"usr/share/uefi-provision-keys/pk.auth"})
        self.assertEqual(self.vault.calls, [
            ("uefi-secureboot", "PK", uefi_auth_sign.PK_GUID,
             b"unsigned ESL bytes", 12345)])

        members = repack.ar_read(path)
        by_name = {m[0]: m[5] for m in members}
        data_tar = lzma.decompress(by_name["data.tar.xz"])
        with tarfile.open(fileobj=io.BytesIO(data_tar), mode="r:") as tf:
            auth = tf.extractfile(
                "./usr/share/uefi-provision-keys/pk.auth").read()
        self.assertTrue(auth.startswith(b"unsigned ESL bytes<auth:"))

        control_tar = lzma.decompress(by_name["control.tar.xz"])
        with tarfile.open(fileobj=io.BytesIO(control_tar), mode="r:") as tf:
            md5sums = tf.extractfile("md5sums").read().decode()
        self.assertIn("%s  usr/share/uefi-provision-keys/pk.auth"
                      % hashlib.md5(auth).hexdigest(), md5sums)
        # Untouched: same content, same hash as before.
        self.assertIn("usr/share/doc/example/README", md5sums)


class ResignSkipsADebWithoutPk(UefiAuthSignFixture):
    def test(self):
        path = self.deb(extra_pk=False)
        with open(path, "rb") as f:
            before = f.read()
        changed = uefi_auth_sign.resign(path, self.vault, "uefi-secureboot", 12345)
        self.assertEqual(changed, set())
        self.assertEqual(self.vault.calls, [])
        with open(path, "rb") as f:
            self.assertEqual(f.read(), before)


class ResignIsReproducible(UefiAuthSignFixture):
    def test(self):
        path1 = self.deb("one.deb")
        path2 = self.deb("two.deb")
        uefi_auth_sign.resign(path1, self.vault, "uefi-secureboot", 12345)
        uefi_auth_sign.resign(path2, FakeVault(), "uefi-secureboot", 12345)
        with open(path1, "rb") as f:
            b1 = f.read()
        with open(path2, "rb") as f:
            b2 = f.read()
        self.assertEqual(b1, b2)


if __name__ == "__main__":
    avocado.main()
