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

# grub-install embeds the serial of the ESP it writes to; the FAT rebuild
# gives the volume a fixed one later, so it is set right after mkfs.
class FatSerial(avocado.Test):
    def pinned(self, signature):
        i = imager()
        g = mock.Mock()
        g.pread_device.return_value = signature
        i._pin_fat_serial(g, "/dev/sda1", "A1B2C3D4")
        return g.pwrite_device.call_args.args

    def test_fat32_keeps_its_volume_id_at_0x43_little_endian(self):
        self.assertEqual(self.pinned(b"FAT32   "),
                         ("/dev/sda1", bytes([0xD4, 0xC3, 0xB2, 0xA1]), 0x43))

    def test_fat16_keeps_it_at_0x27(self):
        self.assertEqual(self.pinned(b"        ")[2], 0x27)

    def test_a_reproducible_mkfs_pins_what_the_rebuild_will_write(self):
        i = imager()
        i.reproducible = True
        g = mock.Mock()
        g.pread_device.return_value = b"FAT32   "
        part = {"type": "vfat", "label": "esp"}
        with mock.patch.object(Imager, "_fat_serial", return_value="008E79D0"), \
                mock.patch.object(Imager, "_uuid_for", return_value="u"):
            i._mkfs(g, part, "/dev/sda1")
        self.assertEqual(g.pwrite_device.call_args.args[1], bytes([0xD0, 0x79, 0x8E, 0x00]))

    def test_a_plain_mkfs_leaves_the_random_serial_alone(self):
        i = imager()
        i.reproducible = False
        g = mock.Mock()
        with mock.patch.object(Imager, "_uuid_for", return_value="u"), \
                mock.patch.object(Imager, "_pins_fat_serial", return_value=False):
            i._mkfs(g, {"type": "vfat", "label": "esp"}, "/dev/sda1")
        g.pwrite_device.assert_not_called()
