#!/usr/bin/env python3
# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import os
import sys

import avocado

path_to_self = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)
sys.path.append(os.path.dirname(path_to_self))

from seine.build import BuildCmd
from update_guest import AGENT, ESP, EXAMPLE, UpdateGuest


# Checks that loading the pc-ostree-update example with static deltas
# parses and sets deltas-from in the spec.
class DeltaUpdateSpec(avocado.Test):
    def test_spec_with_deltas_from_parses(self):
        build = BuildCmd()
        peer = os.path.join(self.workdir, "deltas.yml")
        with open(peer, "w") as f:
            f.write(
                "image:\n"
                "    ostree:\n"
                '        version: "23"\n'
                "        payload:\n"
                '            deltas-from: ["20", "22"]\n'
            )
        build.load_all([EXAMPLE, peer])
        deltas = build.spec.get("image", {}).get("ostree", {}).get("payload", {}).get("deltas-from")
        self.assertEqual(deltas, ["20", "22"])


# Verifies payload static deltas and guest updates across delta and object-fetch paths.
class UpdateDelta(UpdateGuest, avocado.Test):
    """
    :avocado: tags=full,container
    """
    timeout = 7200

    def settle(self):
        self.sh(f"{AGENT} reconcile")
        self.assert_invariants()

    def test_static_deltas_and_object_fetch_updates(self):
        # Build baseline versions into payload: 19, 20 and 22 shipped, 21 never built.
        disk_19 = self.build("19")
        disk_20 = self.build("20")
        disk_22 = self.build("22")

        # Verify that version 21 was never built.
        self.assertFalse(os.path.exists(os.path.join(self.payload, "uki", "debian-21.efi")))
        self.assertFalse(os.path.exists(os.path.join(self.payload, "repo", "refs", "heads", "debian/amd64.v21")))

        # Building version 23 asking for deltas from missing version 21 is refused early.
        log = self.build_fails("23", deltas_from=["20", "21", "22"])
        self.assertIn("'deltas-from: 21' is not in", log)

        # Build version 23 with deltas-from: ["20", "22"].
        disk_23 = self.build("23", deltas_from=["20", "22"])
        os.remove(disk_23)

        # Verify that deltas exist for the requested baselines.
        deltas_dir = os.path.join(self.payload, "repo", "deltas")
        self.assertTrue(os.path.isdir(deltas_dir))
        self.assertEqual(len(os.listdir(deltas_dir)), 2)

        # Guest on version 20 reaches 23 (delta path).
        self.start(disk_20)
        self.assertEqual(self.booted_version(), "20")
        self.settle()
        status = self.update()
        self.assertEqual((status["result"], status["version"]), ("ok", "23"))
        self.reboot()
        self.assertEqual(self.booted_version(), "23")
        self.wait_until(f"test -e {ESP}/debian-23.efi")
        self.settle()
        self.assertEqual(self.esp(), ["debian-20.efi", "debian-23.efi"])

        # Guest on version 22 reaches 23 (delta path).
        self.start(disk_22)
        self.assertEqual(self.booted_version(), "22")
        self.settle()
        status = self.update()
        self.assertEqual((status["result"], status["version"]), ("ok", "23"))
        self.reboot()
        self.assertEqual(self.booted_version(), "23")
        self.wait_until(f"test -e {ESP}/debian-23.efi")
        self.settle()
        self.assertEqual(self.esp(), ["debian-22.efi", "debian-23.efi"])

        # Guest on version 19 (no delta configured to 23) updates via object-fetch path.
        self.start(disk_19)
        self.assertEqual(self.booted_version(), "19")
        self.settle()
        status = self.update()
        self.assertEqual((status["result"], status["version"]), ("ok", "23"))
        self.reboot()
        self.assertEqual(self.booted_version(), "23")
        self.wait_until(f"test -e {ESP}/debian-23.efi")
        self.settle()
        self.assertEqual(self.esp(), ["debian-19.efi", "debian-23.efi"])
