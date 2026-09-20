#!/usr/bin/env python3
# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import avocado
import datetime
import json
import os
import sys

from unittest import mock

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from seine import pe_cert
from seine.imager import Imager

def _self_signed(common_name):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.datetime.now(datetime.timezone.utc)
    return (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now)
            .not_valid_after(now + datetime.timedelta(days=1))
            .sign(key, hashes.SHA256()))

class BootSignerRecap(avocado.Test):
    def _imager(self):
        imager = Imager.__new__(Imager)
        imager.source = mock.Mock()
        imager.source._output = os.path.join(self.workdir, "disk.img")
        imager._output_dir = self.workdir
        imager._boot_signers = {}
        return imager

    def test_scan_finds_efi_only_under_fat_mounts(self):
        imager = self._imager()
        mounts = [{"type": "vfat", "_prefix": "/boot/efi"},
                  {"type": "ext4", "_prefix": "/"}]
        g = mock.Mock()
        def fake_find(prefix):
            if prefix != "/boot/efi":
                self.fail("scanned a non-fat mount")
            return ["EFI", "EFI/Linux", "EFI/Linux/foo.efi", "EFI/Linux/readme.txt"]
        g.find.side_effect = fake_find
        g.is_dir.return_value = False
        with mock.patch.object(Imager, "_record_boot_signer") as record:
            imager._scan_boot_signers(g, mounts)
        record.assert_called_once_with(g, "/boot/efi/EFI/Linux/foo.efi")

    def test_record_stores_none_for_an_unsigned_file(self):
        imager = self._imager()
        g = mock.Mock()
        def fake_download(path, local):
            with open(local, "wb") as f:
                f.write(b"not-a-pe")
        g.download.side_effect = fake_download
        path = "/boot/efi/EFI/BOOT/BOOTX64.EFI"
        imager._record_boot_signer(g, path)
        self.assertIsNone(imager._boot_signers[path])

    def test_report_groups_by_signer_and_writes_next_to_the_image(self):
        imager = self._imager()
        cert = _self_signed("signer-a")
        fingerprint = pe_cert.fingerprint(cert)
        imager._boot_signers = {
            "/boot/efi/EFI/Linux/parent.efi": cert,
            "/boot/efi/EFI/Linux/parent.efi.extra.d/addon.addon.efi": cert,
            "/boot/efi/EFI/BOOT/BOOTX64.EFI": None,
        }
        imager._report_boot_signers()
        base = imager.source._output

        with open(base + ".boot-signers.txt") as f:
            text = f.read()
        self.assertIn("parent.efi", text)
        self.assertIn("addon.addon.efi", text)
        self.assertIn("unsigned:", text)
        self.assertIn("BOOTX64.EFI", text)
        # Grouped: the signer shared by both files is named once, not twice.
        self.assertEqual(text.count("signer-a"), 1)

        with open(base + ".boot-signers.json") as f:
            manifest = json.load(f)
        self.assertEqual(manifest["unsigned"], ["/boot/efi/EFI/BOOT/BOOTX64.EFI"])
        self.assertEqual(len(manifest["signers"]), 1)
        signer = manifest["signers"][0]
        self.assertEqual(signer["fingerprint"], fingerprint)
        self.assertEqual(sorted(signer["files"]), [
            "/boot/efi/EFI/Linux/parent.efi",
            "/boot/efi/EFI/Linux/parent.efi.extra.d/addon.addon.efi"])

        pem_path = os.path.join(base + ".boot-signers", "%s.pem" % fingerprint)
        with open(pem_path, "rb") as f:
            self.assertEqual(x509.load_pem_x509_certificate(f.read()), cert)

    def test_report_writes_nothing_when_the_disk_has_no_efi(self):
        imager = self._imager()
        imager._report_boot_signers()
        base = imager.source._output
        self.assertFalse(os.path.exists(base + ".boot-signers.txt"))
        self.assertFalse(os.path.exists(base + ".boot-signers.json"))
        self.assertFalse(os.path.exists(base + ".boot-signers"))

if __name__ == "__main__":
    avocado.main()
