#!/usr/bin/env python3

import avocado
import os
import sys

sys.path.append(os.path.dirname(os.path.realpath(__file__)))

from reproducible_base import ReproducibleDiskImage
from seine.utils import HOST_ARCH

COMMON = os.path.join(os.path.dirname(os.path.realpath(__file__)),
                      "..", "..", "examples", "common")

# Sibling of reproducible_disk.py's test, for an ostree sysroot: the two
# images must match byte for byte, which includes the commit.
class OstreeDiskImageIsByteIdenticalAcrossTwoBuilds(ReproducibleDiskImage, avocado.Test):
    """
    :avocado: tags=full,container
    """
    timeout = 3600
    FILENAME = "reproducible-ostree.img"
    RELEASE = "trixie"
    # The common amd64 fragment brings grub and the kernel; the
    # systemd-boot variant below lists its own.
    BOOT = ("requires:\n"
            "    - %(common)s/amd64\n")
    BOOT_PLAYBOOK = ""
    ESP_SIZE = "64MiB"

    def setUp(self):
        super().setUp()
        if HOST_ARCH != "amd64":
            self.cancel("this spec's kernel/bootloader packages are amd64-only")

    def specification(self):
        where = os.path.join(self.workdir, "reproducible-ostree.yml")
        with open(where, "w") as f:
            f.write((
                self.BOOT +
                "distribution:\n"
                "    release: trixie\n"
                "    architecture: amd64\n"
                "    architectures: [amd64]\n"
                "    uri: https://snapshot.debian.org/archive/debian/%(ts)s\n"
                "    feeds:\n"
                "        - suite: trixie\n"
                "          valid-until: false\n"
                "        - suite: trixie-updates\n"
                "          release: trixie\n"
                "          valid-until: false\n"
                "        - suite: trixie-security\n"
                "          release: trixie\n"
                "          uri: https://snapshot.debian.org/archive/debian-security/%(ts)s\n"
                "          valid-until: false\n"
                "playbook:\n"
                "    - name: dracut and ostree\n"
                "      priority: 700\n"
                "      tasks:\n"
                "          - name: install dracut, ostree and systemd\n"
                "            apt:\n"
                "                state: present\n"
                "                name: [dracut, dracut-config-generic, ostree, ostree-boot,\n"
                "                       systemd, systemd-sysv]\n" +
                self.BOOT_PLAYBOOK +
                "image:\n"
                "    filename: %(filename)s\n"
                "    table: gpt\n"
                "    ostree:\n"
                "        mode: standard\n"
                "    partitions:\n"
                "        - label: efi\n"
                "          type: vfat\n"
                "          size: %(esp)s\n"
                "          where: /efi\n"
                "          flags: [boot]\n"
                "        - label: sysroot\n"
                "          type: ext4\n"
                "          where: /\n"
                "        - label: var\n"
                "          type: ext4\n"
                "          size: 256MiB\n"
                "          where: /var\n"
                ) % {"ts": self.SNAPSHOT, "common": COMMON,
                     "esp": self.ESP_SIZE, "filename": self.FILENAME})
        return [where]


# The same with systemd-boot: the ESP also holds the kernel files.
class OstreeSystemdBootDiskImageIsByteIdenticalAcrossTwoBuilds(
        OstreeDiskImageIsByteIdenticalAcrossTwoBuilds):
    """
    :avocado: tags=full,container
    """
    FILENAME = "reproducible-ostree-sdboot.img"
    BOOT = ("requires:\n"
            "    - %(common)s/trixie\n"
            "imager:\n"
            "    kernel: linux-image-amd64\n")
    BOOT_PLAYBOOK = ("    - name: systemd-boot and the kernel\n"
                     "      priority: 800\n"
                     "      tasks:\n"
                     "          - name: install systemd-boot, kernel and firmware blobs\n"
                     "            apt:\n"
                     "                state: present\n"
                     "                name: [systemd-boot, linux-image-amd64, firmware-linux-free]\n")
    ESP_SIZE = "256MiB"
