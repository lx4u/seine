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

from seine import uki_sign
from seine.deb import repack


# Stands in for seine-sbsign's 'sign' endpoint: appends a fixed,
# deterministic marker carrying the key and timestamp used, enough to
# exercise the .deb repack without needing a real vault container.
class FakeVault:
    def __init__(self):
        self.calls = []

    def sbsign_sign(self, key, pe, timestamp):
        self.calls.append((key, pe, timestamp))
        return pe + b"<signed:%s:%d:%s>" % (
            key.encode(), timestamp, hashlib.sha256(pe).hexdigest()[:8].encode())


def _ar_member(name, content):
    return [name, "0", "0", "0", "100644", content]

# A minimal but real .deb (control.tar + data.tar) shaped like what
# uki_sign.py reads. 'efi_name' picks the file the built UKI (or
# addon) lands at.
def _build_deb(path, efi_content, extra_efi=True,
               efi_name="./boot/EFI/Linux/linux-uki-amd64.efi"):
    data_buf = io.BytesIO()
    data_tar = tarfile.open(fileobj=data_buf, mode="w:", format=tarfile.GNU_FORMAT)

    other = b"not a uki\n"
    info = tarfile.TarInfo("./usr/share/doc/example/README")
    info.size = len(other)
    data_tar.addfile(info, io.BytesIO(other))

    if extra_efi:
        info = tarfile.TarInfo(efi_name)
        info.size = len(efi_content)
        data_tar.addfile(info, io.BytesIO(efi_content))
    data_tar.close()
    data_tar_bytes = data_buf.getvalue()

    md5sums = "%s  usr/share/doc/example/README\n" % hashlib.md5(other).hexdigest()
    if extra_efi:
        md5sums += ("%s  %s\n" % (hashlib.md5(efi_content).hexdigest(),
                                 efi_name[2:]))

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


class UkiSignFixture(avocado.Test):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="uki-sign-test-")
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.vault = FakeVault()

    def deb(self, name="example.deb", efi_content=b"unsigned PE bytes",
           extra_efi=True,
           efi_name="./boot/EFI/Linux/linux-uki-amd64.efi"):
        path = os.path.join(self.tmpdir, name)
        _build_deb(path, efi_content, extra_efi, efi_name)
        return path


class HasUkiFindsAndSkips(UkiSignFixture):
    def test_finds_an_efi(self):
        self.assertTrue(uki_sign.has_uki(self.deb()))

    def test_finds_an_addon_efi(self):
        self.assertTrue(uki_sign.has_uki(self.deb(
            efi_name="./boot/EFI/Linux/linux-uki-amd64.efi.extra.d/quiet.addon.efi")))

    def test_skips_a_deb_without_one(self):
        self.assertFalse(uki_sign.has_uki(self.deb(extra_efi=False)))


class ResignSignsAndPatchesMd5sums(UkiSignFixture):
    def test(self):
        path = self.deb()
        changed = uki_sign.resign(path, self.vault, "pc-uki-secureboot", 12345)
        self.assertEqual(
            changed, {"boot/EFI/Linux/linux-uki-amd64.efi"})
        self.assertEqual(self.vault.calls,
                         [("pc-uki-secureboot", b"unsigned PE bytes", 12345)])

        members = repack.ar_read(path)
        by_name = {m[0]: m[5] for m in members}
        data_tar = lzma.decompress(by_name["data.tar.xz"])
        with tarfile.open(fileobj=io.BytesIO(data_tar), mode="r:") as tf:
            efi = tf.extractfile(
                "./boot/EFI/Linux/linux-uki-amd64.efi").read()
        self.assertTrue(efi.startswith(b"unsigned PE bytes<signed:"))

        control_tar = lzma.decompress(by_name["control.tar.xz"])
        with tarfile.open(fileobj=io.BytesIO(control_tar), mode="r:") as tf:
            md5sums = tf.extractfile("md5sums").read().decode()
        self.assertIn("%s  boot/EFI/Linux/linux-uki-amd64.efi"
                      % hashlib.md5(efi).hexdigest(), md5sums)
        # Untouched: same content, same hash as before.
        self.assertIn("usr/share/doc/example/README", md5sums)


class ResignMatchesAddonSuffixToo(UkiSignFixture):
    def test(self):
        path = self.deb(
            efi_name="./boot/EFI/Linux/linux-uki-amd64.efi.extra.d/quiet.addon.efi")
        changed = uki_sign.resign(path, self.vault, "pc-uki-secureboot", 12345)
        self.assertEqual(changed, {
            "boot/EFI/Linux/linux-uki-amd64.efi.extra.d/quiet.addon.efi"})


class ResignSkipsADebWithoutUki(UkiSignFixture):
    def test(self):
        path = self.deb(extra_efi=False)
        with open(path, "rb") as f:
            before = f.read()
        changed = uki_sign.resign(path, self.vault, "pc-uki-secureboot", 12345)
        self.assertEqual(changed, set())
        self.assertEqual(self.vault.calls, [])
        with open(path, "rb") as f:
            self.assertEqual(f.read(), before)


class ResignIsReproducible(UkiSignFixture):
    def test(self):
        path1 = self.deb("one.deb")
        path2 = self.deb("two.deb")
        uki_sign.resign(path1, self.vault, "pc-uki-secureboot", 12345)
        uki_sign.resign(path2, FakeVault(), "pc-uki-secureboot", 12345)
        with open(path1, "rb") as f:
            b1 = f.read()
        with open(path2, "rb") as f:
            b2 = f.read()
        self.assertEqual(b1, b2)


if __name__ == "__main__":
    avocado.main()
