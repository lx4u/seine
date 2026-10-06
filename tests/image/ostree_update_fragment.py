#!/usr/bin/env python3
# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import avocado
import os
import shutil
import subprocess
import sys
import tempfile
import yaml

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine.build import BuildCmd

COMMON = os.path.join(path_to_sources, "examples", "common")
FILES = os.path.join(COMMON, "ostree-update")


class Scratch(avocado.Test):
    def make_scratch(self):
        self.tops = getattr(self, "tops", [])
        self.tops.append(tempfile.mkdtemp(prefix="seine-update-fragment-", dir="/var/tmp"))
        return self.tops[-1]

    def tearDown(self):
        for top in getattr(self, "tops", []):
            shutil.rmtree(top, ignore_errors=True)


# The public keys baked into the image are the halves of the fixed dev keys.
class DevKeys(Scratch):
    def test_the_key_files_hold_the_public_halves_of_the_dev_keys(self):
        if shutil.which("gpg") is None:
            self.cancel("gpg is needed")
        for vault_name, spec, ring in (
                ("ostree-commits", "dev-ostree-key.yaml", "commit-key.gpg"),
                ("update-manifest", "dev-update-manifest-key.yaml", "manifest-key.gpg")):
            with open(os.path.join(COMMON, spec)) as f:
                private = yaml.safe_load(f)["defaults"]["vault"][vault_name]["private_key"]
            home = os.path.join(self.make_scratch(), vault_name)
            os.makedirs(home, mode=0o700)
            run = lambda *args, **kw: subprocess.run(
                ["gpg", "--homedir", home, "--batch"] + list(args),
                capture_output=True, check=True, **kw)
            run("--import", input=private.encode())
            with open(os.path.join(FILES, ring), "rb") as f:
                self.assertEqual(run("--export").stdout, f.read(), ring)


# The fragment, as a spec loads it.
class Fragment(Scratch):
    def load(self):
        spec = os.path.join(self.make_scratch(), "main.yaml")
        with open(spec, "w") as f:
            f.write("requires:\n    - %s\n" % os.path.join(COMMON, "ostree-update") +
                    "update:\n    url: http://192.0.2.1/payload\n"
                    "image:\n    ostree:\n        stateroot: debian\n"
                    "        ref: debian/amd64\n"
                    "distribution:\n    release: trixie\n    architecture: amd64\n")
        build = BuildCmd()
        build.load_all([spec])
        return [t for p in build.spec["playbook"] for t in p["tasks"]]

    def play(self, name):
        return next(t for t in self.load() if t["name"] == name)

    def test_every_file_it_copies_is_found(self):
        sources = [t["copy"]["src"] for t in self.load() if "src" in t.get("copy", {})]
        self.assertEqual(len(sources), 8)
        for src in sources:
            self.assertTrue(os.path.isfile(src), src)

    def test_the_device_settings_come_from_the_spec(self):
        conf = self.play("tell the script about this device")["copy"]["content"]
        self.assertEqual(conf.split(), ["STATEROOT=debian", "REF=debian/amd64",
                                        "REMOTE=seine", "ESP=/efi/EFI/Linux"])

    def test_the_remote_checks_commits_and_summary(self):
        remote = self.play("add the remote")["copy"]["content"]
        for line in ("url=http://192.0.2.1/payload/repo", "gpg-verify=true",
                     "gpg-verify-summary=true"):
            self.assertIn(line, remote.splitlines())

    def test_the_transfer_file_reads_the_uki_directory(self):
        transfer = self.play("say where the UKIs come from")["copy"]["content"]
        self.assertIn("Path=http://192.0.2.1/payload/uki/", transfer)
        self.assertIn("MatchPattern=debian-@v.efi", transfer)
        self.assertIn("InstancesMax=4", transfer)


# The units, checked by systemd with the script where the units expect it.
class Units(Scratch):
    def test_systemd_accepts_the_units(self):
        if shutil.which("systemd-analyze") is None:
            self.cancel("systemd-analyze is needed")
        top = self.make_scratch()
        script = os.path.join(FILES, "seine-update")
        units = []
        for name in ("seine-update.service", "seine-update.timer",
                     "seine-update-reconcile.service"):
            with open(os.path.join(FILES, name)) as f:
                text = f.read().replace("/usr/libexec/seine-update/seine-update", script)
            units.append(os.path.join(top, name))
            with open(units[-1], "w") as f:
                f.write(text)
        checked = subprocess.run(["systemd-analyze", "verify"] + units,
                                 capture_output=True, text=True)
        self.assertEqual(checked.stderr.strip(), "")
        self.assertEqual(checked.returncode, 0)
