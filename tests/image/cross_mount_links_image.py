#!/usr/bin/env python3

import avocado
import os
import shutil
import subprocess
import sys

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine.utils import HOST_ARCH
from tests.testutils import prune_on_pass

# A real build, so it does not run unless asked for.
PLAN = os.environ.get("SEINE_TEST_PLAN", "")

# A hardlink between two partitions cannot exist on disk. forky's kernel
# package links /usr/lib/modules/<v>/vmlinuz to /boot/vmlinuz-<v>, so a
# forky image with its own /boot shows it. The image must hold two
# copies while /usr keeps its other hardlinks.
class KernelFilesOnSeparateBoot(avocado.Test):
    """
    :avocado: tags=full,container
    """
    timeout = 3600

    def setUp(self):
        if PLAN != "full":
            self.cancel("SEINE_TEST_PLAN=full builds an image; this takes a while")
        if shutil.which("podman") is None:
            self.cancel("podman is needed to build an image")
        try:
            import guestfs
        except ImportError as e:
            self.cancel(f"python3-guestfs is missing: {e}")

    def tearDown(self):
        prune_on_pass(self)

    def specification(self):
        disk = os.path.join(self.workdir, "disk.img")
        spec = os.path.join(self.workdir, "main.yaml")
        with open(spec, "w") as f:
            f.write(
                "requires:\n"
                f"    - stdlib:debian/{HOST_ARCH}.yml\n"
                "    - stdlib:debian/forky.yml\n"
                "image:\n"
                f"    filename: {disk}\n"
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
                "        - label: rootfs\n"
                "          type: ext4\n"
                "          size: 2GiB\n"
                "          where: /\n")
        return spec, disk

    def test_links_across_mounts_become_copies_and_other_links_stay(self):
        spec, disk = self.specification()

        environment = dict(os.environ)
        environment["PATH"] = f"{os.path.dirname(sys.executable)}:{environment.get('PATH', '')}"
        log = os.path.join(self.outputdir, "build.log")
        with open(log, "w") as f:
            built = subprocess.run(
                [sys.executable, "-u", "./seine.py", "build", "-v", spec],
                cwd=path_to_sources, env=environment, stdout=f,
                stderr=subprocess.STDOUT)
        self.assertEqual(built.returncode, 0, f"building the disk failed, see {log}")

        import guestfs
        g = guestfs.GuestFS(python_return_dict=True)
        g.add_drive_opts(disk, format="raw", readonly=True)
        g.launch()
        try:
            # sda1 is the efi partition, sda2 /boot, sda3 the root
            g.mount_ro("/dev/sda3", "/")
            g.mount_ro("/dev/sda2", "/boot")

            versions = g.ls("/usr/lib/modules")
            self.assertTrue(versions, "no kernel modules directory in /usr/lib/modules")
            checked = 0
            for version in versions:
                for name in ["vmlinuz", "config"]:
                    inside = f"/usr/lib/modules/{version}/{name}"
                    if not g.is_file(inside, followsymlinks=False):
                        continue
                    outside = f"/boot/{name}-{version}"
                    self.assertTrue(g.is_file(outside, followsymlinks=False),
                                    f"{outside} is missing")
                    self.assertEqual(g.checksum("sha256", inside),
                                     g.checksum("sha256", outside),
                                     f"{inside} differs from {outside}")
                    self.assertEqual(g.stat(inside)["nlink"], 1)
                    checked += 1
            self.assertTrue(checked, "no kernel file was found under /usr/lib/modules")

            linked = [name for name in g.ls("/usr/bin")
                      if g.is_file(f"/usr/bin/{name}", followsymlinks=False)
                      and g.stat(f"/usr/bin/{name}")["nlink"] > 1]
            self.assertTrue(linked, "/usr/bin lost all its hardlinks")

            hosts = g.stat("/etc/hosts")
            self.assertEqual(hosts["mode"] & 0o777, 0o644)
            self.assertIn("localhost", g.cat("/etc/hosts"))
        finally:
            g.close()
