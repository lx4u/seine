#!/usr/bin/env python3

import avocado
import os
import shutil
import subprocess
import sys

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)
sys.path.append(os.path.dirname(path_to_self))

import qemu_boot
from seine.utils import HOST_ARCH
from tests.testutils import prune_on_pass

EXAMPLES = os.path.join(path_to_sources, "examples")
COMMON = os.path.join(EXAMPLES, "common")

PLAN = os.environ.get("SEINE_TEST_PLAN", "")

# One group's rootfs: dracut and ostree before the kernel, grub from the
# common amd64 fragment, a serial console and a banner of its own.
GROUP = """
requires:
    - {common}/amd64
    - {common}/trixie
    - {pc_image}/grub-serial-console

playbook:
    - name: dracut and ostree
      priority: 700
      tasks:
          - name: install dracut, ostree and systemd
            apt:
                name: [dracut, dracut-config-generic, ostree, ostree-boot, systemd, systemd-sysv]
                state: present
    - name: a banner of its own
      priority: 950
      tasks:
          - name: set what getty prints before the login prompt
            copy:
                content: "{name}\\n"
                dest: /etc/issue
"""

# The same with systemd-boot: the common amd64 fragment installs grub, so
# what it adds is spelled out here, without it.
GROUP_SYSTEMD_BOOT = """
distribution:
    architecture: amd64
    architectures:
        - amd64

imager:
    kernel: linux-image-amd64
""" + GROUP.replace("    - {common}/amd64\n", "") + """    - name: systemd-boot and the kernel
      priority: 800
      tasks:
          - name: install systemd-boot, kernel and firmware blobs
            apt:
                name: [systemd-boot, linux-image-amd64, firmware-linux-free]
                state: present
"""

SINGLE_DISK = """
image:
    filename: {disk}
    table: gpt
    ostree:
        mode: standard
    partitions:
        - label: esp
          type: vfat
          size: 256MiB
          where: /efi
          flags: [boot]
        - label: sysroot
          type: ext4
          where: /
        - label: var
          type: ext4
          size: 128MiB
          where: /var
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

imager:
    boot: {owner}

image:
    filename: {disk}
    table: gpt
    ostree:
        mode: standard
    partitions:
        - label: esp
          type: vfat
          size: 256MiB
          source: {owner}
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

# Builds an ostree image with GRUB and boots it under OVMF to a login
# prompt on the serial console: the boot entry names the right root and
# ostree-prepare-root finds the deployment.
class OstreeImageBoots(avocado.Test):
    """
    :avocado: tags=full,container
    """
    timeout = 3600

    def setUp(self):
        if PLAN != "full":
            self.cancel("SEINE_TEST_PLAN=full builds an image; this takes a while")
        if HOST_ARCH != "amd64":
            self.cancel("this spec's kernel and boot loader are amd64-only")
        if shutil.which("podman") is None:
            self.cancel("podman is needed to build an image")
        reason = qemu_boot.cannot_boot()
        if reason:
            self.cancel(reason)

    def tearDown(self):
        prune_on_pass(self)

    def write(self, name, content):
        path = os.path.join(self.workdir, name)
        with open(path, "w") as f:
            f.write(content)
        return path

    def build(self, spec, name):
        environment = dict(os.environ)
        environment["PATH"] = "%s:%s" % (os.path.dirname(sys.executable),
                                         environment.get("PATH", ""))
        log = os.path.join(self.outputdir, "%s.log" % name)
        with open(log, "w") as f:
            built = subprocess.run(
                [sys.executable, "-u", "./seine.py", "build", "-v", spec],
                cwd=path_to_sources, env=environment, stdout=f,
                stderr=subprocess.STDOUT)
        self.assertEqual(built.returncode, 0, "build failed, see %s" % log)

    def boots(self, disk, name):
        workdir = os.path.join(self.workdir, "boot-" + name)
        os.makedirs(workdir)
        found, text = qemu_boot.boot_until(disk, workdir, "login:")
        with open(os.path.join(self.outputdir, "boot-%s.log" % name), "w") as f:
            f.write(text)
        self.assertTrue(found, "no 'login:' on the serial console, see boot-%s.log" % name)
        self.assertIn(name, text, "the banner of '%s' was not shown" % name)
        self.assertNotIn("emergency mode", text)

    def test_a_single_sysroot_boots(self):
        self.single_sysroot_boots(GROUP)

    def test_a_single_sysroot_boots_with_systemd_boot(self):
        self.single_sysroot_boots(GROUP_SYSTEMD_BOOT)

    def single_sysroot_boots(self, group):
        disk = os.path.join(self.workdir, "single.img")
        spec = self.write("single.yaml", (group + SINGLE_DISK).format(
            common=COMMON, pc_image=os.path.join(EXAMPLES, "pc-image"),
            name="single-os", disk=disk))
        self.build(spec, "single")
        self.boots(disk, "single-os")

    # The boot owner is the first menu entry, so building with each in
    # turn boots each group's own sysroot.
    def test_each_group_of_a_two_sysroot_disk_boots(self):
        self.each_group_boots(GROUP)

    def test_each_group_of_a_two_sysroot_disk_boots_with_systemd_boot(self):
        self.each_group_boots(GROUP_SYSTEMD_BOOT)

    def each_group_boots(self, group):
        pc_image = os.path.join(EXAMPLES, "pc-image")
        groups = {}
        for name in ("main", "other"):
            groups[name] = self.write("%s.yaml" % name, group.format(
                common=COMMON, pc_image=pc_image, name="%s-os" % name))
        for owner in ("main", "other"):
            disk = os.path.join(self.workdir, "%s-first.img" % owner)
            spec = self.write("disk-%s.yaml" % owner, DISK.format(
                common=COMMON, main=groups["main"], other=groups["other"],
                owner=owner, disk=disk))
            self.build(spec, "disk-%s" % owner)
            self.boots(disk, "%s-os" % owner)
            os.remove(disk)
