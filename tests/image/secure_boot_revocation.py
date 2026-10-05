#!/usr/bin/env python3

import avocado
import os
import sys

path_to_self    = os.path.realpath(__file__)
sys.path.append(os.path.dirname(path_to_self))

import qemu_guest
from secure_boot import SecureBootGuest

# An old UKI keeps its valid signature, so it boots until its hash is added
# to dbx, with an update that the KEK signs.
class UkiRevocation(SecureBootGuest, avocado.Test):
    """
    :avocado: tags=full,container
    """
    timeout = 3600

    def boot_old_uki_once(self):
        self.sh("bootctl set-oneshot debian-os.efi")
        self.guest.reboot(login=False)

    def test_a_uki_in_dbx_is_refused(self):
        self.enable_secure_boot()
        self.fetch_boot_files()
        self.install_uki("debian-new", self.booted_commit(), "new", "100")
        self.sh("cp /efi/EFI/Linux/debian-os.efi %s/old.efi" % qemu_guest.SHARE)
        self.keys.revocation(os.path.join(self.share, "old.efi"),
                             os.path.join(self.share, "dbx.auth"))

        self.boot_old_uki_once()
        self.guest.login()
        self.assertIn("systemd.hostname=uki-host", self.sh("cat /proc/cmdline"))

        self.sh("efi-updatevar -a -f %s/dbx.auth dbx" % qemu_guest.SHARE)
        self.boot_old_uki_once()
        self.guest.expect(r"(?i)access denied", 120)

        # The newer UKI, which dbx does not name, still boots.
        self.stop()
        self.start()
        self.assertIn("systemd.hostname=new", self.sh("cat /proc/cmdline"))
