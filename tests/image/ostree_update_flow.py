#!/usr/bin/env python3
# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import avocado
import os
import sys

path_to_self    = os.path.realpath(__file__)
sys.path.append(os.path.dirname(path_to_self))

from update_guest import AGENT, ESP, UNHEALTHY, UpdateGuest

# The example image updates itself with its own agent: a good update is
# kept as the last-known-good, a bad one falls back and is removed for
# good, and old versions go as newer ones arrive.
class UpdateFlow(UpdateGuest, avocado.Test):
    """
    :avocado: tags=full,container
    """
    timeout = 7200

    def commit(self):
        return self.sh("cat /proc/cmdline").split("ostree=/ostree/debian-")[1].split()[0]

    # The agent sets the pin and removes failed updates when it runs.
    def settle(self):
        self.sh(f"{AGENT} reconcile")
        self.assert_invariants()

    def pinned(self):
        return [name for name, pin in self.deployments() if pin]

    def test_good_bad_and_good_updates(self):
        self.start(self.build("1"))
        self.assertEqual(self.update().get("result"), "nothing")
        self.settle()
        factory = self.commit()
        self.assertEqual(self.pinned(), [f"{factory}.0"])

        # A good update waits for its boot, then becomes the last-known-good.
        os.remove(self.build("2"))
        status = self.update()
        self.assertEqual((status["result"], status["version"]), ("ok", "2"))
        self.assertEqual(self.esp(), ["debian-1.efi", "debian-2+3-0.efi"])
        self.assertEqual(self.update()["result"], "waiting")
        self.reboot()
        self.assertEqual(self.booted_version(), "2")
        self.wait_until(f"test -e {ESP}/debian-2.efi")
        good = self.commit()
        self.settle()
        self.assertEqual(self.pinned(), [f"{good}.0"])
        self.assertEqual(self.esp(), ["debian-1.efi", "debian-2.efi"])

        # A bad update boots but is never blessed: three tries, then back.
        os.remove(self.build("3", UNHEALTHY))
        status = self.update()
        self.assertEqual((status["result"], status["version"]), ("ok", "3"))
        self.assertEqual(self.esp(), ["debian-2.efi", "debian-3+3-0.efi"])
        for tries in range(3):
            self.reboot()
            self.assertEqual(self.booted_version(), "3")
            self.assertEqual(self.pinned(), [f"{good}.0"])
        self.reboot()
        self.assertEqual(self.booted_version(), "2")
        self.assertEqual(self.commit(), good)
        self.wait_until(f"test ! -e {ESP}/debian-3+0-3.efi")
        self.settle()
        self.assertEqual(self.esp(), ["debian-2.efi"])
        self.assertEqual(self.pinned(), [f"{good}.0"])
        self.assertEqual(len(self.deployments()), 1)
        status = self.sh("cat /var/lib/seine-update/status")
        self.assertIn("result=failed-update", status)
        self.assertIn("version=3", status)
        self.sh("awk '$1 == 3 { found = 1 } END { exit !found }' /var/lib/seine-update/failed")

        # The bad version is still in the payload: it is not fetched again.
        status = self.update()
        self.assertEqual(status["result"], "nothing")
        self.assertIn("denied", status["message"])

        # A fixed version replaces it, and the factory version is long gone.
        os.remove(self.build("4"))
        status = self.update()
        self.assertEqual((status["result"], status["version"]), ("ok", "4"))
        self.reboot()
        self.assertEqual(self.booted_version(), "4")
        self.wait_until(f"test -e {ESP}/debian-4.efi")
        fixed = self.commit()
        self.settle()
        self.assertEqual(self.esp(), ["debian-2.efi", "debian-4.efi"])
        self.assertEqual(self.pinned(), [f"{fixed}.0"])
        self.assertEqual(len(self.deployments()), 2)
        self.sh(f"test ! -e /sysroot/ostree/debian-{factory}")
