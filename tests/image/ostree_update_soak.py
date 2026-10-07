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

SCRIPT_CHANGE = """
playbook:
    - name: modify the update script
      priority: 950
      tasks:
          - name: add marker to seine-update
            lineinfile:
                path: /usr/libexec/seine-update/seine-update
                line: "# SEINE_UPDATE_VERSION=2"
"""


# Checks that loading the pc-ostree-update example with a modified update
# script parses and loads the playbook tasks.
class SoakUpdateSpec(avocado.Test):
    def test_spec_with_script_change_parses(self):
        build = BuildCmd()
        peer = os.path.join(self.workdir, "script_change.yml")
        with open(peer, "w") as f:
            f.write(SCRIPT_CHANGE)
        build.load_all([EXAMPLE, peer])
        names = [p.get("name") for p in build.spec.get("playbook", [])]
        self.assertIn("modify the update script", names)


# Verifies five updates in a row (soak), updating across an update script
# change, and refusing an update when the ESP lacks sufficient free room.
class UpdateSoak(UpdateGuest, avocado.Test):
    """
    :avocado: tags=full,container
    """
    timeout = 7200

    def settle(self):
        self.sh(f"{AGENT} reconcile")
        self.assert_invariants()

    def check_state(self, expected_version, expected_ukis=2, expected_deploys=2):
        self.assertEqual(self.booted_version(), expected_version)
        self.assertEqual(len(self.esp()), expected_ukis)
        self.assertEqual(len(self.deployments()), expected_deploys)
        links = self.sh("ls /sysroot/ostree | grep '^debian-' || true").split()
        self.assertEqual(len(links), expected_deploys)
        self.assert_invariants()
        free_esp = int(self.sh(f"df -B1 --output=avail {ESP} | tail -n 1").strip())
        self.assertGreater(free_esp, 50 * 1024 * 1024)

    def test_soak_five_updates_script_change_and_full_esp(self):
        # Start on factory disk (version 1)
        self.start(self.build("1"))
        self.settle()
        self.check_state("1", expected_ukis=1, expected_deploys=1)

        # Update 1: version 2
        os.remove(self.build("2"))
        status = self.update()
        self.assertEqual((status["result"], status["version"]), ("ok", "2"))
        self.reboot()
        self.assertEqual(self.booted_version(), "2")
        self.wait_until(f"test -e {ESP}/debian-2.efi")
        self.settle()
        self.check_state("2")

        # Update 2: version 3
        os.remove(self.build("3"))
        status = self.update()
        self.assertEqual((status["result"], status["version"]), ("ok", "3"))
        self.reboot()
        self.assertEqual(self.booted_version(), "3")
        self.wait_until(f"test -e {ESP}/debian-3.efi")
        self.settle()
        self.check_state("3")

        # Update 3: version 4
        os.remove(self.build("4"))
        status = self.update()
        self.assertEqual((status["result"], status["version"]), ("ok", "4"))
        self.reboot()
        self.assertEqual(self.booted_version(), "4")
        self.wait_until(f"test -e {ESP}/debian-4.efi")
        self.settle()
        self.check_state("4")

        # Update 4: version 5 (ships modified update script)
        os.remove(self.build("5", extra=SCRIPT_CHANGE))
        status = self.update()
        self.assertEqual((status["result"], status["version"]), ("ok", "5"))
        self.reboot()
        self.assertEqual(self.booted_version(), "5")
        self.wait_until(f"test -e {ESP}/debian-5.efi")
        self.sh("grep -q 'SEINE_UPDATE_VERSION=2' /usr/libexec/seine-update/seine-update")
        self.settle()
        self.check_state("5")

        # Build version 6 (reuses rootfs cache of version 5)
        os.remove(self.build("6", extra=SCRIPT_CHANGE))

        # Test full ESP refusal: fill the ESP and verify update is refused
        self.sh("dd if=/dev/zero of=/efi/fill.dat bs=1M count=480 2>/dev/null || true; sync")
        status, output = self.guest.run(AGENT, 300)
        self.assertEqual(status, 1)
        self.assertIn("not enough room on the ESP", output)
        lines = self.sh("cat /var/lib/seine-update/status").splitlines()
        res = dict(line.split("=", 1) for line in lines)
        self.assertEqual(res.get("result"), "failed")
        self.assertIn("not enough room on the ESP", res.get("message", ""))

        # Running system is unharmed; clean up filler file
        self.sh("rm -f /efi/fill.dat && sync")
        self.settle()
        self.check_state("5")

        # Update 5: version 6 now succeeds using the updated script
        status = self.update()
        self.assertEqual((status["result"], status["version"]), ("ok", "6"))
        self.reboot()
        self.assertEqual(self.booted_version(), "6")
        self.wait_until(f"test -e {ESP}/debian-6.efi")
        self.sh("grep -q 'SEINE_UPDATE_VERSION=2' /usr/libexec/seine-update/seine-update")
        self.settle()
        self.check_state("6")
