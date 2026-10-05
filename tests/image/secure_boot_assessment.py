#!/usr/bin/env python3

import avocado
import os
import re
import sys

path_to_self    = os.path.realpath(__file__)
sys.path.append(os.path.dirname(path_to_self))

from secure_boot import SecureBootGuest

ESP = "/efi/EFI/Linux"

# A UKI with a counter in its name ('x+3.efi') is tried three times by
# systemd-boot. A good boot renames it ('x.efi'), three failed ones leave
# 'x+0-3.efi' and the older UKI boots.
class BootAssessment(SecureBootGuest, avocado.Test):
    """
    :avocado: tags=full,container
    """
    timeout = 3600

    def setUp(self):
        super().setUp()
        self.enable_secure_boot()
        self.fetch_boot_files()
        self.sh("systemctl enable systemd-boot-check-no-failures.service")

    def test_a_counted_uki_is_renamed_after_a_good_boot(self):
        self.install_uki("debian-x", self.booted_commit(), "x", "100", tries=3)
        self.sh("test -e %s/debian-x+3.efi" % ESP)
        self.guest.reboot()
        self.assertIn("systemd.hostname=x", self.sh("cat /proc/cmdline"))
        self.wait_until("test -e %s/debian-x.efi" % ESP)
        self.sh("test ! -e %s/debian-x+*" % ESP)

    def test_a_uki_that_keeps_failing_gives_way_to_the_older_one(self):
        factory = self.booted_commit()
        update = self.commit_update("2")
        self.install_uki("debian-c", update, "c", "100", tries=3,
                         extra="systemd.unit=reboot.target")
        self.deploy_update(update)

        # The new UKI reboots at once, three times, and then the older one boots.
        self.guest.reboot(login=False)
        self.guest.login(timeout=300)
        self.assertIn("systemd.hostname=uki-host", self.sh("cat /proc/cmdline"))
        self.sh("test -e %s/debian-c+0-3.efi" % ESP)
        self.assertEqual(self.booted_commit(), factory)

        # The deployment of the failed update waits to be undeployed.
        status = self.sh("ostree admin status")
        self.assertRegex(status, r"(?m)^\s*debian %s\.0 \(pending\)" % update)
        self.assertRegex(status, r"(?m)^\* debian %s\.0" % factory)
