#!/usr/bin/env python3
# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import os
import shutil
import sys
import time
import yaml
import zlib

import avocado

path_to_self = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)
sys.path.append(os.path.dirname(path_to_self))

from seine import vault
from seine.imager import ostree
from seine.imager import payload as payload_mod
from update_guest import AGENT, ESP, UpdateGuest


# The example image updates itself with its own agent: updates that have
# been tampered with or replayed must be refused, and the running system
# must stay intact.
class UpdateTamper(UpdateGuest, avocado.Test):
    """
    :avocado: tags=full,container
    """
    timeout = 7200

    def agent_status(self):
        lines = self.sh("cat /var/lib/seine-update/status").splitlines()
        return dict(line.split("=", 1) for line in lines)

    # Runs the agent expecting a failure, and checks that invariants hold.
    def update_fails(self):
        self.wait_for_network()
        status, output = self.guest.run(AGENT, 300)
        self.assertEqual(status, 1, f"agent succeeded unexpectedly: {output}")
        res = self.agent_status()
        self.assertEqual(res.get("result"), "failed")
        self.assert_invariants()
        return res

    def dev_vault(self):
        manifest_key_file = os.path.join(
            path_to_sources, "examples", "common", "dev-update-manifest-key.yaml")
        with open(manifest_key_file) as f:
            defaults = yaml.safe_load(f)["defaults"]["vault"]
        return vault.for_build(defaults)

    # Updates mtime of all payload files so http.server never returns 304.
    def touch_payload(self):
        now = time.time() + 10
        for root, dirs, files in os.walk(self.payload):
            for name in files:
                os.utime(os.path.join(root, name), (now, now))

    def restore_payload(self):
        shutil.rmtree(self.payload)
        shutil.copytree(self.pristine, self.payload)
        self.touch_payload()

    def read_commit_object(self, checksum):
        path = os.path.join(
            self.payload, "repo", "objects", checksum[:2], f"{checksum[2:]}.commit")
        with open(path, "rb") as f:
            data = f.read()
        try:
            return zlib.decompress(data)
        except zlib.error:
            return data

    def commit_checksum(self, version="2"):
        ref_path = os.path.join(
            self.payload, "repo", "refs", "heads", "debian", f"amd64.v{version}")
        with open(ref_path) as f:
            return f.read().strip()

    def test_tampered_and_replayed_updates(self):
        self.start(self.build("1"))
        self.assertEqual(self.update().get("result"), "nothing")
        self.sh(f"{AGENT} reconcile")
        self.assert_invariants()

        # Save version 1's summary before version 2 overwrites it.
        v1_summary_path = os.path.join(self.payload, "repo", "summary")
        with open(v1_summary_path, "rb") as f:
            v1_summary = f.read()
        with open(v1_summary_path + ".sig", "rb") as f:
            v1_summary_sig = f.read()

        # Keep a copy of version 1's payload for the replay test.
        v1_payload = os.path.join(self.workdir, "payload-v1")
        shutil.copytree(self.payload, v1_payload)

        # Build version 2 into the payload.
        os.remove(self.build("2"))
        c2 = self.commit_checksum("2")

        self.pristine = os.path.join(self.workdir, "payload-pristine")
        shutil.copytree(self.payload, self.pristine)

        # 1. Bad manifest signature: sysupdate cannot read the update list.
        sig_file = os.path.join(self.payload, "uki", "SHA256SUMS.gpg")
        with open(sig_file, "wb") as f:
            f.write(b"bad-signature")
        self.touch_payload()
        res = self.update_fails()
        self.assertIn("cannot read the update list", res.get("message", ""))
        self.restore_payload()

        # 2. UKI bytes differ from the manifest: download checksum mismatch.
        uki_file = os.path.join(self.payload, "uki", "debian-2.efi")
        with open(uki_file, "r+b") as f:
            f.seek(512)
            b = f.read(1)
            f.seek(512)
            f.write(bytes([b[0] ^ 0xff]))
        self.touch_payload()
        res = self.update_fails()
        self.assertEqual(res.get("version"), "2")
        self.assertIn("cannot install the UKI", res.get("message", ""))
        self.restore_payload()

        # 3. Summary missing: ostree pull fails without summary.
        os.remove(os.path.join(self.payload, "repo", "summary"))
        os.remove(os.path.join(self.payload, "repo", "summary.sig"))
        res = self.update_fails()
        self.assertIn("cannot pull", res.get("message", ""))
        self.restore_payload()

        # 4. Stale summary: points to commit 1, differing from version 2.
        with open(os.path.join(self.payload, "repo", "summary"), "wb") as f:
            f.write(v1_summary)
        with open(os.path.join(self.payload, "repo", "summary.sig"), "wb") as f:
            f.write(v1_summary_sig)
        self.touch_payload()
        res = self.update_fails()
        self.assertIn("differs from 2", res.get("message", ""))
        self.restore_payload()

        # 5. Unsigned commit by id: commitmeta has no signatures.
        meta_file = os.path.join(
            self.payload, "repo", "objects", c2[:2], f"{c2[2:]}.commitmeta")
        with open(meta_file, "wb") as f:
            f.write(b"")
        self.touch_payload()
        res = self.update_fails()
        self.assertIn("cannot pull", res.get("message", ""))
        self.restore_payload()

        # 6. Commit signed by another key: signed by update-manifest, not trusted.
        provider = self.dev_vault()
        raw_commit = self.read_commit_object(c2)
        wrong_sig = provider.pgp_detach_sign(
            "update-manifest", raw_commit, int(time.time()))
        with open(meta_file, "wb") as f:
            f.write(ostree.commitmeta([wrong_sig]))
        self.touch_payload()
        res = self.update_fails()
        self.assertIn("cannot pull", res.get("message", ""))
        self.restore_payload()

        # 7. UKI of another commit (H4): UKI is valid but names another commit.
        with open(uki_file, "rb") as f:
            uki_data = f.read()
        other_commit = "0" * 64
        self.assertIn(c2.encode(), uki_data)
        wrong_uki = uki_data.replace(c2.encode(), other_commit.encode())
        with open(uki_file, "wb") as f:
            f.write(wrong_uki)
        payload_mod.write_manifest(
            self.payload,
            lambda text: provider.pgp_detach_sign(
                "update-manifest", text, int(time.time())))
        self.touch_payload()
        res = self.update_fails()
        self.assertEqual(res.get("version"), "2")
        self.assertIn("does not name the commit", res.get("message", ""))
        self.assertEqual(self.esp(), ["debian-1.efi"])
        self.restore_payload()

        # Verify that after all rejected updates, a pristine update succeeds.
        status = self.update()
        self.assertEqual((status["result"], status["version"]), ("ok", "2"))
        self.reboot()
        self.assertEqual(self.booted_version(), "2")
        self.wait_until(f"test -e {ESP}/debian-2.efi")
        self.sh(f"{AGENT} reconcile")
        self.assert_invariants()

        # 8. Replay of an older signed payload: ignored on version 2.
        shutil.rmtree(self.payload)
        shutil.copytree(v1_payload, self.payload)
        self.touch_payload()
        status = self.update()
        self.assertEqual(status["result"], "nothing")
        self.assert_invariants()
