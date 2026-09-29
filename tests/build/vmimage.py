#!/usr/bin/env python3
# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import avocado
import hashlib
import os
import shutil
import subprocess
import sys
import tarfile

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine import vmimage

class FakeImage:
    def __init__(self, output):
        self._output = output

    def _image_digest(self):
        return "disk-digest"

def refused(test, spec):
    with test.assertRaises(ValueError) as error:
        vmimage.VmImages(FakeImage("/deploy/pc-image.img")).parse(spec)
    return str(error.exception)

MINIMAL_SPEC = {
    "image": {"filename": "pc-image.img"},
    "vms": {"formats": {"vmware": {"type": "vmdk"}}},
}

class Parsing(avocado.Test):
    def test_needs_an_image_section(self):
        self.assertIn("needs an 'image' section", refused(
            self, {"vms": MINIMAL_SPEC["vms"]}))

    def test_formats_must_be_given_and_non_empty(self):
        self.assertIn("non-empty dictionary", refused(
            self, {"image": {}, "vms": {}}))
        self.assertIn("non-empty dictionary", refused(
            self, {"image": {}, "vms": {"formats": {}}}))

    def test_unknown_top_level_setting(self):
        self.assertIn("no 'bogus' setting", refused(
            self, {"image": {}, "vms": {"bogus": {}}}))

    def test_type_is_required_and_checked(self):
        self.assertIn("shall be one of", refused(
            self, {"image": {}, "vms": {"formats": {"a": {}}}}))
        self.assertIn("shall be one of", refused(
            self, {"image": {}, "vms": {"formats": {"a": {"type": "floppy"}}}}))

    def test_unknown_format_setting(self):
        self.assertIn("no 'bogus' setting", refused(self, {
            "image": {}, "vms": {"formats": {
                "a": {"type": "vmdk", "bogus": True}}}}))

    def test_scalar_types_are_checked(self):
        self.assertIn("true or false", refused(self, {
            "image": {}, "vms": {"formats": {
                "a": {"type": "vmdk", "tpm": "yes"}}}}))
        self.assertIn("positive integer", refused(self, {
            "image": {}, "vms": {"formats": {
                "a": {"type": "vmdk", "cpus": 0}}}}))
        self.assertIn("'uefi' or 'bios'", refused(self, {
            "image": {}, "vms": {"formats": {
                "a": {"type": "vmdk", "firmware": "coreboot"}}}}))

    def test_defaults_apply_and_a_format_can_override_them(self):
        images = vmimage.VmImages(FakeImage("/deploy/pc-image.img"))
        images.parse({
            "image": {},
            "vms": {
                "defaults": {"cpus": 4, "memory": 4096},
                "formats": {
                    "vmware": {"type": "vmdk"},
                    "hyperv": {"type": "vhdx", "cpus": 8},
                },
            },
        })
        by_name = {fmt.name: fmt for fmt in images.formats}
        self.assertEqual(by_name["vmware"].cpus, 4)
        self.assertEqual(by_name["hyperv"].cpus, 8)
        self.assertEqual(by_name["hyperv"].memory, 4096)
        # Defaults not given: seine's own built-in ones apply.
        self.assertTrue(by_name["vmware"].tpm)
        self.assertTrue(by_name["vmware"].serial)
        self.assertEqual(by_name["vmware"].firmware, "uefi")

    def test_options_pass_through(self):
        images = vmimage.VmImages(FakeImage("/deploy/pc-image.img"))
        images.parse({
            "image": {},
            "vms": {"formats": {"a": {
                "type": "vmdk", "options": {"adapter_type": "lsilogic"}}}},
        })
        self.assertEqual(images.formats[0].options, {"adapter_type": "lsilogic"})

class OutputNaming(avocado.Test):
    def test_disk_output_is_suffixed_by_format_name(self):
        images = vmimage.VmImages(FakeImage("/deploy/pc-image.img"))
        images.parse({
            "image": {},
            "vms": {"formats": {
                "vmware": {"type": "vmdk"},
                "vmware-thick": {"type": "vmdk", "thin-provisioning": False},
            }},
        })
        outputs = {images._disk_output(fmt) for fmt in images.formats}
        self.assertEqual(outputs, {
            "/deploy/pc-image-vmware.vmdk",
            "/deploy/pc-image-vmware-thick.vmdk",
        })

    def test_descriptor_output_is_none_for_types_without_a_generator(self):
        # Only 'vhdx' pairs with a descriptor -- 'ova' is already a full
        # package, the rest are bare disks for attaching by hand.
        for vtype in ("qcow2", "vmdk", "vdi", "ova"):
            images = vmimage.VmImages(FakeImage("/deploy/pc-image.img"))
            images.parse({"image": {}, "vms": {"formats": {"a": {"type": vtype}}}})
            self.assertIsNone(images._descriptor_output(images.formats[0]), vtype)

    def test_ova_output_uses_the_ova_extension(self):
        images = vmimage.VmImages(FakeImage("/deploy/pc-image.img"))
        images.parse({"image": {}, "vms": {"formats": {"vmware": {"type": "ova"}}}})
        self.assertEqual(images._disk_output(images.formats[0]),
                         "/deploy/pc-image-vmware.ova")

class ThinProvisioning(avocado.Test):
    def test_thin_and_thick_pick_different_qemu_img_options(self):
        images = vmimage.VmImages(FakeImage("/deploy/pc-image.img"))
        images.parse({"image": {}, "vms": {"formats": {
            "thin": {"type": "vmdk", "thin-provisioning": True},
            "thick": {"type": "vmdk", "thin-provisioning": False},
        }}})
        by_name = {fmt.name: fmt for fmt in images.formats}
        self.assertEqual(images._convert_options(by_name["thin"]),
                         {"subformat": "streamOptimized"})
        self.assertEqual(images._convert_options(by_name["thick"]),
                         {"subformat": "monolithicFlat"})

    def test_an_explicit_option_wins_over_the_automatic_one(self):
        images = vmimage.VmImages(FakeImage("/deploy/pc-image.img"))
        images.parse({"image": {}, "vms": {"formats": {"a": {
            "type": "vmdk", "options": {"subformat": "twoGbMaxExtentSparse"}}}}})
        self.assertEqual(images._convert_options(images.formats[0]),
                         {"subformat": "twoGbMaxExtentSparse"})

class Descriptors(avocado.Test):
    def _format(self, **overrides):
        images = vmimage.VmImages(FakeImage("/deploy/pc-image.img"))
        settings = {"type": "vhdx"}
        settings.update(overrides)
        images.parse({"image": {}, "vms": {"formats": {"hyperv": settings}}})
        return images.formats[0]

    def test_hyperv_script_reflects_firmware_tpm_and_serial(self):
        text = vmimage._render_hyperv(self._format(), "pc-image-hyperv.vhdx")
        self.assertIn("-EnableSecureBoot On", text)
        self.assertIn("Enable-VMTPM", text)
        self.assertIn("Set-VMComPort", text)
        self.assertIn('-VHDPath "pc-image-hyperv.vhdx"', text)

    def test_hyperv_script_omits_disabled_settings(self):
        text = vmimage._render_hyperv(
            self._format(firmware="bios", tpm=False, serial=False), "d.vhdx")
        self.assertIn("-EnableSecureBoot Off", text)
        self.assertNotIn("VMTPM", text)
        self.assertNotIn("VMComPort", text)

# Exercises the real 'qemu-img convert' binary end to end: cheap (a tiny
# sparse raw file), so run whenever it's installed rather than gating it
# behind a 'container'/'kvm' tag like the tests that boot something.
class Conversion(avocado.Test):
    def setUp(self):
        if shutil.which("qemu-img") is None:
            self.cancel("qemu-img is needed to convert a disk image")
        self.raw = os.path.join(self.workdir, "pc-image.img")
        subprocess.run(["qemu-img", "create", "-f", "raw", self.raw, "4M"],
                       check=True, capture_output=True)

    def test_converts_and_is_a_cache_hit_on_a_second_run(self):
        images = vmimage.VmImages(FakeImage(self.raw))
        images.parse({"image": {}, "vms": {"formats": {
            "vmware": {"type": "vmdk"}}}})
        fmt = images.formats[0]

        images._convert(fmt)
        output = images._disk_output(fmt)
        self.assertTrue(os.path.isfile(output))
        info = subprocess.run(["qemu-img", "info", "--output=json", output],
                              check=True, capture_output=True, text=True).stdout
        self.assertIn('"format": "vmdk"', info)

        written = os.path.getmtime(output)
        images._convert(fmt)
        self.assertEqual(os.path.getmtime(output), written,
                         "a second run with nothing changed must not reconvert")

    def test_builds_an_ova_vmware_and_virtualbox_can_both_import(self):
        images = vmimage.VmImages(FakeImage(self.raw))
        images.parse({"image": {}, "vms": {"formats": {
            "vmware": {"type": "ova", "cpus": 4, "memory": 4096}}}})
        fmt = images.formats[0]

        images._build(fmt)
        output = images._disk_output(fmt)
        self.assertTrue(tarfile.is_tarfile(output))

        with tarfile.open(output) as tar:
            names = tar.getnames()
            # Descriptor, then manifest, then disk -- the order every
            # OVF-importing tool expects.
            self.assertEqual(names, ["vmware.ovf", "vmware.mf", "vmware-disk1.vmdk"])
            ovf = tar.extractfile("vmware.ovf").read().decode()
            self.assertIn('vmw:value="efi"', ovf)
            self.assertIn("<rasd:VirtualQuantity>4</rasd:VirtualQuantity>", ovf)
            self.assertIn("<rasd:VirtualQuantity>4096</rasd:VirtualQuantity>", ovf)

            disk_bytes = tar.extractfile("vmware-disk1.vmdk").read()
            digest = hashlib.sha256(disk_bytes).hexdigest()
            mf = tar.extractfile("vmware.mf").read().decode()
            self.assertIn(f"SHA256(vmware-disk1.vmdk)= {digest}", mf)

        written = os.path.getmtime(output)
        images._build(fmt)
        self.assertEqual(os.path.getmtime(output), written,
                         "a second run with nothing changed must not rebuild the OVA")
