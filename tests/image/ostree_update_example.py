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
from tests.testutils import RELEASE, prune_on_pass

# Where the example serves its payload from (its update: url).
PORT = 8000

EXAMPLE_DIR = os.path.join(path_to_sources, "examples", "pc-ostree-update-image")
EXAMPLE = os.path.join(EXAMPLE_DIR, "main.yaml")
EXAMPLE_V2 = os.path.join(EXAMPLE_DIR, "v2.yaml")
EXAMPLE_V3 = os.path.join(EXAMPLE_DIR, "v3.yaml")
EXAMPLE_V4 = os.path.join(EXAMPLE_DIR, "v4.yaml")
EXAMPLE_V5 = os.path.join(EXAMPLE_DIR, "v5.yaml")
EXAMPLE_V5_1 = os.path.join(EXAMPLE_DIR, "v5.1.yaml")
EXAMPLE_V9 = os.path.join(EXAMPLE_DIR, "v9.yaml")

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

    def test_v1_writes_issue_version(self):
        build = BuildCmd()
        build.load_all([EXAMPLE])
        tasks = [t for p in build.spec["playbook"] if p["name"] == "system identity" for t in p["tasks"]]
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["copy"]["dest"], "/etc/issue")
        self.assertIn("v1", tasks[0]["copy"]["content"])

    def test_v2_overlay_sets_version_and_deltas(self):
        build = BuildCmd()
        build.load_all([EXAMPLE, EXAMPLE_V2])
        ostree = build.spec["image"]["ostree"]
        self.assertEqual(ostree["version"], "2")
        self.assertEqual(ostree["payload"]["deltas-from"], ["1"])
        tasks = [t for p in build.spec["playbook"] if p["name"] == "v2 packages" for t in p["tasks"]]
        self.assertEqual(len(tasks), 2)
        self.assertEqual(tasks[0]["apt"]["name"], ["htop", "strace"])
        self.assertEqual(tasks[1]["copy"]["dest"], "/etc/issue")
        self.assertIn("v2", tasks[1]["copy"]["content"])

    def test_v3_overlay_sets_version_and_deltas(self):
        build = BuildCmd()
        build.load_all([EXAMPLE, EXAMPLE_V3])
        ostree = build.spec["image"]["ostree"]
        self.assertEqual(ostree["version"], "3")
        self.assertEqual(ostree["payload"]["deltas-from"], ["1", "2"])
        tasks = [t for p in build.spec["playbook"] if p["name"] == "v3 packages" for t in p["tasks"]]
        self.assertEqual(len(tasks), 2)
        self.assertEqual(tasks[0]["apt"]["name"], ["chrony"])
        self.assertEqual(tasks[1]["copy"]["dest"], "/etc/issue")
        self.assertIn("v3", tasks[1]["copy"]["content"])

    def test_v4_overlay_sets_version_and_deltas(self):
        build = BuildCmd()
        build.load_all([EXAMPLE, EXAMPLE_V4])
        ostree = build.spec["image"]["ostree"]
        self.assertEqual(ostree["version"], "4")
        self.assertEqual(ostree["payload"]["deltas-from"], ["3"])
        tasks = [t for p in build.spec["playbook"] if p["name"] == "v4 daemon" for t in p["tasks"]]
        self.assertEqual(len(tasks), 2)
        self.assertEqual(tasks[0]["apt"]["name"], ["htop", "strace", "chrony"])
        self.assertEqual(tasks[1]["copy"]["dest"], "/etc/issue")
        self.assertIn("v4", tasks[1]["copy"]["content"])

    def test_v5_overlay_sets_version_and_failing_unit(self):
        build = BuildCmd()
        build.load_all([EXAMPLE, EXAMPLE_V5])
        ostree = build.spec["image"]["ostree"]
        self.assertEqual(ostree["version"], "5")
        self.assertEqual(ostree["payload"]["deltas-from"], ["4"])
        pkg_tasks = [t for p in build.spec["playbook"] if p["name"] == "v5 packages" for t in p["tasks"]]
        self.assertEqual(len(pkg_tasks), 2)
        self.assertEqual(pkg_tasks[0]["apt"]["name"], ["htop", "strace", "chrony"])
        self.assertEqual(pkg_tasks[1]["copy"]["dest"], "/etc/issue")
        self.assertIn("v5", pkg_tasks[1]["copy"]["content"])
        fail_tasks = [t for p in build.spec["playbook"] if p["name"] == "an unhealthy system" for t in p["tasks"]]
        self.assertEqual(len(fail_tasks), 2)
        self.assertEqual(fail_tasks[0]["copy"]["dest"], "/etc/systemd/system/seine-test-fail.service")

    def test_v5_1_overlay_sets_version_and_prunes_packages(self):
        build = BuildCmd()
        build.load_all([EXAMPLE, EXAMPLE_V5_1])
        ostree = build.spec["image"]["ostree"]
        self.assertEqual(ostree["version"], "5.1")
        self.assertEqual(ostree["payload"]["deltas-from"], ["4"])
        tasks = [t for p in build.spec["playbook"] if p["name"] == "v5.1 hotfix packages" for t in p["tasks"]]
        self.assertEqual(len(tasks), 2)
        self.assertEqual(tasks[0]["apt"]["name"], ["strace", "chrony"])
        self.assertEqual(tasks[1]["copy"]["dest"], "/etc/issue")
        self.assertIn("v5.1", tasks[1]["copy"]["content"])

    def test_v9_overlay_sets_version_deltas_and_custom_app(self):
        build = BuildCmd()
        build.load_all([EXAMPLE, EXAMPLE_V9])
        ostree = build.spec["image"]["ostree"]
        self.assertEqual(ostree["version"], "9")
        self.assertEqual(ostree["payload"]["deltas-from"], ["1", "5.1"])
        tasks = [t for p in build.spec["playbook"] if p["name"] == "v9 app and iperf3" for t in p["tasks"]]
        self.assertEqual(len(tasks), 5)
        self.assertEqual(tasks[0]["apt"]["name"], ["strace", "python3-minimal", "iperf3"])
        self.assertEqual(tasks[1]["copy"]["dest"], "/etc/issue")
        self.assertIn("v9", tasks[1]["copy"]["content"])
        self.assertEqual(tasks[4]["copy"]["dest"], "/usr/bin/myapp")


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
        reason = qemu_boot.cannot_boot(kvm_required=True)
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
            f.write(f"distribution:\n    release: {RELEASE}\n"
                    f"image:\n    filename: {disk}\n")
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
                "/usr/lib/systemd/systemd-networkd-wait-online --timeout=60 -q", 90)
            self.assertEqual(status, 0, "the network did not come up")
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
