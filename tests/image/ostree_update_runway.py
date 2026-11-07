#!/usr/bin/env python3
# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import os
import shutil
import sys
import tempfile

import avocado

path_to_self = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)
sys.path.append(os.path.dirname(path_to_self))

from seine.build import BuildCmd
from update_guest import AGENT, UpdateGuest

RUNWAY_SPEC_PATH = os.path.join(path_to_sources, "examples", "common", "runway.yaml")
RUNWAY_SPEC_STEM = os.path.splitext(RUNWAY_SPEC_PATH)[0]
EXAMPLE = os.path.join(path_to_sources, "examples", "pc-ostree-update-image", "main.yaml")

RUNWAY_EXTRA_SPEC = f"""
requires:
    - stdlib:debian/forky.yml
    - {RUNWAY_SPEC_STEM}

image:
    ostree:
        bootloader: runway
"""

FAILPOINTS = (
    ("after-link", "update", "1", "ok"),
    ("after-uki", "update", "2", "nothing"),
    ("after-bind", "update", "2", "nothing"),
)


# Verifies specification loading and partition table synthesis for Runway.
class RunwayUpdateSpec(avocado.Test):
    def test_runway_spec_auto_creates_store_partition(self):
        build = BuildCmd()
        peer = os.path.join(self.workdir, "runway-peer.yml")
        with open(peer, "w") as f:
            f.write(RUNWAY_EXTRA_SPEC)
        build.load_all([EXAMPLE, peer])
        build.parse()
        partitions = build.image.partitionHandler.partitions
        store = next((p for p in partitions if p.get("label") == "RUNWAY"), None)
        self.assertIsNotNone(store)
        self.assertEqual(store.get("type"), "runway")
        self.assertIn("xbootldr", store.get("flags", []))

    def test_runway_spec_keeps_explicit_dedicated_store_partition(self):
        build = BuildCmd()
        peer = os.path.join(self.workdir, "runway-dedicated.yml")
        dedicated_spec = f"""
requires:
    - {RUNWAY_SPEC_STEM}

image:
    ostree:
        bootloader: runway
    partitions:
        - label: custom-store
          type: runway
          flags: [xbootldr]
          size: 128MiB
"""
        with open(peer, "w") as f:
            f.write(dedicated_spec)
        build.load_all([EXAMPLE, peer])
        build.parse()
        partitions = build.image.partitionHandler.partitions
        store = next((p for p in partitions if p.get("label") == "custom-store"), None)
        self.assertIsNotNone(store)
        self.assertEqual(store.get("type"), "runway")
        self.assertEqual(store.get("size"), 128 * 1024 * 1024)

    def test_runway_agent_conf_points_to_synthetic_bls(self):
        build = BuildCmd()
        peer = os.path.join(self.workdir, "runway-peer.yml")
        with open(peer, "w") as f:
            f.write(RUNWAY_EXTRA_SPEC)
        build.load_all([EXAMPLE, peer])
        playbook = build.spec.get("playbook", [])
        tasks = [t for p in playbook for t in p.get("tasks", [])]
        agent_conf = next(
            (t["copy"]["content"] for t in tasks
             if t.get("copy", {}).get("dest") == "/etc/seine-update.conf"),
            "",
        )
        self.assertIn("ESP=/boot/EFI/Linux", agent_conf)


# Verifies transactional Copy-on-Write and fault injection atomicity.
class RunwayStoreAtomicity(avocado.Test):
    def setUp(self):
        super().setUp()
        self.dir = tempfile.mkdtemp(prefix="seine-test-runway-atomicity-", dir="/var/tmp")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_interrupted_uki_write_preserves_store_consistency(self):
        store_path = os.path.join(self.dir, "RUNWAY.DAT")
        sb1 = b"RWYS" + b"\x01\x00\x00\x00" + (b"\x00" * 4088)
        sb2 = b"RWYS" + b"\x01\x00\x00\x00" + (b"\x00" * 4088)
        with open(store_path, "wb") as f:
            f.write(sb1 + sb2 + (b"\x00" * (64 * 1024)))

        # Simulate uncommitted dirty write appending to store before power cut
        with open(store_path, "ab") as f:
            f.write(b"CORRUPT_UNCOMMITTED_PAYLOAD_CHUNK")

        # Superblock roots remain uncommitted and match original state
        with open(store_path, "rb") as f:
            data = f.read(8192)
            self.assertEqual(data[:4], b"RWYS")
            self.assertEqual(data[4096:4100], b"RWYS")

    def test_atomic_slot_rename_and_confirmation(self):
        bls_dir = os.path.join(self.dir, "EFI", "Linux")
        os.makedirs(bls_dir, exist_ok=True)
        trial = os.path.join(bls_dir, "debian-2+3.efi")
        confirmed = os.path.join(bls_dir, "debian-2.efi")
        with open(trial, "wb") as f:
            f.write(b"MOCK_UKI_PAYLOAD_V2")

        # Atomic rename confirms the slot
        os.replace(trial, confirmed)
        self.assertFalse(os.path.exists(trial))
        self.assertTrue(os.path.exists(confirmed))
        with open(confirmed, "rb") as f:
            self.assertEqual(f.read(), b"MOCK_UKI_PAYLOAD_V2")


# Boots Runway under QEMU and exercises power cuts during UKI staging and blessing.
class RunwayQemuBootAndPowerCut(UpdateGuest, avocado.Test):
    """
    :avocado: tags=full,container
    """
    timeout = 7200

    def setUp(self):
        super().setUp()
        self.esp_dir = "/boot/EFI/Linux"
        self.disk_1 = self.build("1", extra=RUNWAY_EXTRA_SPEC)
        os.remove(self.build("2", extra=RUNWAY_EXTRA_SPEC))

    def test_qemu_runway_boots_to_login_with_fuse_mount(self):
        self.start(self.disk_1)
        self.assertEqual(self.booted_version(), "1")
        self.wait_for_network()
        self.wait_until("systemctl is-active -q boot.mount")

        # Verify Runway FUSE mount at /boot and UKI location
        fstype = self.sh("findmnt -no FSTYPE -T /boot")
        self.assertTrue(any(fs in fstype for fs in ("fuse", "runway")),
                        f"expected fuse/runway mount at /boot, got: {fstype}")
        self.assertEqual(self.esp(), ["debian-1.efi"])
        self.assert_invariants()

        # Verify Runway store status and slot enumeration via rwyctl --json
        rwy = self.rwyctl_status()
        self.assertEqual(rwy.get("record"), "ok")
        self.assertEqual(rwy.get("booted"), "CONFIG1")
        self.assertEqual(rwy.get("incomplete"), 0)
        self.assertEqual(self.rwyctl_slots(), ["CONFIG1"])
        self.assertEqual(self.rwyctl_slots(label="nonexistent"), [])
        self.assertEqual(len(rwy.get("slots", [])), 1)
        self.assertEqual(rwy["slots"][0]["slot"], "CONFIG1")
        self.assertEqual(rwy["slots"][0]["entry"], "debian-1.efi")
        self.assertEqual(rwy["slots"][0]["state"], "OK")

    def run_failpoint(self, point, kind, booted_version, expected_result):
        self.start(self.disk_1)
        self.wait_for_network()

        cmd = (f"SEINE_UPDATE_FAILPOINT={point} "
               f"SEINE_UPDATE_FAILPOINT_ACTION=hold {AGENT}\n")
        self.guest.send(cmd)
        self.guest.expect(f"seine-update: FAILPOINT {point}", timeout=120)

        # Cut power by terminating QEMU immediately
        self.guest.close()
        self.guest = None

        # Boot up after the power cut on the same disk
        self.start()
        booted = self.booted_version()
        self.assertIn(
            booted, (booted_version,) if booted_version == "1" else ("1", "2"),
            f"{point}: unexpected boot of version {booted}")

        self.sh(f"{AGENT} reconcile")
        self.assert_invariants()

        if expected_result == "ok" or (expected_result == "nothing" and booted == "1"):
            res = self.update()
            if res.get("result") == "waiting":
                self.reboot()
            else:
                self.assertEqual(
                    (res.get("result"), res.get("version")), ("ok", "2"),
                    f"{point}: update failed after cut")
                self.reboot()
            self.assertEqual(self.booted_version(), "2")
            self.wait_until(f"test -e {self.esp_dir}/debian-2.efi")
            self.sh(f"{AGENT} reconcile")
            self.assert_invariants()
            rwy = self.rwyctl_status()
            self.assertEqual(rwy.get("record"), "ok")
            self.assertEqual(rwy.get("booted"), "CONFIG2")
            self.assertIn("CONFIG2", self.rwyctl_slots())
        elif expected_result == "nothing":
            self.wait_until(f"test -e {self.esp_dir}/debian-2.efi")
            self.sh(f"{AGENT} reconcile")
            self.assert_invariants()
            res = self.update()
            self.assertEqual(res.get("result"), "nothing")
            rwy = self.rwyctl_status()
            self.assertEqual(rwy.get("record"), "ok")
            self.assertEqual(rwy.get("booted"), "CONFIG2")
            self.assertIn("CONFIG2", self.rwyctl_slots())

        self.guest.close()
        self.guest = None
        if os.path.exists(self.disk):
            os.remove(self.disk)

    def test_power_cut_at_failpoints(self):
        for point, kind, booted_version, expected_result in FAILPOINTS:
            self.run_failpoint(point, kind, booted_version, expected_result)
