#!/usr/bin/env python3
# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import avocado
import os
import sys

sys.path.append(os.path.dirname(os.path.realpath(__file__)))

from reproducible_base import ReproducibleDiskImage
from seine.utils import HOST_ARCH


# Sibling of reproducible_disk.py and reproducible_disk_lvm.py, verifying that
# an image configured with image: watchdog: 30s auto-creates its EFI Boot
# Guard config partitions with deterministic FAT formatting and BGENV.DAT
# environment blocks, producing byte-identical disk images across two builds.
class DiskImageWithEfiBootGuardIsByteIdenticalAcrossTwoBuilds(ReproducibleDiskImage, avocado.Test):
    """
    :avocado: tags=full,container
    """
    timeout = 3600
    FILENAME = "reproducible-disk-efibootguard.img"

    def specification(self):
        where = os.path.join(self.workdir, "reproducible-disk-efibootguard.yml")
        with open(where, "w") as f:
            f.write(
                "distribution:\n"
                "    release: bookworm\n"
                "    architecture: %(arch)s\n"
                "    architectures: [%(arch)s]\n"
                "    uri: https://snapshot.debian.org/archive/debian/%(ts)s\n"
                "    feeds:\n"
                "        - suite: bookworm\n"
                "          valid-until: false\n"
                "        - suite: bookworm-updates\n"
                "          valid-until: false\n"
                "        - suite: bookworm-security\n"
                "          uri: https://snapshot.debian.org/archive/debian-security/%(ts)s\n"
                "          valid-until: false\n"
                "packages:\n"
                "    - source: apt://busybox\n"
                "      profiles: [nocheck]\n"
                "image:\n"
                "    filename: reproducible-disk-efibootguard.img\n"
                "    table: gpt\n"
                "    watchdog: 30s\n"
                "    size: 512MiB\n"
                "    partitions:\n"
                "        - label: esp\n"
                "          where: /efi\n"
                "          type: vfat\n"
                "          size: 64MiB\n"
                "        - label: root\n"
                "          where: /\n"
                "          size: 300MiB\n"
                % {"arch": HOST_ARCH, "ts": self.SNAPSHOT}
            )
        return [where]
