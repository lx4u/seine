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

# One group's rootfs: dracut and ostree before the kernel, and a package
# of its own to tell the two sysroots apart.
GROUP = """
requires:
    - {common}/trixie

distribution:
    architecture: amd64

playbook:
    - name: dracut and ostree
      priority: 700
      tasks:
          - name: install dracut, ostree and systemd
            apt:
                name: [dracut, dracut-config-generic, ostree, ostree-boot, systemd, systemd-sysv]
                state: present
    - name: kernel and a package of its own
      priority: 800
      tasks:
          - name: install them
            apt:
                name: [linux-image-amd64, {package}]
                state: present
"""

DISK = """
requires:
    - {common}/trixie

distribution:
    architecture: amd64

multiconfig:
    main:
        - {main}
    other:
        - {other}

image:
    filename: {disk}
    table: gpt
    ostree:
        mode: standard
    partitions:
        - label: esp
          type: vfat
          size: 64MiB
          source: main
          where: /efi
          flags: [boot]
        - label: main-root
          type: ext4
          source: main
          where: /
        - label: main-var
          type: ext4
          size: 128MiB
          source: main
          where: /var
        - label: other-root
          type: ext4
          source: other
          where: /
        - label: other-var
          type: ext4
          size: 128MiB
          source: other
          where: /var
"""

# Two groups on one disk, each its own ostree sysroot with its own
# /var: both are staged and deployed one after the other, and neither
# sees the other's content.
class TwoOstreeSysrootsOnOneDisk(avocado.Test):
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

    def write(self, name, content):
        path = os.path.join(self.workdir, name)
        with open(path, "w") as f:
            f.write(content)
        return path

    def deployment(self, g, stateroot):
        names = [d for d in g.ls("/ostree/deploy/%s/deploy" % stateroot)
                 if not d.endswith(".origin")]
        self.assertEqual(len(names), 1, names)
        return "/ostree/deploy/%s/deploy/%s" % (stateroot, names[0])

    def test_each_group_gets_its_own_sysroot_and_var(self):
        disk = os.path.join(self.workdir, "disk.img")
        main = self.write("main.yaml", GROUP.format(common=COMMON, package="jq"))
        other = self.write("other.yaml", GROUP.format(common=COMMON, package="bc"))
        spec = self.write("disk.yaml", DISK.format(
            common=COMMON, main=main, other=other, disk=disk))
        environment = dict(os.environ)
        environment["PATH"] = "%s:%s" % (os.path.dirname(sys.executable),
                                         environment.get("PATH", ""))
        log = os.path.join(self.outputdir, "build.log")
        with open(log, "w") as f:
            built = subprocess.run(
                [sys.executable, "-u", "./seine.py", "build", "-v", spec],
                cwd=path_to_sources, env=environment, stdout=f,
                stderr=subprocess.STDOUT)
        self.assertEqual(built.returncode, 0, "build failed, see %s" % log)

        import guestfs
        g = guestfs.GuestFS(python_return_dict=True)
        g.add_drive_opts(disk, format="raw", readonly=True)
        g.launch()
        try:
            # sda1 esp, sda2 main-root, sda3 main-var, sda4 other-root,
            # sda5 other-var -- declaration order.
            for root, var, stateroot, own, foreign in (
                    ("/dev/sda2", "/dev/sda3", "main", "jq", "bc"),
                    ("/dev/sda4", "/dev/sda5", "other", "bc", "jq")):
                g.mount_ro(root, "/")
                self.assertTrue(
                    g.is_file("/ostree/repo/refs/heads/%s/amd64" % stateroot),
                    "no '%s/amd64' ref in %s" % (stateroot, root))
                deployment = self.deployment(g, stateroot)
                self.assertTrue(g.is_file("%s/usr/bin/%s" % (deployment, own)),
                                "%s's own %s never reached its sysroot" % (stateroot, own))
                self.assertFalse(g.is_file("%s/usr/bin/%s" % (deployment, foreign)),
                                 "%s leaked into %s's sysroot" % (foreign, stateroot))
                self.assertEqual(
                    [n for n in g.ls("/") if n.startswith(".seine")], [],
                    "the build left files on %s" % root)
                g.umount("/")

                g.mount_ro(var, "/")
                self.assertTrue(g.is_file("/.ostree-selabeled"),
                                "deploy did not seed %s" % var)
                g.umount("/")
        finally:
            g.close()
