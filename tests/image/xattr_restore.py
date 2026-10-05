#!/usr/bin/env python3

import avocado
import os
import sys
from unittest import mock

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine.imager.imager import Imager


class XattrRestoreUnitTests(avocado.Test):
    def test_rootfs_is_extracted_with_xattrs(self):
        imager = Imager.__new__(Imager)
        imager.source = mock.Mock()
        imager.source._tarball_for = mock.Mock(return_value="rootfs.tar")
        imager.source.options = {"files": [], "keep": False, "verbose": False}
        imager._write_fstab = mock.Mock()
        imager._label_selinux = mock.Mock()
        imager._ingest_containers = mock.Mock()
        g = mock.Mock()
        ph = mock.Mock()
        ph.bootlets = []
        mount = {"_prefix": "/", "type": "ext4"}

        imager._populate_source(
            g, ph, None, [mount], {id(mount): "/dev/sda1"}, {}, {id(mount): 1},
            container_devices=[])

        g.tar_in_opts.assert_called_once_with("rootfs.tar", "/", xattrs=True)
