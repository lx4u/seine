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

COMMON = os.path.join(path_to_sources, "examples", "common")

PLAN = os.environ.get("SEINE_TEST_PLAN", "")

SPEC = """
requires:
    - {common}/amd64
    - {common}/trixie

playbook:
    - name: dracut and ostree
      priority: 700
      tasks:
          - name: install dracut, ostree and systemd
            apt:
                name: [dracut, dracut-config-generic, ostree, ostree-boot, systemd, systemd-sysv]
                state: present

image:
    filename: {disk}
    table: gpt
    ostree:
        mode: standard
    partitions:
        - label: esp
          type: vfat
          size: 64MiB
          where: /efi
          flags: [boot]
        - label: sysroot
          type: ext4
          where: /
        - label: var
          type: ext4
          size: 256MiB
          where: /var
"""

# A 'standard' ostree image built end to end, then looked into with
# guestfs: one deployment of the committed rootfs, nothing of the build
# left on the sysroot, /var seeded on its own partition.
class StandardOstreeImageBuilds(avocado.Test):
    """
    :avocado: tags=full,container
    """
    timeout = 3600

    def setUp(self):
        if PLAN != "full":
            self.cancel("SEINE_TEST_PLAN=full builds an image; this takes a while")
        if HOST_ARCH != "amd64":
            self.cancel("this spec's kernel is amd64-only")
        if shutil.which("podman") is None:
            self.cancel("podman is needed to build an image")
        try:
            import guestfs
        except ImportError as e:
            self.cancel("python3-guestfs is missing: %s" % e)

    def tearDown(self):
        prune_on_pass(self)

    def test_the_sysroot_holds_one_deployment_of_the_commit(self):
        disk = os.path.join(self.workdir, "disk.img")
        spec = os.path.join(self.workdir, "main.yaml")
        with open(spec, "w") as f:
            f.write(SPEC.format(common=COMMON, disk=disk))
        log = os.path.join(self.outputdir, "build.log")
        with open(log, "w") as f:
            built = subprocess.run(
                [sys.executable, "-u", "./seine.py", "build", "-v", spec],
                cwd=path_to_sources, stdout=f, stderr=subprocess.STDOUT)
        self.assertEqual(built.returncode, 0, "build failed, see %s" % log)

        import guestfs
        g = guestfs.GuestFS(python_return_dict=True)
        g.add_drive_opts(disk, format="raw", readonly=True)
        g.launch()
        try:
            g.mount_ro("/dev/sda2", "/")
            self.assertIn("mode=bare", g.cat("/ostree/repo/config"))
            deployments = [d for d in g.ls("/ostree/deploy/debian/deploy")
                           if not d.endswith(".origin")]
            self.assertEqual(len(deployments), 1, deployments)
            deployment = "/ostree/deploy/debian/deploy/%s" % deployments[0]

            entries = g.ls("/boot/loader/entries")
            self.assertEqual(len(entries), 1, entries)
            entry = g.cat("/boot/loader/entries/%s" % entries[0])
            self.assertIn("options ostree=/ostree/boot.1/debian/", entry)

            self.assertEqual(g.cat("%s/etc/machine-id" % deployment), "",
                             "every device would share this machine-id")
            self.assertFalse(g.is_file("%s/etc/ssh/ssh_host_ed25519_key" % deployment))
            self.assertTrue(g.is_file("%s/usr/bin/ostree" % deployment))
            self.assertEqual(g.readlink("%s/home" % deployment), "var/home")

            leftovers = [n for n in g.ls("/") if n.startswith(".seine")]
            self.assertEqual(leftovers, [], "the build left files on the sysroot")
            g.umount("/")

            g.mount_ro("/dev/sda3", "/")
            self.assertTrue(g.is_file("/.ostree-selabeled"),
                            "deploy did not seed the /var partition")
            g.umount("/")
        finally:
            g.close()
