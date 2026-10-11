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

PC_IMAGE = os.path.join(path_to_sources, "examples", "pc-image")

# A real build and a boot, so it does not run unless asked for.
PLAN = os.environ.get("SEINE_TEST_PLAN", "")

# Builds the shipped pc-image example for one release and boots it under
# OVMF to a login prompt on the serial console. The image has a separate
# /boot and LVM volumes: the initrd must find and mount them.
class PcImageBoots(avocado.Test):
    """
    :avocado: tags=full,container
    """
    timeout = 3600
    release = "forky"

    def setUp(self):
        if PLAN != "full":
            self.cancel("SEINE_TEST_PLAN=full builds an image; this takes a while")
        if HOST_ARCH != "amd64":
            self.cancel("pc-image is an amd64 example")
        if shutil.which("podman") is None:
            self.cancel("podman is needed to build an image")
        reason = qemu_boot.cannot_boot()
        if reason:
            self.cancel(reason)

    def tearDown(self):
        prune_on_pass(self)

    def test_the_image_boots_to_a_login_prompt(self):
        # Peer files amend the example: a serial console, the release
        # and the disk's name.
        disk = os.path.join(self.workdir, "pc-image.img")
        peer = os.path.join(self.workdir, "peer.yaml")
        with open(peer, "w") as f:
            f.write(f"distribution:\n    release: {self.release}\n"
                    f"image:\n    filename: {disk}\n")

        environment = dict(os.environ)
        environment["PATH"] = f"{os.path.dirname(sys.executable)}:{environment.get('PATH', '')}"
        log = os.path.join(self.outputdir, "build.log")
        with open(log, "w") as f:
            built = subprocess.run(
                [sys.executable, "-u", "./seine.py", "build", "-v",
                 os.path.join(PC_IMAGE, "main.yaml"),
                 os.path.join(PC_IMAGE, "grub-serial-console.yaml"), peer],
                cwd=path_to_sources, env=environment, stdout=f,
                stderr=subprocess.STDOUT)
        self.assertEqual(built.returncode, 0, f"build failed, see {log}")

        workdir = os.path.join(self.workdir, "boot")
        os.makedirs(workdir)
        found, text = qemu_boot.boot_until(disk, workdir, "login:")
        with open(os.path.join(self.outputdir, "boot.log"), "w") as f:
            f.write(text)
        self.assertTrue(found, "no 'login:' on the serial console, see boot.log")
        self.assertNotIn("emergency mode", text)
        self.assertIn(self.release, text, "the booted system does not name its release")
