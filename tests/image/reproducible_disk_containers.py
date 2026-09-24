#!/usr/bin/env python3

import avocado
import os
import sys

sys.path.append(os.path.dirname(os.path.realpath(__file__)))

from reproducible_base import ReproducibleDiskImage
from seine.utils import HOST_ARCH

# Sibling of reproducible_disk.py's test: builds the same snapshot-pinned spec
# including preloaded container images twice, and checks the two '.img' files
# are byte-for-byte identical.
class DiskImageWithContainersIsByteIdenticalAcrossTwoBuilds(ReproducibleDiskImage, avocado.Test):
    """
    :avocado: tags=full,container
    """
    timeout = 3600
    FILENAME = "reproducible-disk-containers.img"

    def specification(self):
        where = os.path.join(self.workdir, "reproducible-disk-containers.yml")
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
                "containers:\n"
                "    - image: docker.io/library/alpine:3.19\n"
                "      digests:\n"
                "          amd64: sha256:b58899f069c47216f6002a6850143dc6fae0d35eb8b0df9300bbe6327b9c2171\n"
                "          arm64: sha256:5cd72f301a291887075a70d8b14aed6ee228fc9fd8f65e1e7eaa072f2115efd6\n"
                "image:\n"
                "    filename: reproducible-disk-containers.img\n"
                "    table: gpt\n"
                "    size: 512MiB\n"
                "    partitions:\n"
                "        - label: root\n"
                "          type: ext4\n"
                "          size: 300MiB\n"
                "          where: /\n"
                "          flags: [primary]\n"
                "        - label: data\n"
                "          type: ext4\n"
                "          size: 180MiB\n"
                "          where: /var\n"
                "          flags: [primary]\n"
                % {"arch": HOST_ARCH, "ts": self.SNAPSHOT})
        return [where]
