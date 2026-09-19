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

from seine.imager import Imager

# _write_fstab() predicts a vfat filesystem's UUID before the on-disk
# write happens. What it may predict depends on whether the FAT
# rebuild that actually sets that UUID (_normalize_fat_tree()) will
# run at all -- gated behind '--reproducible' since 07bcee2a7. Naming
# the deterministic UUID regardless left a plain (non-reproducible)
# build's fstab pointing at a UUID mkfs.vfat never wrote, hanging
# /efi and /boot at boot.
def imager(reproducible):
    imager = Imager.__new__(Imager)
    imager.reproducible = reproducible
    return imager

def esp_mount():
    return {"_prefix": "/efi", "type": "vfat", "label": "esp",
           "_lvm": False, "identify": None}

class ReproducibleBuildPredictsTheFatSerialItWillWrite(avocado.Test):
    def test(self):
        i = imager(reproducible=True)
        m = esp_mount()
        g = mock.Mock()
        with mock.patch.object(Imager, "_fat_serial", return_value="008E79D0"):
            i._write_fstab(g, [m], {id(m): "/dev/sdb1"}, {})
        g.vfs_uuid.assert_not_called()
        written = g.write.call_args[0][1].decode()
        self.assertIn("UUID=008E-79D0", written)

class NonReproducibleBuildReadsBackTheRealUuid(avocado.Test):
    def test(self):
        i = imager(reproducible=False)
        m = esp_mount()
        g = mock.Mock()
        g.vfs_uuid.return_value = "430F-1BFD"
        i._write_fstab(g, [m], {id(m): "/dev/sdb1"}, {})
        g.vfs_uuid.assert_called_once_with("/dev/sdb1")
        written = g.write.call_args[0][1].decode()
        self.assertIn("UUID=430F-1BFD", written)

if __name__ == "__main__":
    avocado.main()
