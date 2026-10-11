#!/usr/bin/env python3

import avocado
import os
import sys

sys.path.append(os.path.dirname(os.path.realpath(__file__)))

from reproducible_base import ReproducibleDiskImage
from seine.utils import HOST_ARCH

# Sibling of reproducible_disk.py's test for hardlinks across mounts (see
# cross_mount_links_image.py): the copies the imager makes must not change
# the bytes of the disk. forky is the release that has such links.
class CrossMountLinksDiskImageIsByteIdenticalAcrossTwoBuilds(ReproducibleDiskImage, avocado.Test):
    """
    :avocado: tags=full,container
    """
    timeout = 3600
    FILENAME = "reproducible-cross-mount-links.img"
    RELEASE = "forky"

    def setUp(self):
        super().setUp()
        if HOST_ARCH != "amd64":
            self.cancel("this spec's kernel/bootloader packages are amd64-only")

    # The snapshot pin is a side-loaded peer file: it amends the live
    # feeds of stdlib:debian/forky.yml field by field.
    def specification(self):
        pin = os.path.join(self.workdir, "snapshot-pin.yml")
        with open(pin, "w") as f:
            f.write(
                "distribution:\n"
                f"    uri: https://snapshot.debian.org/archive/debian/{self.SNAPSHOT}\n"
                "    feeds:\n"
                "        - suite: forky\n"
                "          valid-until: false\n"
                "        - suite: forky-updates\n"
                "          release: forky\n"
                "          valid-until: false\n"
                "        - suite: forky-security\n"
                "          release: forky\n"
                f"          uri: https://snapshot.debian.org/archive/debian-security/{self.SNAPSHOT}\n"
                "          valid-until: false\n")
        where = os.path.join(self.workdir, "reproducible-cross-mount-links.yml")
        with open(where, "w") as f:
            f.write(
                "requires:\n"
                "    - stdlib:debian/amd64.yml\n"
                "    - stdlib:debian/forky.yml\n"
                "image:\n"
                "    filename: reproducible-cross-mount-links.img\n"
                "    table: gpt\n"
                "    partitions:\n"
                "        - label: efi\n"
                "          type: vfat\n"
                "          size: 64MiB\n"
                "          where: /efi\n"
                "        - label: boot\n"
                "          type: ext4\n"
                "          size: 256MiB\n"
                "          where: /boot\n"
                "        - label: root\n"
                "          type: ext4\n"
                "          size: 2GiB\n"
                "          where: /\n")
        return [where, pin]
