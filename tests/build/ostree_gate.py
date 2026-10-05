#!/usr/bin/env python3

import avocado
import io
import os
import sys
import tarfile

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine.build import BuildCmd
from seine.imager.ostree import REQUIRED, check_tarball

SPEC = """
distribution:
    release: trixie
image:
    filename: disk.img
    ostree:
        mode: %s
    partitions:
        - label: sysroot
          where: /
        - label: var
          where: /var
"""

class TheRootfsIsCheckedBeforeTheDisk(avocado.Test):
    def tarball(self, paths):
        path = os.path.join(self.workdir, "rootfs.tar")
        with tarfile.open(path, "w") as tar:
            for name in paths:
                info = tarfile.TarInfo("./" + name)
                tar.addfile(info, io.BytesIO(b""))
        return path

    def image(self, mode):
        spec = os.path.join(self.workdir, "main.yaml")
        with open(spec, "w") as f:
            f.write(SPEC % mode)
        build = BuildCmd()
        build.options["files"] = [spec]
        build.load_all([spec])
        build.parse()
        return build.image

    def test_a_complete_rootfs_passes(self):
        check_tarball(self.tarball(REQUIRED), "the image")

    def test_missing_pieces_are_named(self):
        with self.assertRaises(ValueError) as cm:
            check_tarball(self.tarball(REQUIRED[:2]), "the image")
        self.assertIn("'/usr/lib/ostree/ostree-prepare-root'", str(cm.exception))
        self.assertNotIn("'/usr/bin/dracut'", str(cm.exception))

    def test_initramfs_tools_is_refused(self):
        tarball = self.tarball(REQUIRED + ("usr/sbin/update-initramfs",))
        with self.assertRaises(ValueError) as cm:
            check_tarball(tarball, "the image")
        self.assertIn("needs dracut", str(cm.exception))

    def test_the_image_task_runs_the_check_when_ostree_is_on(self):
        image = self.image("standard")
        image._tarball = self.tarball(())
        with self.assertRaises(ValueError):
            image._check_ostree_sources()

    def test_a_disabled_image_is_not_looked_at(self):
        image = self.image("disabled")
        image._tarball = os.path.join(self.workdir, "does-not-exist.tar")
        image._check_ostree_sources()
