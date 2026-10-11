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

# Rewrites the conf the example ships, before the kernel builds the initramfs.
VERITY = """\
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

    # Builds the example with 'extra' (spec files) after it, boots it and
    # returns what the composefs mount logged, and the sysroot's features.
    def boot(self, extra=()):
        disk = os.path.join(self.workdir, "pc-ostree-composefs.img")
        where = os.path.join(self.workdir, "filename.yml")
        with open(where, "w") as f:
            f.write(f"image:\n    filename: {disk}\n")
        environment = dict(os.environ)
        environment["PATH"] = f"{os.path.dirname(sys.executable)}:{environment.get('PATH', '')}"
        log = os.path.join(self.outputdir, "build.log")
        with open(log, "w") as f:
            built = subprocess.run(
                [sys.executable, "-u", "./seine.py", "build", "-v",
                 os.path.join(EXAMPLE, "main.yaml"), *extra, where],
                cwd=path_to_sources, env=environment, stdout=f,
                stderr=subprocess.STDOUT)
        self.assertEqual(built.returncode, 0, f"build failed, see {log}")

        overlay = os.path.join(self.workdir, "disk.qcow2")
        variables = os.path.join(self.workdir, "OVMF_VARS.fd")
        qemu_guest.overlay(disk, overlay)
        shutil.copy(os.path.join(qemu_guest.OVMF, "OVMF_VARS_4M.fd"), variables)
        guest = qemu_guest.Guest(overlay, variables, os.path.join(self.outputdir, "guest.log"))
        try:
            guest.expect("login:", 240)
            guest.send("root\n")
            guest.expect("Password:", 20)
            guest.send("welcome123\n")
            guest.login(60)
            status, mount = guest.run("findmnt -no FSTYPE /")
            self.assertEqual(mount, "overlay")
            status, output = guest.run("touch /opt/x 2>&1")
            self.assertIn("Read-only file system", output)
            status, output = guest.run("journalctl -b -t ostree-prepare-root --no-pager")
            status, features = guest.run("dumpe2fs -h $(findmnt -no SOURCE /sysroot) 2>&1")
            return output, features
        finally:
            guest.close()

    def test_the_root_is_a_read_only_composefs(self):
        output, features = self.boot()
        self.assertIn("composefs: mounted successfully (verity=false)", output)

    def test_verity_is_checked_when_asked_for(self):
        where = os.path.join(self.workdir, "verity.yml")
        with open(where, "w") as f:
            f.write(VERITY)
        output, features = self.boot([where])
        self.assertIn("composefs: mounted successfully (verity=true)", output)
        self.assertRegex(features, r"Filesystem features:.* verity")
