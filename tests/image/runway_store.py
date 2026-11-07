#!/usr/bin/env python3
# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import avocado
import os
import shutil
import subprocess
import sys
import tempfile

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine.imager import runway

TRIPLET = {"amd64": "x86_64-linux-gnu", "arm64": "aarch64-linux-gnu"}

# Plain Python, no guestfs appliance involved -- records what is done.
class FakeGuestfs:
    def __init__(self, arch):
        lib = f"/usr/lib/{TRIPLET[arch]}/runway"
        self.files = {f"{lib}/rwy-loader.efi"}
        self.dirs = {f"{lib}/wdt"}
        self.wdt = f"{lib}/wdt"
        self.downloaded = []
        self.uploaded = []

    def is_file(self, path):
        return path in self.files

    def is_dir(self, path):
        return path in self.dirs

    def ls(self, path):
        return ["I6300ESB.EFI", "ITCO.EFI", "readme.txt"]

    def download(self, src, dst):
        open(dst, "wb").close()
        self.downloaded.append(src)

    def upload(self, src, dst):
        self.uploaded.append((src, dst))


class StoreProvisioning(avocado.Test):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="seine-test-runway-")
        self.work = os.path.join(self.dir, "work")
        os.mkdir(self.work)
        self.uki = os.path.join(self.dir, "uki.efi")
        open(self.uki, "wb").close()
        self.cmds = []
        self.tools_missing = False

    def tearDown(self):
        shutil.rmtree(self.dir)

    def run(self, args):
        if self.tools_missing:
            raise subprocess.CalledProcessError(1, args)
        self.cmds.append(args)
        if args[0] == "mkstore":
            open(os.path.join(self.work, args[-1]), "wb").close()

    def provision(self, g, arch="amd64", **opts):
        runway.provision_store(g, self.run, arch, self.work, "/dev/sda2", 64,
                               "8b822277-1913-4a33-a7c7-c043958a93ae",
                               self.uki, "debian-1.efi", **opts)

    def test_the_store_is_formatted_filled_confirmed_and_written(self):
        g = FakeGuestfs("amd64")
        self.provision(g)
        preflight, mkstore, wdt_set, install, confirm = self.cmds
        self.assertEqual(preflight, ["sh", "-c", "command -v mkstore rwyctl"])
        self.assertEqual(mkstore[:3], ["mkstore", "--size", "64"])
        self.assertIn("--quota=80", mkstore)
        self.assertEqual(
            wdt_set,
            ["rwyctl", "--store", "RUNWAY.DAT", "wdt", "set", "--unlock",
             "wdt/I6300ESB.EFI", "wdt/ITCO.EFI"],
        )
        self.assertEqual(install[:5], ["rwyctl", "--store", "RUNWAY.DAT", "install", "--name"])
        self.assertIn("debian-1.efi", install)
        self.assertNotIn("--watchdog", install)
        self.assertEqual(confirm[-2:], ["confirm", "CONFIG1"])
        self.assertEqual(g.uploaded, [(os.path.join(self.work, "RUNWAY.DAT"), "/dev/sda2")])

    def test_only_efi_watchdog_drivers_are_taken(self):
        self.provision(FakeGuestfs("amd64"))
        self.assertEqual(
            sorted(os.listdir(os.path.join(self.work, "wdt"))),
            ["I6300ESB.EFI", "ITCO.EFI"])

    def test_watchdog_and_seed_are_passed_on(self):
        self.provision(FakeGuestfs("amd64"), watchdog=60, seed="abcd")
        _, mkstore, wdt_set, set_wd, install, _ = self.cmds
        self.assertEqual(mkstore[-3:-1], ["--random-seed", "abcd"])
        self.assertEqual(
            wdt_set,
            ["rwyctl", "--store", "RUNWAY.DAT", "wdt", "set", "--unlock",
             "wdt/I6300ESB.EFI", "wdt/ITCO.EFI"],
        )
        self.assertEqual(set_wd, ["rwyctl", "--store", "RUNWAY.DAT", "set", "WATCHDOG", "60"])
        self.assertEqual(install[-2:], ["--watchdog", "60"])

    def test_arm64_loader_files_are_found_by_their_triplet(self):
        self.provision(FakeGuestfs("arm64"), arch="arm64")
        self.assertEqual(len(self.cmds), 5)

    def test_missing_loader_files_are_refused(self):
        g = FakeGuestfs("amd64")
        g.dirs.clear()
        with self.assertRaises(RuntimeError):
            self.provision(g)

    def test_missing_tools_are_explained(self):
        self.tools_missing = True
        with self.assertRaises(RuntimeError) as cm:
            self.provision(FakeGuestfs("amd64"))
        self.assertIn("imager: packages", str(cm.exception))
