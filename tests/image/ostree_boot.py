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

# A UKI of the rootfs' own kernel and initramfs, made at build time
# without the deployment's command line: the imager adds it.
UKI = """    - name: a UKI of the kernel
      priority: 900
      tasks:
          - name: install ukify
            apt:
                name: [systemd-ukify, systemd-boot-efi]
                state: present
          - name: build the UKI
            shell: |
                mkdir -p /boot/EFI/Linux
                ukify build --linux=$(ls /boot/vmlinuz-*) --initrd=$(ls /boot/initrd.img-*) \\
                    --cmdline=systemd.hostname=uki-host --output=/boot/EFI/Linux/os.efi
"""

# agetty prints the IMAGE_VERSION of the booted /etc/os-release.
VERSION_BANNER = """    - name: the version in the banner
      priority: 960
      tasks:
          - name: set what getty prints before the login prompt
            copy:
                content: "uki-os version \\\\S{{IMAGE_VERSION}}\\n"
                dest: /etc/issue
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

    # The ESP tells which boot loader was installed: a boot to the login
    # prompt works with either.
    def assert_loader(self, disk, loader):
        import guestfs
        g = guestfs.GuestFS(python_return_dict=True)
        g.add_drive_opts(disk, format="raw", readonly=True)
        g.launch()
        try:
            g.mount_ro("/dev/sda1", "/")
            self.assertEqual(g.is_file("/loader/loader.conf"), loader == "systemd-boot")
            self.assertEqual(g.is_file("/grub/grub.cfg"), loader == "grub")
        finally:
            g.close()

    def test_a_single_sysroot_boots(self):
        self.single_sysroot_boots(GROUP, "grub")

    def test_a_single_sysroot_boots_with_systemd_boot(self):
        self.single_sysroot_boots(GROUP_SYSTEMD_BOOT, "systemd-boot")

    def single_sysroot_boots(self, group, loader):
        disk = os.path.join(self.workdir, "single.img")
        spec = self.write("single.yaml", (group + SINGLE_DISK).format(
            common=COMMON, pc_image=os.path.join(EXAMPLES, "pc-image"),
            name="single-os", disk=disk))
        self.build(spec, "single")
        self.assert_loader(disk, loader)
        self.boots(disk, "single-os")

    # The boot owner is the first menu entry, so building with each in
    # turn boots each group's own sysroot.
    def test_each_group_of_a_two_sysroot_disk_boots(self):
        self.each_group_boots(GROUP, "grub")

    def test_each_group_of_a_two_sysroot_disk_boots_with_systemd_boot(self):
        self.each_group_boots(GROUP_SYSTEMD_BOOT, "systemd-boot")

    def each_group_boots(self, group, loader):
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
            self.assert_loader(disk, loader)
            self.boots(disk, "%s-os" % owner)
            os.remove(disk)

    # The UKI is on the ESP, the kernel files are not (systemd-boot) or
    # are left to the menu (grub chainloads), and the login prompt shows the
    # host name that only the UKI's command line sets.
    def test_a_single_sysroot_boots_from_its_uki_with_grub(self):
        self.uki_boots(GROUP, "grub")

    def test_a_single_sysroot_boots_from_its_uki_with_systemd_boot(self):
        self.uki_boots(GROUP_SYSTEMD_BOOT, "systemd-boot")

    # The version is stamped in the commit: the UKI is named after it and
    # the booted system reads it from its own os-release.
    def test_a_versioned_sysroot_boots_from_its_uki_with_systemd_boot(self):
        self.uki_boots(GROUP_SYSTEMD_BOOT, "systemd-boot", "23")

    def uki_boots(self, group, loader, version=None):
        disk = os.path.join(self.workdir, "uki.img")
        keys = "" if version is None else (
            '        version: "%s"\n        gpg-key: vault:ostree-commits\n'
            '        manifest-key: vault:update-manifest\n' % version)
        if version is not None:
            group = group.replace("requires:\n", "requires:\n"
                "    - {common}/dev-ostree-key\n"
                "    - {common}/dev-update-manifest-key\n", 1)
        banner = "" if version is None else VERSION_BANNER
        spec = self.write("uki.yaml", (group + UKI + banner + SINGLE_DISK.replace(
                "        mode: standard\n", "        mode: standard\n" + keys)).format(
            common=COMMON, pc_image=os.path.join(EXAMPLES, "pc-image"),
            name="uki-os", disk=disk))
        self.build(spec, "uki")
        efi_name = "debian-os.efi" if version is None else "debian-%s.efi" % version

        import guestfs
        g = guestfs.GuestFS(python_return_dict=True)
        g.add_drive_opts(disk, format="raw", readonly=True)
        g.launch()
        try:
            g.mount_ro("/dev/sda2", "/")
            commit = g.cat("/ostree/repo/refs/heads/debian/amd64").strip()
            target = g.readlink("/ostree/debian-%s" % commit)
            self.assertEqual(target, "deploy/debian/deploy/%s.0" % commit)
            self.assertTrue(g.is_dir("/ostree/%s" % target))
            g.umount("/")
            g.mount_ro("/dev/sda1", "/")
            self.assertEqual(g.ls("/EFI/Linux"), [efi_name])
            uki = g.read_file("/EFI/Linux/" + efi_name)
            self.assertIn(b"ostree=/ostree/debian-%s" % commit.encode(), uki)
            if version is not None:
                self.assertIn(b"IMAGE_VERSION=%s" % version.encode(), uki)
            if loader == "grub":
                self.assertIn("chainloader /EFI/Linux/%s" % efi_name,
                              g.cat("/grub/grub.cfg"))
            else:
                self.assertFalse(g.exists("/ostree"), "kernel files on the ESP")
                self.assertEqual(
                    g.ls("/loader/entries") if g.is_dir("/loader/entries") else [],
                    [], "entries on the ESP")
        finally:
            g.close()
        workdir = os.path.join(self.workdir, "boot-uki")
        os.makedirs(workdir)
        found, text = qemu_boot.boot_until(disk, workdir, "login:")
        with open(os.path.join(self.outputdir, "boot-uki.log"), "w") as f:
            f.write(text)
        self.assertTrue(found, "no 'login:' on the serial console")
        self.assertIn("uki-host login:", text, "the system did not boot from the UKI")
        self.assertIn("uki-os", text)
        if version is not None:
            self.assertIn("uki-os version %s" % version, text)
