#!/usr/bin/env python3
# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import avocado
import io
import os
import sys
import tarfile
import time

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine.image import Image

def add_file(tar, name, mtime=0):
    info = tarfile.TarInfo(name)
    info.size = 3
    info.mtime = mtime
    tar.addfile(info, io.BytesIO(b"abc"))

def add_symlink(tar, name, target):
    info = tarfile.TarInfo(name)
    info.type = tarfile.SYMTYPE
    info.linkname = target
    tar.addfile(info)

# The build container's resolv.conf is the build host's one.
class CleanTarball(avocado.Test):
    def clean(self, add):
        path = os.path.join(self.workdir, "rootfs.tar")
        with tarfile.open(path, "w") as tar:
            add(tar)
        image = Image(None, {"keep": False, "verbose": False})
        image._clean_tarball(path)
        with tarfile.open(path) as tar:
            return {m.name: m for m in tar.getmembers()}

    def test_a_regular_resolv_conf_is_dropped_in_both_name_forms(self):
        for name in ["etc/resolv.conf", "./etc/resolv.conf"]:
            members = self.clean(lambda tar: (add_file(tar, name),
                                              add_file(tar, "etc/hostname")))
            self.assertEqual(list(members), ["etc/hostname"], name)

    def test_a_symlinked_resolv_conf_stays(self):
        members = self.clean(lambda tar: add_symlink(
            tar, "etc/resolv.conf", "../run/systemd/resolve/stub-resolv.conf"))
        self.assertEqual(list(members), ["etc/resolv.conf"])

    def test_other_members_are_kept_and_recent_ones_get_the_epoch(self):
        recent = int(time.time()) + 60
        members = self.clean(lambda tar: (add_file(tar, "etc/resolv.conf.bak"),
                                          add_file(tar, "usr/bin/new", recent)))
        self.assertEqual(sorted(members), ["etc/resolv.conf.bak", "usr/bin/new"])
        self.assertLess(members["usr/bin/new"].mtime, recent)
