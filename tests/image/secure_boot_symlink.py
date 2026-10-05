#!/usr/bin/env python3

import avocado
import os
import re
import sys

path_to_self    = os.path.realpath(__file__)
sys.path.append(os.path.dirname(path_to_self))

from secure_boot import SecureBootGuest

# A UKI names its deployment through /ostree/<stateroot>-<commit>, so it
# keeps booting that deployment when an update renumbers the boot entries.
class UkiSymlink(SecureBootGuest, avocado.Test):
    """
    :avocado: tags=full,container
    """
    timeout = 3600

    def ostree_words(self):
        entries = self.sh("cat /boot/loader/entries/*.conf")
        return re.findall(r"ostree=(\S+)", entries)

    def booted_deployment(self):
        return re.search(r"(?m)^\* \S+ (\S+)", self.sh("ostree admin status")).group(1)

    def test_an_old_uki_boots_its_own_deployment_after_an_update(self):
        self.enable_secure_boot()
        factory = self.booted_commit()
        self.assertEqual(self.booted_deployment(), "%s.0" % factory)
        self.fetch_boot_files()
        stale = self.ostree_words()
        self.assertEqual(len(stale), 1)

        # Same kernel, new commit: its own UKI, deployment and link.
        update = self.commit_update("2")
        self.install_uki("debian-u2", update, "u2", "100")
        self.deploy_update(update)
        self.assertNotIn(stale[0], self.ostree_words(), "the entry kept its path")
        self.guest.reboot()
        self.assertIn("systemd.hostname=u2", self.sh("cat /proc/cmdline"))
        self.assertEqual(self.booted_deployment(), "%s.0" % update)
        self.assertEqual(self.sh("cat /usr/share/seine-test/version"), "2")

        # A rollback flips the boot entries again; the factory UKI does not care.
        self.sh("ostree admin set-default 1")
        self.sh("bootctl set-oneshot debian-os.efi")
        self.guest.reboot()
        self.assertIn("systemd.hostname=uki-host", self.sh("cat /proc/cmdline"))
        self.assertEqual(self.booted_commit(), factory)
        self.assertEqual(self.booted_deployment(), "%s.0" % factory)
        self.sh("test ! -e /usr/share/seine-test/version")
