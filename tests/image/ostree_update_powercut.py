#!/usr/bin/env python3
# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import os
import random
import sys
import time

import avocado

path_to_self = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)
sys.path.append(os.path.dirname(path_to_self))

from update_guest import AGENT, ESP, UpdateGuest

# The failpoints of seine-update, the type of step, the version expected to
# boot after the cut, and the expected update outcome after reconcile.
FAILPOINTS = (
    ("after-elect", "update", "1", "ok"),
    ("after-pull", "update", "1", "ok"),
    ("after-prune-ukis", "update", "1", "ok"),
    ("after-deploy", "update", "1", "ok"),
    ("after-link", "update", "1", "ok"),
    ("after-prune", "update", "1", "ok"),
    ("after-uki", "update", "2", "nothing"),
    ("after-bind", "update", "2", "nothing"),
    ("after-denylist", "cleanup", "1", "denied"),
    ("after-undeploy", "cleanup", "1", "denied"),
)


# Cutting power during an update must leave the system bootable and sane:
# reconcile repairs any partial state, invariants hold, and the next
# update succeeds.
class UpdatePowerCut(UpdateGuest, avocado.Test):
    """
    :avocado: tags=full,container
    """
    timeout = 7200

    def setUp(self):
        super().setUp()
        self.disk_1 = self.build("1")
        os.remove(self.build("2"))

    def run_failpoint(self, point, kind, booted_version, expected_result):
        self.start(self.disk_1)
        self.wait_for_network()

        if kind == "cleanup":
            # Stage a failed update for version 2 to trigger cleanup.
            self.sh("/usr/lib/systemd/systemd-sysupdate update 2")
            self.sh(f"mv {ESP}/debian-2+3-0.efi {ESP}/debian-2+0-3.efi")
            self.sh("ostree pull seine debian/amd64")
            c2 = self.sh("ostree rev-parse seine:debian/amd64").strip()
            self.sh(f"ostree admin deploy {c2}")
            d = self.sh(
                f"ls /sysroot/ostree/deploy/debian/deploy/ | grep -E '^{c2}\\.[0-9]+$'").strip()
            self.sh(
                f"ln -sfn deploy/debian/deploy/{d} /sysroot/ostree/debian-{c2}")
            self.sh("sync")
            cmd = (f"SEINE_UPDATE_FAILPOINT={point} "
                   f"SEINE_UPDATE_FAILPOINT_ACTION=hold {AGENT} reconcile\n")
        else:
            cmd = (f"SEINE_UPDATE_FAILPOINT={point} "
                   f"SEINE_UPDATE_FAILPOINT_ACTION=hold {AGENT}\n")

        self.guest.send(cmd)
        self.guest.expect(f"seine-update: FAILPOINT {point}", timeout=120)

        # Cut power by terminating QEMU immediately.
        self.guest.close()
        self.guest = None

        # Boot up after the power cut on the same disk.
        self.start()
        booted = self.booted_version()
        self.assertIn(
            booted, (booted_version,) if booted_version == "1" else ("1", "2"),
            f"{point}: unexpected boot of version {booted}")

        self.sh(f"{AGENT} reconcile")
        self.assert_invariants()

        if expected_result == "ok" or (expected_result == "nothing" and booted == "1"):
            res = self.update()
            if res.get("result") == "waiting":
                self.reboot()
            else:
                self.assertEqual(
                    (res.get("result"), res.get("version")), ("ok", "2"),
                    f"{point}: update failed after cut")
                self.reboot()
            self.assertEqual(self.booted_version(), "2")
            self.wait_until(f"test -e {ESP}/debian-2.efi")
            self.sh(f"{AGENT} reconcile")
            self.assert_invariants()
        elif expected_result == "nothing":
            self.wait_until(f"test -e {ESP}/debian-2.efi")
            self.sh(f"{AGENT} reconcile")
            self.assert_invariants()
            res = self.update()
            self.assertEqual(res.get("result"), "nothing")
        elif expected_result == "denied":
            self.assertEqual(self.esp(), ["debian-1.efi"])
            self.assertEqual(len(self.deployments()), 1)
            self.sh("awk '$1 == 2 { found = 1 } END { exit !found }' "
                    "/var/lib/seine-update/failed")
            res = self.update()
            self.assertEqual(res.get("result"), "nothing")
            self.assertIn("denied", res.get("message", ""))
            self.assert_invariants()

        self.guest.close()
        self.guest = None
        if os.path.exists(self.disk):
            os.remove(self.disk)

    def test_power_cut_at_failpoints(self):
        for point, kind, booted_version, expected_result in FAILPOINTS:
            self.run_failpoint(point, kind, booted_version, expected_result)

    def test_random_power_cuts(self):
        """
        :avocado: tags=long
        """
        rnd = random.Random(42)
        for _ in range(3):
            self.start(self.disk_1)
            self.wait_for_network()

            # Start update in the background and cut power at a random delay.
            self.guest.send(f"{AGENT} >/tmp/update.log 2>&1 &\n")
            time.sleep(rnd.uniform(0.3, 2.5))
            self.guest.close()
            self.guest = None

            # Boot up after the power cut.
            self.start()
            self.sh(f"{AGENT} reconcile")
            self.assert_invariants()

            booted = self.booted_version()
            if booted == "2":
                self.wait_until(f"test -e {ESP}/debian-2.efi")
                self.sh(f"{AGENT} reconcile")
                self.assert_invariants()
                res = self.update()
                self.assertEqual(res.get("result"), "nothing")
            else:
                self.assertEqual(booted, "1")
                res = self.update()
                if res.get("result") == "waiting":
                    self.reboot()
                else:
                    self.assertEqual(
                        (res.get("result"), res.get("version")), ("ok", "2"))
                    self.reboot()
                self.assertEqual(self.booted_version(), "2")
                self.wait_until(f"test -e {ESP}/debian-2.efi")
                self.sh(f"{AGENT} reconcile")
                self.assert_invariants()

            self.guest.close()
            self.guest = None
            if os.path.exists(self.disk):
                os.remove(self.disk)
