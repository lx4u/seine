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

# The imager takes the EFI Boot Guard binary from the root file-system.
EFIBOOTGUARD = """    - name: EFI Boot Guard
      wave: kernel
      tasks:
          - name: install EFI Boot Guard
            apt:
                name: efibootguard
                state: present
"""

WATCHDOG_SPEC = """
image:
    watchdog: 30s
playbook:
""" + EFIBOOTGUARD

# The hook is a dracut module that runs before udev loads the watchdog
# driver, as the kernel would feed the timer after that. It is added before
# the kernel is installed, so the initramfs and the UKI include it.
FREEZE_EXTRA = """
image:
    watchdog: 30s
playbook:
""" + EFIBOOTGUARD + """    - name: freeze early boot
      wave: initramfs
      tasks:
          - name: make the directory of the dracut module
            file:
                path: /usr/lib/dracut/modules.d/99seine-freeze
                state: directory
          - name: add a dracut module that freezes the boot
            copy:
                dest: "{{ item.dest }}"
                mode: "{{ item.mode }}"
                content: "{{ item.content }}"
            loop:
                - dest: /usr/lib/dracut/modules.d/99seine-freeze/module-setup.sh
                  mode: "0755"
                  content: |
                      #!/bin/bash
                      check() { return 0; }
                      depends() { return 0; }
                      install() { inst_hook pre-udev 99 "$moddir/freeze.sh"; }
                - dest: /usr/lib/dracut/modules.d/99seine-freeze/freeze.sh
                  mode: "0755"
                  content: |
                      #!/bin/sh
                      echo "seine-test: FREEZING EARLY BOOT FOR WATCHDOG" > /dev/console
                      while true; do sleep 1 2>/dev/null || true; done
                - dest: /etc/dracut.conf.d/90-seine-freeze.conf
                  mode: "0644"
                  content: |
                      add_dracutmodules+=" seine-freeze "
"""


# Checks that loading the pc-ostree-update example with image: watchdog
# configures the watchdog timeout on the partition handler.
class WatchdogUpdateSpec(avocado.Test):
    def test_spec_with_watchdog_parses_timeout(self):
        build = BuildCmd()
        peer = os.path.join(self.workdir, "watchdog.yml")
        with open(peer, "w") as f:
            f.write(WATCHDOG_SPEC)
        build.load_all([EXAMPLE, peer])
        self.assertEqual(build.spec["image"]["watchdog"], "30s")


# Verifies that an early boot hang (before PID 1 starts and refreshes the
# watchdog) causes the hardware watchdog timer to expire, triggering a
# machine reset that enables systemd-boot to fall back to the last-known-good UKI.
class UpdateWatchdogReset(UpdateGuest, avocado.Test):
    """
    :avocado: tags=full,container
    """
    timeout = 7200
    watchdog = True

    def setUp(self):
        super().setUp()
        self.disk_1 = self.build("1", extra=WATCHDOG_SPEC)
        os.remove(self.build("2", extra=FREEZE_EXTRA))

    def test_watchdog_resets_on_early_boot_hang_and_falls_back(self):
        self.start(self.disk_1)
        self.assertEqual(self.booted_version(), "1")
        self.wait_for_network()

        # Update to version 2 (staged, waiting for reboot)
        res = self.update()
        self.assertEqual((res.get("result"), res.get("version")), ("ok", "2"))

        # Set 1 remaining try so a single watchdog timeout triggers fallback
        self.sh(f"mv {ESP}/debian-2+3-0.efi {ESP}/debian-2+1-0.efi && sync")

        # Reboot into the trial UKI
        self.guest.send("systemctl reboot\n")
        self.guest.expect(r"reboot: Restarting system", 150)

        # Version 2 freezes in early boot, printing our hook message
        self.guest.expect("seine-test: FREEZING EARLY BOOT FOR WATCHDOG", timeout=120)

        # Watchdog resets the machine; systemd-boot falls back to version 1
        self.login(timeout=180)
        self.assertEqual(self.booted_version(), "1")

        # Reconcile cleans up the failed update
        self.sh(f"{AGENT} reconcile")
        self.assert_invariants()
        self.assertEqual(self.esp(), ["debian-1.efi"])
        self.sh("awk '$1 == 2 { found = 1 } END { exit !found }' /var/lib/seine-update/failed")

        # Further updates to version 2 are denied
        res = self.update()
        self.assertEqual(res.get("result"), "nothing")
        self.assertIn("denied", res.get("message", ""))

    def test_watchdog_resets_exhausts_all_tries_and_falls_back(self):
        """
        :avocado: tags=long
        """
        self.start(self.disk_1)
        self.assertEqual(self.booted_version(), "1")
        self.wait_for_network()

        # Update to version 2 (staged, with default 3 tries)
        res = self.update()
        self.assertEqual((res.get("result"), res.get("version")), ("ok", "2"))

        # Reboot into the trial UKI
        self.guest.send("systemctl reboot\n")
        self.guest.expect(r"reboot: Restarting system", 150)

        # Each try freezes and triggers a watchdog hardware reset
        for _ in range(3):
            self.guest.expect("seine-test: FREEZING EARLY BOOT FOR WATCHDOG", timeout=120)

        # After tries are exhausted, systemd-boot falls back to version 1
        self.login(timeout=180)
        self.assertEqual(self.booted_version(), "1")

        # Reconcile cleans up the failed update
        self.sh(f"{AGENT} reconcile")
        self.assert_invariants()
        self.assertEqual(self.esp(), ["debian-1.efi"])
        self.sh("awk '$1 == 2 { found = 1 } END { exit !found }' /var/lib/seine-update/failed")

        # Further updates to version 2 are denied
        res = self.update()
        self.assertEqual(res.get("result"), "nothing")
        self.assertIn("denied", res.get("message", ""))
