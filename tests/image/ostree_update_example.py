#!/usr/bin/env python3
# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import avocado
import functools
import http.server
import os
import shutil
import subprocess
import sys
import threading

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)
sys.path.append(os.path.dirname(path_to_self))

import qemu_boot
import qemu_guest
from seine.build import BuildCmd
from seine.utils import HOST_ARCH
from tests.testutils import prune_on_pass

# Where the example serves its payload from (its update: url).
PORT = 8000

EXAMPLE = os.path.join(path_to_sources, "examples", "pc-ostree-update-image", "main.yaml")

PLAN = os.environ.get("SEINE_TEST_PLAN", "")


# What the example asks for, as a spec loads it.
class ExampleSpec(avocado.Test):
    def test_it_writes_a_signed_payload_for_a_versioned_image(self):
        build = BuildCmd()
        build.load_all([EXAMPLE])
        ostree = build.spec["image"]["ostree"]
        self.assertEqual(ostree["version"], "1")
        self.assertEqual(ostree["gpg-key"], "vault:ostree-commits")
        self.assertEqual(ostree["manifest-key"], "vault:update-manifest")
        self.assertEqual((ostree["stateroot"], ostree["ref"]), ("debian", "debian/amd64"))
        self.assertTrue(build.spec["update"]["url"].startswith("http://10.0.2.2:"))

    def test_it_keeps_the_esp_where_the_script_looks_for_it(self):
        build = BuildCmd()
        build.load_all([EXAMPLE])
        esp = next(p for p in build.spec["image"]["partitions"] if p["label"] == "esp")
        self.assertEqual(esp["where"], "/efi")


# The example as shipped, built with its payload served over HTTP, then
# booted: the agent runs once and finds nothing newer.
class ExampleUpdates(avocado.Test):
    """
    :avocado: tags=full,container
    """
    timeout = 3600

    def setUp(self):
        if PLAN != "full":
            self.cancel("SEINE_TEST_PLAN=full builds an image; this takes a while")
        if HOST_ARCH != "amd64":
            self.cancel("examples/pc-ostree-update-image is amd64-only")
        if shutil.which("podman") is None:
            self.cancel("podman is needed to build an image")
        reason = qemu_boot.cannot_boot()
        if reason:
            self.cancel(reason)

    def tearDown(self):
        prune_on_pass(self)

    def test_the_agent_runs_on_the_booted_image(self):
        disk = os.path.join(self.workdir, "pc-ostree-update.img")
        payload = os.path.join(self.workdir, "pc-ostree-update-payload")
        os.makedirs(payload)
        handler = functools.partial(
            http.server.SimpleHTTPRequestHandler, directory=payload)
        handler.log_message = lambda *args: None
        try:
            server = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), handler)
        except OSError:
            self.cancel(f"port {PORT}, where the example's payload is served, is in use")
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            self.build_and_boot(disk)
        finally:
            server.shutdown()
            server.server_close()

    def build_and_boot(self, disk):
        where = os.path.join(self.workdir, "where.yml")
        with open(where, "w") as f:
            f.write(f"image:\n    filename: {disk}\n")
        environment = dict(os.environ)
        environment["PATH"] = f"{os.path.dirname(sys.executable)}:{environment.get('PATH', '')}"
        log = os.path.join(self.outputdir, "build.log")
        with open(log, "w") as f:
            built = subprocess.run(
                [sys.executable, "-u", "./seine.py", "build", "-v", EXAMPLE, where],
                cwd=path_to_sources, env=environment, stdout=f,
                stderr=subprocess.STDOUT)
        self.assertEqual(built.returncode, 0, f"build failed, see {log}")
        self.assertTrue(os.path.isfile(os.path.join(
            self.workdir, "pc-ostree-update-payload", "uki", "debian-1.efi")))

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
            status, output = guest.run("grep IMAGE_VERSION /etc/os-release")
            self.assertEqual(output, "IMAGE_VERSION=1")
            status, output = guest.run(
                "(for i in $(seq 60); do ls /run/systemd/netif/leases/* && exit 0; sleep 1; done; exit 1)", 90)
            self.assertEqual(status, 0, "no DHCP lease")
            status, output = guest.run("grep -h '^url=' /etc/ostree/remotes.d/seine.conf")
            self.assertEqual(output, f"url=http://10.0.2.2:{PORT}/repo")
            status, output = guest.run("/usr/libexec/seine-update/seine-update", 180)
            self.assertEqual(status, 0, output)
            status, output = guest.run("cat /var/lib/seine-update/status")
            self.assertIn("result=nothing", output.splitlines())
            status, output = guest.run("/usr/libexec/seine-update/seine-update check")
            self.assertEqual(status, 0, output)
        finally:
            guest.close()
