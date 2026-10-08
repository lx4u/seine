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

EXAMPLE = os.path.join(path_to_sources, "examples", "pc-ostree-image")

PLAN = os.environ.get("SEINE_TEST_PLAN", "")

# examples/pc-ostree-image/ built as shipped, only the disk's location
# changed, then booted under OVMF to a login prompt.
class OstreeExampleBoots(avocado.Test):
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

    # The deployment's fstab mounts the /var/home partition, which is
    # an ext4 file system of its own.
    def assertHomeIsMounted(self, disk):
        import guestfs
        g = guestfs.GuestFS(python_return_dict=True)
        g.add_drive_opts(disk, format="raw", readonly=True)
        g.launch()
        try:
            g.mount_ro("/dev/sda2", "/")
            deployments = [d for d in g.ls("/ostree/deploy/debian/deploy")
                           if not d.endswith(".origin")]
            fstab = g.cat("/ostree/deploy/debian/deploy/%s/etc/fstab" % deployments[0])
            self.assertIn("/var/home", [l.split()[1].rstrip("/") for l in fstab.splitlines()])
            self.assertEqual(g.vfs_type("/dev/sda4"), "ext4")
        finally:
            g.close()

    def test_the_example_builds_and_boots(self):
        disk = os.path.join(self.workdir, "pc-ostree.img")
        where = os.path.join(self.workdir, "filename.yml")
        with open(where, "w") as f:
            f.write("image:\n    filename: %s\n" % disk)

        environment = dict(os.environ)
        environment["PATH"] = "%s:%s" % (os.path.dirname(sys.executable),
                                         environment.get("PATH", ""))
        log = os.path.join(self.outputdir, "build.log")
        with open(log, "w") as f:
            built = subprocess.run(
                [sys.executable, "-u", "./seine.py", "build", "-v",
                 os.path.join(EXAMPLE, "main.yaml"), where],
                cwd=path_to_sources, env=environment, stdout=f,
                stderr=subprocess.STDOUT)
        self.assertEqual(built.returncode, 0, "build failed, see %s" % log)

        self.assertHomeIsMounted(disk)

        workdir = os.path.join(self.workdir, "boot")
        os.makedirs(workdir)
        found, text = qemu_boot.boot_until(disk, workdir, "login:")
        with open(os.path.join(self.outputdir, "boot.log"), "w") as f:
            f.write(text)
        self.assertTrue(found, "no 'login:' on the serial console, see boot.log")
        self.assertNotIn("emergency mode", text)
