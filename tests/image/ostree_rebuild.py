#!/usr/bin/env python3
# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import avocado
import os
import sys

from unittest import mock

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine.imager.imager import Imager

def imager(output_dir=None):
    i = Imager.__new__(Imager)
    i._guest_paths = {}
    i._output_dir = output_dir
    return i

# The ext rebuild reads each mount where the guest has it: its own
# prefix, or under /sysroot for an ostree build.
class MountPaths(avocado.Test):
    def test_a_mount_is_where_its_prefix_says_by_default(self):
        m = {"_prefix": "/var/"}
        self.assertEqual(imager()._where(m), "/var/")

    def test_an_ostree_build_mounts_it_elsewhere(self):
        i = imager()
        m = {"_prefix": "/var/"}
        i._guest_paths = {id(m): "/sysroot/ostree/deploy/debian/var"}
        self.assertEqual(i._where(m), "/sysroot/ostree/deploy/debian/var")

# xargs splits a long list over several cp calls and a hardlink only
# survives within one call, so the ostree sysroot is copied by one tar.
class TarCopy(avocado.Test):
    def lines(self, entries):
        i = imager(self.workdir)
        g = mock.Mock()
        lines = i._tar_copy_script(g, "/sysroot", "/scratch/content", entries,
                                   "sysroot", "/scratch/tools", 1700000000)
        return g, lines

    def test_every_entry_goes_through_one_archive_in_sorted_order(self):
        g, lines = self.lines(["/a", "/a/b", "/c"])
        creates = [l for l in lines if " -cf " in l]
        self.assertEqual(len(creates), 1)
        self.assertIn("--no-recursion --xattrs --numeric-owner", creates[0])
        self.assertEqual(len([l for l in lines if "xargs" in l]), 0)

    def test_the_list_is_uploaded_not_written(self):
        g, _ = self.lines(["/a", "/a/b"])
        self.assertEqual(g.upload.call_count, 1)
        g.write.assert_not_called()

    def test_directory_times_are_fixed_after_the_copy(self):
        _, lines = self.lines(["/a"])
        self.assertIn("touch -d @1700000000", lines[-1])
        self.assertTrue(any(" -xpf " in l for l in lines[:-1]))
