#!/usr/bin/env python3
# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import avocado
import os
import sys

path_to_self = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine.imager.efibootguard import (
    create_bgenv,
    parse_bgenv,
    EBG_CHAINLOAD_KERNEL,
    BGENV_FILENAME,
)
from seine.imager.gpt import GPT_TYPE_BASIC_DATA
from seine.imager.imager import Imager
from seine.partition import PartitionHandler


class MockGuestfs:
    def __init__(self):
        self.calls = []
        self.files = {}

    def part_add(self, dev, prlogex, start, end):
        self.calls.append(("add", prlogex, start, end))

    def part_set_name(self, dev, idx, name):
        self.calls.append(("name", idx, name))

    def part_set_gpt_guid(self, dev, idx, guid):
        self.calls.append(("guid", idx, guid))

    def part_set_gpt_type(self, dev, idx, gpt_type):
        self.calls.append(("type", idx, gpt_type))

    def mountpoints(self):
        return {}

    def zero_device(self, dev):
        self.calls.append(("zero_device", dev))

    def mkdir_p(self, path):
        self.calls.append(("mkdir_p", path))

    def sh(self, cmd):
        self.calls.append(("sh", cmd))

    def rm(self, path):
        self.calls.append(("rm", path))

    def rm_rf(self, path):
        self.calls.append(("rm_rf", path))

    def mount(self, dev, path):
        self.calls.append(("mount", dev, path))

    def umount(self, path):
        self.calls.append(("umount", path))

    def utimens(self, path, atime_sec, atime_nsec, mtime_sec, mtime_nsec):
        self.calls.append(("utimens", path, mtime_sec))

    def upload(self, local_path, remote_path):
        with open(local_path, "rb") as f:
            self.files.setdefault(remote_path, []).append(f.read())
        self.calls.append(("upload", remote_path))


class EfiBootGuardEnvironmentBlock(avocado.Test):
    def test_create_and_parse_bgenv_amd64(self):
        kernel = EBG_CHAINLOAD_KERNEL["amd64"]
        data = create_bgenv(kernel, revision=1, watchdog=30, ustate=0)
        self.assertEqual(len(data), 132104)

        parsed = parse_bgenv(data)
        self.assertEqual(parsed["kernel"], r"\EFI\systemd\systemd-bootx64.efi")
        self.assertEqual(parsed["args"], "")
        self.assertEqual(parsed["revision"], 1)
        self.assertEqual(parsed["watchdog"], 30)
        self.assertEqual(parsed["ustate"], 0)
        self.assertEqual(parsed["in_progress"], 0)

    def test_create_and_parse_bgenv_arm64(self):
        kernel = EBG_CHAINLOAD_KERNEL["arm64"]
        data = create_bgenv(kernel, revision=2, watchdog=60, ustate=0)
        self.assertEqual(len(data), 132104)

        parsed = parse_bgenv(data)
        self.assertEqual(parsed["kernel"], r"\EFI\systemd\systemd-bootaa64.efi")
        self.assertEqual(parsed["revision"], 2)
        self.assertEqual(parsed["watchdog"], 60)
        self.assertEqual(parsed["ustate"], 0)

    def test_crc32_verification_detects_tampered_payload(self):
        data = bytearray(create_bgenv(r"\EFI\systemd\systemd-bootx64.efi", 1, 30))
        # Tamper with a byte in the payload
        data[10] ^= 0xFF
        with self.assertRaises(ValueError) as cm:
            parse_bgenv(bytes(data))
        self.assertIn("CRC32 mismatch", str(cm.exception))

    def test_invalid_bgenv_size_is_refused(self):
        with self.assertRaises(ValueError) as cm:
            parse_bgenv(b"too-short")
        self.assertIn("invalid BGENV.DAT size", str(cm.exception))

    def test_string_overflow_is_refused(self):
        long_str = "x" * 256
        with self.assertRaises(ValueError) as cm:
            create_bgenv(long_str, 1, 30)
        self.assertIn("string exceeds maximum length", str(cm.exception))


class EfiBootGuardImager(avocado.Test):
    def test_gpt_layout_assigns_basic_data_type_guid_to_bgenv_partitions(self):
        imager = Imager.__new__(Imager)
        imager.source = type("Source", (), {"spec": {"imager": {}}})()
        imager._identity_seed = lambda: "test-seed"
        g = MockGuestfs()

        part1 = {
            "label": "BGENV1",
            "_start_mib": 1,
            "_end_mib": 17,
            "flags": ["bgenv"],
        }
        imager._partition_device(g, "gpt", part1, 1, "amd64", {})
        self.assertIn(("type", 1, GPT_TYPE_BASIC_DATA), g.calls)

        part2 = {
            "label": "bgenv2",
            "_start_mib": 17,
            "_end_mib": 33,
        }
        imager._partition_device(g, "gpt", part2, 2, "amd64", {})
        self.assertIn(("type", 2, GPT_TYPE_BASIC_DATA), g.calls)

    def test_initialize_bgenv_writes_dual_partitions(self):
        imager = Imager.__new__(Imager)
        spec = {
            "distribution": {"architecture": "amd64"},
            "imager": {},
        }
        imager.source = type("Source", (), {
            "spec": spec,
            "_epoch": lambda *args: 1700000000,
        })()
        imager.reproducible = True
        imager._output_dir = None

        ph = PartitionHandler()
        ph.parse({
            "image": {
                "filename": "disk.img",
                "table": "gpt",
                "watchdog": "30s",
                "partitions": [
                    {"label": "esp", "where": "/efi", "type": "vfat", "size": "64MiB"},
                    {"label": "root", "where": "/"},
                ],
            },
        })

        bgenv_parts = [p for p in ph.partitions if ph._is_bgenv(p)]
        self.assertEqual(len(bgenv_parts), 2)

        part_devices = {
            id(bgenv_parts[0]): "/dev/sda3",
            id(bgenv_parts[1]): "/dev/sda4",
        }

        g = MockGuestfs()
        imager._initialize_bgenv(g, ph, part_devices)

        mount_calls = [c for c in g.calls if c[0] == "mount"]
        self.assertEqual(mount_calls, [("mount", "/dev/sda3", "/"), ("mount", "/dev/sda4", "/")])

        umount_calls = [c for c in g.calls if c[0] == "umount"]
        self.assertEqual(umount_calls, [("umount", "/"), ("umount", "/")])

        utimens_calls = [c for c in g.calls if c[0] == "utimens"]
        self.assertEqual(utimens_calls, [
            ("utimens", f"/{BGENV_FILENAME}", 1700000000),
            ("utimens", f"/{BGENV_FILENAME}", 1700000000),
        ])

        uploaded = g.files[f"/{BGENV_FILENAME}"]
        self.assertEqual(len(uploaded), 2)
        env1 = parse_bgenv(uploaded[0])
        self.assertEqual(env1["revision"], 1)
        self.assertEqual(env1["watchdog"], 30)
        self.assertEqual(env1["kernel"], r"\EFI\systemd\systemd-bootx64.efi")

        env2 = parse_bgenv(uploaded[1])
        self.assertEqual(env2["revision"], 2)
        self.assertEqual(env2["watchdog"], 30)
        self.assertEqual(env2["kernel"], r"\EFI\systemd\systemd-bootx64.efi")

    def test_initialize_bgenv_with_extra_tools_reproducible(self):
        imager = Imager.__new__(Imager)
        spec = {
            "distribution": {"architecture": "amd64"},
            "imager": {},
        }
        imager.source = type("Source", (), {
            "spec": spec,
            "_epoch": lambda *args: 1700000000,
        })()
        imager.reproducible = True
        imager._output_dir = None
        imager._extra_tools_files = ["/usr/bin/mformat", "/usr/bin/mcopy"]
        imager._upload_tools = lambda g, path, files: path
        imager._fat_serial = lambda label, dev: "12345678"

        ph = PartitionHandler()
        ph.parse({
            "image": {
                "filename": "disk.img",
                "table": "gpt",
                "watchdog": "30s",
                "partitions": [
                    {"label": "esp", "where": "/efi", "type": "vfat", "size": "64MiB"},
                    {"label": "root", "where": "/"},
                ],
            },
        })

        bgenv_parts = [p for p in ph.partitions if ph._is_bgenv(p)]
        part_devices = {
            id(bgenv_parts[0]): "/dev/sda3",
            id(bgenv_parts[1]): "/dev/sda4",
        }

        g = MockGuestfs()
        imager._initialize_bgenv(g, ph, part_devices)

        mount_calls = [c for c in g.calls if c[0] == "mount"]
        self.assertEqual(mount_calls, [])

        sh_calls = [c[1] for c in g.calls if c[0] == "sh"]
        self.assertTrue(any("mformat -i /dev/sda3" in c for c in sh_calls))
        self.assertTrue(any("mcopy -Q -i /dev/sda3" in c for c in sh_calls))
        self.assertTrue(any("mformat -i /dev/sda4" in c for c in sh_calls))
        self.assertTrue(any("mcopy -Q -i /dev/sda4" in c for c in sh_calls))

    def test_initialize_bgenv_unsupported_arch_raises(self):
        imager = Imager.__new__(Imager)
        spec = {
            "distribution": {"architecture": "mips"},
            "imager": {},
        }
        imager.source = type("Source", (), {"spec": spec, "_epoch": lambda *args: 0})()
        imager.reproducible = False
        imager._output_dir = None

        ph = PartitionHandler()
        ph.parse({
            "image": {
                "filename": "disk.img",
                "table": "gpt",
                "watchdog": "30s",
                "partitions": [
                    {"label": "esp", "where": "/efi", "type": "vfat", "size": "64MiB"},
                    {"label": "root", "where": "/"},
                ],
            },
        })
        bgenv_parts = [p for p in ph.partitions if ph._is_bgenv(p)]
        part_devices = {id(p): f"/dev/sda{i}" for i, p in enumerate(bgenv_parts, 3)}

        g = MockGuestfs()
        with self.assertRaises(NotImplementedError) as cm:
            imager._initialize_bgenv(g, ph, part_devices)
        self.assertIn("not implemented for architecture 'mips'", str(cm.exception))
