#!/usr/bin/env python3

import avocado
import os
import sys

path_to_self    = os.path.realpath(__file__)
sys.path.append(os.path.dirname(path_to_self))

from secure_boot import SecureBootGuest

# The throw-away keys are enrolled from the guest while the firmware is in
# Setup Mode; after that only what db signs may run.
class SecureBootBoots(SecureBootGuest, avocado.Test):
    """
    :avocado: tags=full,container
    """
    timeout = 3600

    def test_the_signed_boot_chain_boots_with_secure_boot_enabled(self):
        self.enable_secure_boot()
        cmdline = self.sh("cat /proc/cmdline")
        self.assertIn("systemd.hostname=uki-host", cmdline)
        self.assertRegex(cmdline, r"ostree=/ostree/debian-[0-9a-f]{64}")
