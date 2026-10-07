#!/usr/bin/env python3
# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import os
import sys

import avocado

path_to_self = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)
sys.path.append(os.path.dirname(path_to_self))

from seine.build import BuildCmd
from update_guest import AGENT, ESP, EXAMPLE, UpdateGuest

HANG_EXTRA = """
playbook:
    - name: hang before boot completion
      priority: 950
      tasks:
          - name: create boot-complete drop-in directory
            file:
                path: /etc/systemd/system/boot-complete.target.d
                state: directory
          - name: override boot-complete job timeout
            copy:
                dest: /etc/systemd/system/boot-complete.target.d/99-timeout.conf
                content: |
                    [Unit]
                    JobTimeoutSec=20s
                    JobTimeoutAction=reboot-force
          - name: add a unit that hangs before boot completion
            copy:
                dest: /etc/systemd/system/seine-test-hang.service
                content: |
                    [Unit]
                    Description=hang before boot completion
                    Before=boot-complete.target

                    [Service]
                    Type=oneshot
                    StandardOutput=journal+console
                    ExecStart=/bin/sh -c 'echo "seine-test: HANGING BEFORE BOOT COMPLETE" > /dev/console 2>&1; while true; do sleep 1 2>/dev/null || true; done'
                    RemainAfterExit=yes

                    [Install]
                    RequiredBy=boot-complete.target
          - name: enable the hanging service
            systemd:
                name: seine-test-hang.service
                enabled: true
"""


# Checks that loading the pc-ostree-update example with a hanging unit
# parses and loads the playbook tasks.
class HangUpdateSpec(avocado.Test):
    def test_spec_with_hang_task_parses(self):
        build = BuildCmd()
        peer = os.path.join(self.workdir, "hang.yml")
        with open(peer, "w") as f:
            f.write(HANG_EXTRA)
        build.load_all([EXAMPLE, peer])
        names = [p.get("name") for p in build.spec.get("playbook", [])]
        self.assertIn("hang before boot completion", names)


# Verifies that when a service ordered before boot-complete.target never
# completes, the job timeout on boot-complete.target resets the machine,
# tries are consumed, and systemd-boot falls back to the last-known-good UKI.
class UpdateBootHang(UpdateGuest, avocado.Test):
    """
    :avocado: tags=full,container
    """
    timeout = 7200

    def setUp(self):
        super().setUp()
        self.disk_1 = self.build("1")
        os.remove(self.build("2", extra=HANG_EXTRA))

    def settle(self):
        self.sh(f"{AGENT} reconcile")
        self.assert_invariants()

    def test_boot_hang_times_out_and_falls_back(self):
        self.start(self.disk_1)
        self.assertEqual(self.booted_version(), "1")
        self.wait_for_network()

        # Update to version 2
        res = self.update()
        self.assertEqual((res.get("result"), res.get("version")), ("ok", "2"))
        self.assertEqual(self.update().get("result"), "waiting")

        # Set 1 remaining try so a single timeout reset triggers fallback
        self.sh(f"mv {ESP}/debian-2+3-0.efi {ESP}/debian-2+1-0.efi && sync")

        # Reboot into the trial UKI
        self.guest.send("systemctl reboot\n")
        self.guest.expect(r"reboot: Restarting system", 150)

        # Version 2 hangs before boot completion and times out
        self.guest.expect("seine-test: HANGING BEFORE BOOT COMPLETE", timeout=120)
        self.guest.expect(r"reboot: Restarting system", timeout=60)

        # Job timeout forces a reboot; systemd-boot falls back to version 1
        self.login(timeout=180)
        self.assertEqual(self.booted_version(), "1")

        # Reconcile cleans up the failed update
        self.wait_until(f"test ! -e {ESP}/debian-2+0-1.efi")
        self.settle()
        self.assertEqual(self.esp(), ["debian-1.efi"])
        self.sh("awk '$1 == 2 { found = 1 } END { exit !found }' /var/lib/seine-update/failed")
        status = self.sh("cat /var/lib/seine-update/status")
        self.assertIn("result=failed-update", status)
        self.assertIn("version=2", status)

        # Further updates to version 2 are denied
        res = self.update()
        self.assertEqual(res.get("result"), "nothing")
        self.assertIn("denied", res.get("message", ""))

    def test_boot_hang_exhausts_all_tries_and_falls_back(self):
        """
        :avocado: tags=long
        """
        self.start(self.disk_1)
        self.assertEqual(self.booted_version(), "1")
        self.wait_for_network()

        # Update to version 2 (with default 3 tries)
        res = self.update()
        self.assertEqual((res.get("result"), res.get("version")), ("ok", "2"))
        self.assertEqual(self.update().get("result"), "waiting")

        # Reboot into the trial UKI
        self.guest.send("systemctl reboot\n")
        self.guest.expect(r"reboot: Restarting system", 150)

        # Each try hangs and triggers a forced reboot upon job timeout
        for _ in range(3):
            self.guest.expect("seine-test: HANGING BEFORE BOOT COMPLETE", timeout=120)
            self.guest.expect(r"reboot: Restarting system", timeout=60)

        # After tries are exhausted, systemd-boot falls back to version 1
        self.login(timeout=180)
        self.assertEqual(self.booted_version(), "1")

        # Reconcile cleans up the failed update
        self.wait_until(f"test ! -e {ESP}/debian-2+0-3.efi")
        self.settle()
        self.assertEqual(self.esp(), ["debian-1.efi"])
        self.sh("awk '$1 == 2 { found = 1 } END { exit !found }' /var/lib/seine-update/failed")
        status = self.sh("cat /var/lib/seine-update/status")
        self.assertIn("result=failed-update", status)
        self.assertIn("version=2", status)

        # Further updates to version 2 are denied
        res = self.update()
        self.assertEqual(res.get("result"), "nothing")
        self.assertIn("denied", res.get("message", ""))
