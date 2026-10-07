#!/usr/bin/env python3
# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import os
import shutil
import subprocess
import sys

import avocado

path_to_self    = os.path.realpath(__file__)
sys.path.append(os.path.dirname(path_to_self))

import ostree_update_flow
import qemu_guest
from update_guest import ESP

# The update flow again, with the dev key enrolled as db: every UKI that the
# imager signs must boot, and one that is not signed must not.
class UpdateFlowSecureBoot(ostree_update_flow.UpdateFlow):
    """
    :avocado: tags=full,container
    """
    secure_boot = True

    def test_unsigned_uki_is_refused(self):
        if shutil.which("sbattach") is None:
            self.cancel("sbattach is needed to remove a signature")
        self.start(self.build("1"))
        os.remove(self.build("2"))
        unsigned = os.path.join(self.share, "unsigned.efi")
        shutil.copy(os.path.join(self.payload, "uki", "debian-2.efi"), unsigned)
        subprocess.run(["sbattach", "--remove", unsigned], check=True)
        # One try left: the firmware fails to start it, and the next boot
        # runs the factory UKI.
        name = "debian-2+1-0.efi"
        self.sh(f"cp {qemu_guest.SHARE}/unsigned.efi {ESP}/{name}.tmp "
                f"&& mv {ESP}/{name}.tmp {ESP}/{name} && sync")
        self.reboot()
        self.assertEqual(self.booted_version(), "1")
        # The try was used and refused: the agent has removed the failed file.
        self.wait_until(f"test ! -e {ESP}/debian-2+0-1.efi")
        self.assertEqual(self.esp(), ["debian-1.efi"])
