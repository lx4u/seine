#!/usr/bin/env python3
# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import avocado
import os
import sys

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(os.path.dirname(path_to_self))

from update_guest import ESP, UpdateGuest
from tests.testutils import RELEASE

EXAMPLES = os.path.join(path_to_sources, "examples")

# The update example, turned into a signed composefs image (needs forky).
COMPOSEFS = f"""
requires:
    - stdlib:debian/forky.yml
    - {EXAMPLES}/ostree-composefs/packages
    - {EXAMPLES}/common/dev-composefs-key
    - {EXAMPLES}/pc-ostree-composefs-image/composefs
image:
    ostree:
        mode: composefs
        composefs-key: vault:composefs-commits
"""

# The update agent on a composefs root: the new commit carries the
# signature that the initramfs of its UKI checks.
class UpdateComposefs(UpdateGuest, avocado.Test):
    """
    :avocado: tags=full,container
    """
    timeout = 7200

    def setUp(self):
        if RELEASE != "forky":
            self.cancel("composefs needs SEINE_TEST_RELEASE=forky")
        super().setUp()

    def prepare_root_log(self):
        return self.sh("journalctl -b -t ostree-prepare-root --no-pager")

    def assert_signed_composefs(self):
        self.assertEqual(self.sh("findmnt -no FSTYPE /"), "overlay")
        log = self.prepare_root_log()
        self.assertIn("Validated commit signature using "
                      "'/etc/ostree/initramfs-root-binding.key'", log)
        self.assertIn("composefs: mounted successfully (verity=true)", log)

    def test_an_update_boots_as_signed_composefs(self):
        self.start(self.build("1", COMPOSEFS))
        self.assert_signed_composefs()
        self.assertEqual(self.update().get("result"), "nothing")

        os.remove(self.build("2", COMPOSEFS))
        status = self.update()
        self.assertEqual((status["result"], status["version"]), ("ok", "2"))
        self.reboot()
        self.assertEqual(self.booted_version(), "2")
        self.assert_signed_composefs()
        self.wait_until(f"test -e {ESP}/debian-2.efi")
        self.sh("/usr/libexec/seine-update/seine-update reconcile")
        self.assert_invariants()
