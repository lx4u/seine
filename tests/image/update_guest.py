# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import functools
import http.server
import os
import shutil
import subprocess
import sys
import threading
import time

import yaml

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)
sys.path.append(os.path.dirname(path_to_self))

import qemu_boot
import qemu_guest
import secure_boot
from seine.utils import HOST_ARCH
from tests.testutils import prune_on_pass

PLAN = os.environ.get("SEINE_TEST_PLAN", "")

EXAMPLE = os.path.join(path_to_sources, "examples", "pc-ostree-update-image", "main.yaml")

# The key that signs the example's boot files: its certificate is the db entry.
DEV_KEY = os.path.join(path_to_sources, "examples", "common", "dev-secureboot-key.yaml")

# Where the example serves its payload from (its update: url).
PORT = 8000

ESP = "/efi/EFI/Linux"
AGENT = "/usr/libexec/seine-update/seine-update"

# A unit that fails on every boot: the UKI boots, but is never blessed.
UNHEALTHY = """
playbook:
    - name: an unhealthy system
      priority: 950
      tasks:
          - name: add a unit that fails
            copy:
                dest: /etc/systemd/system/seine-test-fail.service
                content: "[Service]\\nExecStart=/bin/false\\n[Install]\\nWantedBy=multi-user.target\\n"
          - name: start it at boot
            systemd:
                name: seine-test-fail.service
                enabled: true
"""

# efi-updatevar enrols the keys from the guest, which is in Setup Mode.
EFITOOLS = """
playbook:
    - name: tools to enrol the Secure Boot keys
      priority: 850
      tasks:
          - name: install efitools
            apt:
                name: [efitools]
                state: present
"""

# Shared scaffolding for the tests that update the example image with its
# own agent. Not an avocado.Test itself, so avocado does not run it alone.
class UpdateGuest:
    # True: the guest runs with Secure Boot on, the dev key enrolled as db.
    secure_boot = False

    def setUp(self):
        self.guest = None
        self.server = None
        if PLAN != "full":
            self.cancel("SEINE_TEST_PLAN=full builds an image; this takes a while")
        if HOST_ARCH != "amd64":
            self.cancel("examples/pc-ostree-update-image is amd64-only")
        if shutil.which("podman") is None:
            self.cancel("podman is needed to build an image")
        reason = qemu_boot.cannot_boot()
        if reason:
            self.cancel(reason)
        self.share = None
        if self.secure_boot:
            reason = secure_boot.missing_tool()
            if reason:
                self.cancel(reason)
            self.make_keys()
        self.payload = os.path.join(self.workdir, "payload")
        os.makedirs(self.payload)
        self.variables = os.path.join(self.workdir, "OVMF_VARS.fd")
        shutil.copy(os.path.join(qemu_guest.OVMF, "OVMF_VARS_4M.fd"), self.variables)
        self.serve()

    def tearDown(self):
        if self.guest:
            self.guest.close()
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        prune_on_pass(self)

    # Throw-away PK and KEK, and the dev certificate as db. The guest sees the
    # three .auth files as a 9p share.
    def make_keys(self):
        cert = os.path.join(self.workdir, "dev-db.crt")
        with open(DEV_KEY) as f:
            pem = yaml.safe_load(f)["defaults"]["vault"]["uefi-secureboot"]["cert_pem"]
        with open(cert, "w") as f:
            f.write(pem)
        self.keys = secure_boot.Keys(os.path.join(self.workdir, "keys"), db_cert=cert)
        self.share = os.path.join(self.workdir, "share")
        os.makedirs(self.share)
        for name in ("db", "KEK", "PK"):
            shutil.copy(self.keys.path(name, "auth"), self.share)

    def serve(self):
        handler = functools.partial(
            http.server.SimpleHTTPRequestHandler, directory=self.payload)
        handler.log_message = lambda *args: None
        try:
            self.server = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), handler)
        except OSError:
            self.cancel(f"port {PORT}, where the example's payload is served, is in use")
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    # Builds the example as 'version' into the payload. 'extra' is a spec
    # that is loaded after the example. Returns the disk image.
    def build(self, version, extra=""):
        disk = os.path.join(self.workdir, f"disk-{version}.img")
        peer = os.path.join(self.workdir, f"version-{version}.yml")
        with open(peer, "w") as f:
            f.write(f"image:\n    filename: {disk}\n    ostree:\n"
                    f'        version: "{version}"\n'
                    f"        payload:\n            path: {self.payload}\n")
        specs = [peer]
        # The same tools in every version keep the rootfs the same.
        extras = ([EFITOOLS] if self.secure_boot else []) + ([extra] if extra else [])
        for number, text in enumerate(extras):
            specs.append(os.path.join(self.workdir, f"extra-{version}-{number}.yml"))
            with open(specs[-1], "w") as f:
                f.write(text)
        environment = dict(os.environ)
        environment["PATH"] = f"{os.path.dirname(sys.executable)}:{environment.get('PATH', '')}"
        log = os.path.join(self.outputdir, f"build-{version}.log")
        with open(log, "w") as f:
            built = subprocess.run(
                [sys.executable, "-u", "./seine.py", "build", "-v", EXAMPLE] + specs,
                cwd=path_to_sources, env=environment, stdout=f,
                stderr=subprocess.STDOUT)
        self.assertEqual(built.returncode, 0, f"build of {version} failed, see {log}")
        self.assertTrue(os.path.isfile(os.path.join(self.payload, "uki", f"debian-{version}.efi")))
        return disk

    # Starts the guest on a copy-on-write overlay of 'disk' and logs in. With
    # Secure Boot, the first start enrols the keys.
    def start(self, disk=None):
        if disk:
            self.disk = os.path.join(self.workdir, "disk.qcow2")
            qemu_guest.overlay(disk, self.disk)
        self.starts = getattr(self, "starts", 0) + 1
        log = os.path.join(self.outputdir, f"serial-{self.starts}.log")
        self.guest = qemu_guest.Guest(self.disk, self.variables, log,
                                      secure_boot=self.secure_boot, share=self.share)
        self.login()
        if disk and self.secure_boot:
            self.enrol()

    # The firmware is in Setup Mode and boots the signed factory UKI anyway.
    def enrol(self):
        self.assertIn("Secure Boot: disabled (setup)", self.sh("bootctl status"))
        for name in ("db", "KEK", "PK"):
            self.sh(f"efi-updatevar -f {qemu_guest.SHARE}/{name}.auth {name}")
        self.reboot()
        self.assertIn("Secure Boot: enabled (user)", self.sh("bootctl status"))

    # The image has a root login with a password, not an autologin.
    def login(self, timeout=240):
        self.guest.expect("login:", timeout)
        self.guest.send("root\n")
        self.guest.expect("Password:", 20)
        self.guest.send("welcome123\n")
        self.guest.login(60)

    def reboot(self, timeout=300):
        self.guest.send("systemctl reboot\n")
        self.guest.expect(r"reboot: Restarting system", 150)
        self.login(timeout)

    def sh(self, command, timeout=60):
        status, output = self.guest.run(command, timeout)
        self.assertEqual(status, 0, f"'{command}' failed: {output}")
        return output

    def wait_until(self, command, timeout=120):
        deadline = time.time() + timeout
        while self.guest.run(command)[0] != 0:
            if time.time() > deadline:
                self.fail(f"'{command}' still fails after {timeout} s: "
                          f"{self.guest.run(f'ls -l {ESP}')[1]}")
            time.sleep(2)

    # The agent needs the network: the DHCP lease comes after the login.
    def wait_for_network(self):
        self.wait_until("ls /run/systemd/netif/leases/*", 90)

    # Runs the agent once. Returns its status file as a dict.
    def update(self):
        self.wait_for_network()
        self.sh(AGENT, 300)
        lines = self.sh("cat /var/lib/seine-update/status").splitlines()
        return dict(line.split("=", 1) for line in lines)

    # The deployments of ostree admin status: (name, pinned).
    def deployments(self):
        rows = []
        for line in self.sh("ostree admin status").splitlines():
            if line.lstrip("* ").startswith("debian "):
                rows.append([line.lstrip("* ").split()[1], False])
            elif "Pinned: yes" in line and rows:
                rows[-1][1] = True
        return rows

    def esp(self):
        return self.sh(f"ls {ESP}").split()

    def booted_version(self):
        return self.sh("sed -n 's/^IMAGE_VERSION=//p' /etc/os-release")

    # Run after every step: what the agent promises always holds.
    def assert_invariants(self):
        self.sh(f"{AGENT} check")
        status = self.sh("ls /sysroot/ostree | grep '^debian-' || true")
        for link in status.split():
            self.sh(f"test -e /sysroot/ostree/{link}")
        self.assertEqual(self.sh(f"ls {ESP}/.#sysupdate* {ESP}/*.tmp 2>/dev/null || true"), "")
        pinned = [name for name, pin in self.deployments() if pin]
        self.assertEqual(len(pinned), 1, f"expected one pinned deployment, got {pinned}")
        self.assertLessEqual(len(self.deployments()), 3)
