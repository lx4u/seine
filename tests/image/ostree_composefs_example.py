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
import qemu_guest
from seine.utils import HOST_ARCH
from tests.testutils import prune_on_pass

EXAMPLE = os.path.join(path_to_sources, "examples", "pc-ostree-composefs-image")

PLAN = os.environ.get("SEINE_TEST_PLAN", "")

# Asks for verity alone: the conf says so and no commit signature is
# made, as the example's signed boot would otherwise ask for one.
VERITY = """\
image:
    ostree:
        composefs-key: ~
playbook:
    - name: ask for verity
      wave: verity
      after: [initramfs]
      before: [kernel]
      tasks:
          - name: enabled = verity
            lineinfile:
                path: /usr/lib/ostree/prepare-root.conf
                regexp: '^enabled ='
                line: enabled = verity
"""

# examples/pc-ostree-composefs-image/ built as shipped, only the disk's
# location changed, then booted under OVMF: / must be the composefs
# overlay, not the classic ostree bind that a missing conf falls back to.
class OstreeComposefsExampleBoots(avocado.Test):
    """
    :avocado: tags=full,container
    """
    timeout = 3600

    def setUp(self):
        if PLAN != "full":
            self.cancel("SEINE_TEST_PLAN=full builds an image; this takes a while")
        if HOST_ARCH != "amd64":
            self.cancel("stdlib:debian/amd64.yml is amd64-only")
        if shutil.which("podman") is None:
            self.cancel("podman is needed to build an image")
        reason = qemu_boot.cannot_boot()
        if reason:
            self.cancel(reason)

    def tearDown(self):
        prune_on_pass(self)

    # Builds the example with 'extra' (spec text) after it and returns a
    # disk that boots it, over a copy that the test may spoil.
    def build(self, extra=()):
        disk = os.path.join(self.workdir, "pc-ostree-composefs.img")
        specs = []
        for number, text in enumerate((f"image:\n    filename: {disk}\n",) + tuple(extra)):
            specs.append(os.path.join(self.workdir, f"extra{number}.yml"))
            with open(specs[-1], "w") as f:
                f.write(text)
        environment = dict(os.environ)
        environment["PATH"] = f"{os.path.dirname(sys.executable)}:{environment.get('PATH', '')}"
        log = os.path.join(self.outputdir, "build.log")
        with open(log, "w") as f:
            built = subprocess.run(
                [sys.executable, "-u", "./seine.py", "build", "-v",
                 os.path.join(EXAMPLE, "main.yaml"), *specs],
                cwd=path_to_sources, env=environment, stdout=f,
                stderr=subprocess.STDOUT)
        self.assertEqual(built.returncode, 0, f"build failed, see {log}")
        overlay = os.path.join(self.workdir, "disk.qcow2")
        qemu_guest.overlay(disk, overlay)
        variables = os.path.join(self.workdir, "OVMF_VARS.fd")
        shutil.copy(os.path.join(qemu_guest.OVMF, "OVMF_VARS_4M.fd"), variables)
        return overlay, variables

    def guest(self, overlay, variables, name):
        return qemu_guest.Guest(overlay, variables, os.path.join(self.outputdir, name))

    def log_in(self, guest):
        guest.expect("login:", 240)
        guest.send("root\n")
        guest.expect("Password:", 20)
        guest.send("welcome123\n")
        guest.login(60)

    def composefs_log(self, guest):
        status, output = guest.run("journalctl -b -t ostree-prepare-root --no-pager")
        return output

    def test_the_root_is_a_signed_read_only_composefs(self):
        overlay, variables = self.build()
        guest = self.guest(overlay, variables, "guest.log")
        try:
            self.log_in(guest)
            status, mount = guest.run("findmnt -no FSTYPE /")
            self.assertEqual(mount, "overlay")
            status, output = guest.run("touch /opt/x 2>&1")
            self.assertIn("Read-only file system", output)
            output = self.composefs_log(guest)
            self.assertIn("Validated commit signature using "
                          "'/etc/ostree/initramfs-root-binding.key'", output)
            self.assertIn("composefs: mounted successfully (verity=true)", output)
            # The signature is what the boot trusts: without it, no boot.
            guest.run("mount -o remount,rw /sysroot && "
                      "rm /sysroot/ostree/repo/objects/*/*.commitmeta && sync")
        finally:
            guest.close()
        guest = self.guest(overlay, variables, "tampered.log")
        try:
            guest.expect("No commitmeta for deploy", 240)
        finally:
            guest.close()

    def test_verity_alone_is_checked_without_a_signature(self):
        overlay, variables = self.build([VERITY])
        guest = self.guest(overlay, variables, "guest.log")
        try:
            self.log_in(guest)
            output = self.composefs_log(guest)
            self.assertIn("composefs: mounted successfully (verity=true)", output)
            self.assertNotIn("Validated commit signature", output)
            status, features = guest.run("dumpe2fs -h $(findmnt -no SOURCE /sysroot) 2>&1")
            self.assertRegex(features, r"Filesystem features:.* verity")
        finally:
            guest.close()
