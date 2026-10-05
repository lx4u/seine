#!/usr/bin/env python3

import avocado
import os
import sys

path_to_self    = os.path.realpath(__file__)
sys.path.append(os.path.dirname(path_to_self))

import qemu_guest
from secure_boot import SecureBootGuest

# One signed UKI file can carry several command lines (profiles), and what
# is not signed (the entry picked, a boot entry file) can only choose
# among signed images.
class UkiProfiles(SecureBootGuest, avocado.Test):
    """
    :avocado: tags=full,container
    """
    timeout = 3600

    def test_a_profile_is_picked_for_one_boot_and_its_command_line_applies(self):
        self.enable_secure_boot()
        self.fetch_boot_files()
        self.install_uki("debian-multi", self.booted_commit(), "main", "100",
                         profiles=("p1", "p2"))
        entries = self.sh("bootctl list --json=short")
        for entry in ("debian-multi.efi@p1", "debian-multi.efi@p2"):
            self.assertIn(entry, entries)

        self.sh("bootctl set-oneshot debian-multi.efi@p1")
        self.guest.reboot()
        self.assertIn("systemd.hostname=p1", self.sh("cat /proc/cmdline"))
        self.guest.reboot()
        self.assertNotIn("systemd.hostname=p1", self.sh("cat /proc/cmdline"))

    def test_an_unsigned_kernel_in_a_boot_entry_file_is_refused(self):
        self.enable_secure_boot()
        self.fetch_boot_files()
        commit = self.booted_commit()
        self.sh("mkdir -p /efi/unsigned && cp %s/vmlinuz /efi/unsigned/vmlinuz "
                "&& cp %s/initramfs.img /efi/unsigned/initrd" % (
                    qemu_guest.SHARE, qemu_guest.SHARE))
        self.sh("printf 'title unsigned\\nlinux /unsigned/vmlinuz\\n"
                "initrd /unsigned/initrd\\noptions %s\\n' "
                "> /efi/loader/entries/unsigned.conf"
                % self.uki_cmdline(commit, "unsigned"))
        self.sh("bootctl set-oneshot unsigned.conf && sync")
        self.guest.reboot(login=False)
        self.guest.expect(r"(?i)access denied", 120)

        # The one-shot choice was used up: the next boot is a normal one.
        self.stop()
        self.start()
        self.assertNotIn("systemd.hostname=unsigned", self.sh("cat /proc/cmdline"))
