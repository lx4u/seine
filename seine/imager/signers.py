# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import datetime
import json
import os
import shutil
import struct
import tempfile

from seine import pe_cert
from seine.container import ContainerEngine

# What 'image: secure-boot:' signs on the disk: the boot loader and UKI
# binaries, and the report of who signed what. Part of Imager, which gives
# it its source and scratch helpers.
class BootSigners:
    # ESP/XBOOTLDR are both forced 'vfat', so that type finds every
    # '.efi'. Runs before anchoring, so the parent UKI's entry here is
    # provisional; _anchor_one_uki() fixes it after rewriting the file.
    def _scan_boot_signers(self, g, mounts):
        for m in mounts:
            if m["type"] not in ("vfat", "msdos"):
                continue
            prefix = m["_prefix"].rstrip("/")
            for relpath in sorted(g.find(m["_prefix"])):
                if not relpath.lower().endswith(".efi"):
                    continue
                path = "%s/%s" % (prefix, relpath)
                if not g.is_dir(path):
                    self._record_boot_signer(g, path)

    def _record_boot_signer(self, g, path):
        workdir = tempfile.mkdtemp(dir=self._output_dir, prefix="boot-signer-")
        local = os.path.join(workdir, "check.efi")
        g.download(path, local)
        with open(local, "rb") as f:
            self._boot_signers[path] = pe_cert.extract_signer_cert(f.read())
        shutil.rmtree(workdir, ignore_errors=True)

    # Grouped by signer, so a shared key isn't named once per file.
    # Writes a text summary, a JSON manifest, and each unique cert as
    # its own '.pem', next to the image.
    def _report_boot_signers(self):
        if not self._boot_signers:
            return
        by_fingerprint = {}
        unsigned = []
        for path, cert in self._boot_signers.items():
            if cert is None:
                unsigned.append(path)
                continue
            by_fingerprint.setdefault(
                pe_cert.fingerprint(cert), (cert, []))[1].append(path)

        cert_dir = "%s.boot-signers" % self.source._output
        if by_fingerprint:
            os.makedirs(cert_dir, exist_ok=True)

        lines = ["Boot chain signers:"]
        manifest = {"signers": [], "unsigned": sorted(unsigned)}
        for fingerprint in sorted(by_fingerprint):
            cert, paths = by_fingerprint[fingerprint]
            paths = sorted(paths)
            lines.append("  %s (%s)" % (pe_cert.subject(cert), fingerprint))
            lines += ["    %s" % path for path in paths]

            pem_name = "%s.pem" % fingerprint
            with open(os.path.join(cert_dir, pem_name), "wb") as f:
                f.write(pe_cert.to_pem(cert))
            manifest["signers"].append({
                "fingerprint": fingerprint, "subject": pe_cert.subject(cert),
                "cert": "%s/%s" % (os.path.basename(cert_dir), pem_name),
                "files": paths})
        if unsigned:
            lines.append("  unsigned:")
            lines += ["    %s" % path for path in sorted(unsigned)]

        print("\n".join(lines))
        with open("%s.txt" % cert_dir, "w") as f:
            f.write("\n".join(lines) + "\n")
        with open("%s.json" % cert_dir, "w") as f:
            json.dump(manifest, f, indent=2, sort_keys=True)
            f.write("\n")

    # TimeDateStamp sits 8 bytes into the PE header ('e_lfanew' at 0x3c
    # points to it). CheckSum covers the whole file, so it needs redoing too.
    def _pin_pe_timestamp(self, path, epoch):
        with open(path, "r+b") as f:
            f.seek(0x3c)
            pe_offset = struct.unpack("<I", f.read(4))[0]
            f.seek(pe_offset + 8)
            f.write(struct.pack("<I", epoch))
        self._recompute_pe_checksum(path, pe_offset + 88)

    # Microsoft's CheckSumMappedFile algorithm: sum the file as 32-bit
    # words (the checksum field itself counted as zero), fold overflow
    # back in, then add the file's own length.
    def _recompute_pe_checksum(self, path, checksum_offset):
        with open(path, "rb") as f:
            data = f.read()
        size = len(data)
        if size % 4:
            data += b"\0" * (4 - size % 4)
        total = 0
        for i in range(0, len(data), 4):
            if checksum_offset <= i < checksum_offset + 4:
                continue
            total += struct.unpack_from("<I", data, i)[0]
            if total > 0xffffffff:
                total = (total & 0xffffffff) + (total >> 32)
        total = (total & 0xffff) + (total >> 16)
        total = (total & 0xffff) + (total >> 16)
        total = (total + size) & 0xffffffff
        with open(path, "r+b") as f:
            f.seek(checksum_offset)
            f.write(struct.pack("<I", total))

    # Signs a rebuilt UKI, by vault reference or by mounted host key.
    # Returns the signed file's name within workdir.
    def _sign_uki(self, workdir, epoch):
        secure_boot = self.source.partitionHandler.secure_boot
        key = secure_boot["private-key"]
        if key.startswith("vault:"):
            return self._sign_uki_vault(workdir, epoch, key[len("vault:"):])
        # sbsign stamps its own signing time, ignoring SOURCE_DATE_EPOCH --
        # libfaketime pins what it (and any clock call) sees instead.
        when = datetime.datetime.fromtimestamp(
            epoch, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        faketime = (
            "libfaketime=$(dpkg -L libfaketime | grep -E '/libfaketime\\.so\\.[0-9]+$') && "
            "LD_PRELOAD=$libfaketime FAKETIME='@%s' "
            "sbsign --key /work-key --cert /work-cert "
            "--output signed.efi rebuilt.efi" % when)
        ContainerEngine.run(
            ["container", "run", "--rm", "-v", "%s:/work" % workdir,
             "-v", "%s:/work-key:ro" % os.path.abspath(secure_boot["private-key"]),
             "-v", "%s:/work-cert:ro" % os.path.abspath(secure_boot["public-cert"]),
             "-w", "/work", self._extra_tools.name,
             "sh", "-c", faketime], check=True)
        return "signed.efi"

    # The vault of this build, with this image's own defaults folded in.
    def _vault_provider(self):
        from seine import vault as _vault
        own_defaults = self.source._vault_defaults()
        provider = _vault.for_build(own_defaults)
        # for_build() only seeds a dev instance from whichever caller
        # started it first -- a package build's own vault use, earlier in
        # this same run, usually gets there before the imager does. Fold
        # this image's own defaults in too (same as BuildCmd._vault_lookup).
        defaults = getattr(provider, "_defaults", None)
        if type(defaults) == type({}) and defaults is not own_defaults:
            defaults.update(own_defaults)
        return provider

    # Bytes up, signed PE back; the key never leaves the vault. The
    # timestamp is pinned to the build epoch, not faked.
    def _sign_uki_vault(self, workdir, epoch, name):
        provider = self._vault_provider()
        with open(os.path.join(workdir, "rebuilt.efi"), "rb") as f:
            signed = provider.sbsign_sign(name, f.read(), epoch)
        with open(os.path.join(workdir, "signed.efi"), "wb") as f:
            f.write(signed)
        return "signed.efi"

    # Only what 'image: secure-boot:' covers -- unset means every EFI
    # binary the bootloader installs stays exactly as it shipped, same
    # as an unsigned UKI.
    def _sign_bootloader_files(self, g, bootloader, esp_mount):
        if self.source.partitionHandler.secure_boot is None:
            return
        for path in bootloader.paths_to_sign(esp_mount):
            if g.is_file(path):
                self._sign_pe_in_place(g, path)

    # Reuses _sign_uki() by staging the download under the name it
    # already expects; unlike a UKI, nothing here needs ukify rebuilt.
    def _sign_pe_in_place(self, g, path):
        workdir = tempfile.mkdtemp(dir=self._output_dir, prefix="boot-sign-")
        g.download(path, os.path.join(workdir, "rebuilt.efi"))
        epoch = self.source._epoch()
        result = self._sign_uki(workdir, epoch)
        g.upload(os.path.join(workdir, result), path)
        g.utimens(path, epoch, 0, epoch, 0)
        shutil.rmtree(workdir, ignore_errors=True)
