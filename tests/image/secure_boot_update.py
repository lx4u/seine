#!/usr/bin/env python3

import avocado
import os
import sys

path_to_self    = os.path.realpath(__file__)
sys.path.append(os.path.dirname(path_to_self))

from secure_boot import SecureBootGuest

ESP = "/efi/EFI/Linux"

# What the on-device updater does, by hand over the serial console: a good
# update is deployed and blessed; a bad one falls back and is removed with
# its link and its UKI. The updater itself is not part of these tests.
class UpdateOrder(SecureBootGuest, avocado.Test):
    """
    :avocado: tags=full,container
    """
    timeout = 3600

    def ostree_status(self):
        return self.sh("ostree admin status")

    def test_a_good_update_is_blessed_and_a_bad_one_is_removed(self):
        self.enable_secure_boot()
        self.fetch_boot_files()
        self.sh("systemctl enable systemd-boot-check-no-failures.service")
        factory = self.booted_commit()

        # Good update: UKI first, then the deployment and its link.
        good = self.commit_update("2")
        self.install_uki("debian-u2", good, "u2", "100", tries=3)
        self.deploy_update(good)
        self.guest.reboot()
        self.assertIn("systemd.hostname=u2", self.sh("cat /proc/cmdline"))
        self.assertEqual(self.booted_commit(), good)
        self.wait_until("test -e %s/debian-u2.efi" % ESP)

        # Bad update: the new UKI fails three times, the blessed one boots.
        bad = self.commit_update("3")
        self.install_uki("debian-u3", bad, "u3", "101", tries=3,
                         extra="systemd.unit=reboot.target")
        self.deploy_update(bad)
        self.guest.reboot(login=False)
        self.guest.login(timeout=300)
        self.assertIn("systemd.hostname=u2", self.sh("cat /proc/cmdline"))
        self.assertEqual(self.booted_commit(), good)
        self.sh("test -e %s/debian-u3+0-3.efi" % ESP)
        self.assertRegex(self.ostree_status(), r"(?m)^\s*debian %s\.0 \(pending\)" % bad)

        # ostree keeps the booted and the new deployment only, so the factory
        # one went with the deploy: its link dangles and its UKI cannot boot.
        link = "/sysroot/ostree/debian-%s" % factory
        self.assertNotIn(factory, self.ostree_status())
        self.sh("test -L %s && test ! -e %s" % (link, link))

        # Each deployment that goes takes its UKI and its link along.
        self.sh("ostree admin undeploy 0")
        self.sh("rm %s/debian-u3+0-3.efi /sysroot/ostree/debian-%s" % (ESP, bad))
        self.sh("rm %s/debian-os.efi %s && sync" % (ESP, link))
        self.assertNotIn(bad, self.ostree_status())
        self.assertEqual(self.sh("ls %s" % ESP), "debian-u2.efi")
        self.sh("ls /sysroot/ostree | grep -c '^debian-' | grep -qx 1")

        self.guest.reboot()
        self.assertEqual(self.booted_commit(), good)
