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
        imager.source._tarball_for = mock.Mock(return_value="/deploy/rootfs.tar")
        imager._tar_shares = {"/deploy": "seine_rootfs0"}
        imager.source.options = {"files": [], "keep": False, "verbose": False}
        imager._write_fstab = mock.Mock()
        imager._label_selinux = mock.Mock()
        imager._ingest_containers = mock.Mock()
        g = mock.Mock()
        ph = mock.Mock()
        ph.bootlets = []
        ph.plan_links.return_value = ([], [])
        mount = {"_prefix": "/", "type": "ext4"}

        with mock.patch("seine.imager.imager.tarfile.open"):
            imager._populate_source(
                g, ph, None, [mount], {id(mount): "/dev/sda1"}, {}, {id(mount): 1},
                container_devices=[])

        # The appliance's own tar, not tar_in: its wire format changes
        # between libguestfs releases.
        g.tar_in_opts.assert_not_called()
        scripts = [call.args[1][0] for call in g.debug.call_args_list]
        self.assertIn("mount -t 9p", scripts[0])
        self.assertIn("tar -xf /run/seine_rootfs0/rootfs.tar", scripts[1])
        self.assertIn("--xattrs '--xattrs-include=*' -C /sysroot", scripts[1])
        self.assertIn("umount /run/seine_rootfs0", scripts[2])

    def test_one_share_serves_every_tarball_of_a_directory(self):
        imager = Imager.__new__(Imager)
        imager.source = mock.Mock()
        paths = {None: "/deploy/a.tar", "x": "/deploy/b.tar", "y": "/other/c.tar"}
        imager.source._tarball_for = mock.Mock(side_effect=paths.get)
        g = mock.Mock()

        imager._share_tarballs(g, [None, "x", "y"])

        self.assertEqual(imager._tar_shares,
                         {"/deploy": "seine_rootfs0", "/other": "seine_rootfs1"})
        self.assertEqual(g.config.call_count, 2)
        self.assertIn("path=/deploy,mount_tag=seine_rootfs0,",
                      g.config.call_args_list[0].args[1])
        self.assertIn("readonly=on", g.config.call_args_list[0].args[1])
