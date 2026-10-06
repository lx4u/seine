# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import hashlib
import os
import tempfile

from seine import pe_cert
from seine.container import ContainerEngine
from seine.extends.uki import ukify_argv
from seine.imager.appliance import DEVICE
from seine.imager.appliance import SCRATCH_DEVICE
from seine.imager.rebuild import EXT_FSTYPES
from seine.packages import FALLBACK_EPOCH
from seine.partition import RO_FSTYPES

# The read-only side of a disk: squashfs and erofs images with their dm-verity
# hash trees, and the UKI that anchors a root hash on its command line.
# Part of Imager, which gives it its source, scratch and signing helpers.
class UkiAnchor:
    # Runs before 'hp's partition is written -- the root hash decides both
    # GUIDs. squashfs (unlike erofs) doesn't pad to veritysetup's 4096-byte
    # blocks, so pad here.
    def _build_verity(self, g, tools_dir, work_dir, m, scratch, hp, part_index):
        print("Building verity hash tree for '%s'..." % m["label"])
        block_size = 4096
        size = g.filesize(scratch)
        aligned = -(-size // block_size) * block_size
        if aligned != size:
            g.truncate_size(scratch, aligned)

        run = "LD_LIBRARY_PATH=%s %s/veritysetup" % (tools_dir, tools_dir)
        hash_scratch = "%s/%s.verity.img" % (work_dir, hp["label"])
        roothash_file = "%s/%s.roothash" % (work_dir, hp["label"])
        # Without these, verity picks a random salt and UUID each build,
        # so even identical content gets a different root hash every time.
        salt = hashlib.sha256(
            ("verity-salt:%s" % self._uuid_for("verity-salt", m["label"])).encode()
        ).hexdigest()
        g.sh("%s format --root-hash-file=%s --salt=%s --uuid=%s %s %s"
             % (run, roothash_file, salt, self._uuid_for("verity-uuid", m["label"]),
                scratch, hash_scratch))
        roothash = g.read_file(roothash_file).decode().strip()
        g.rm(roothash_file)
        if len(roothash) != 64:
            raise RuntimeError(
                "'veritysetup format' for '%s' produced a %d-hex-digit root "
                "hash, expected 64 (sha256) -- only the default hash "
                "algorithm is supported" % (m["label"], len(roothash)))

        data_guid = self._hex_to_gpt_guid(roothash[0:32])
        hash_guid = self._hex_to_gpt_guid(roothash[32:64])
        g.part_set_gpt_guid(DEVICE, part_index[id(m)], data_guid)
        g.part_set_gpt_guid(DEVICE, part_index[id(hp)], hash_guid)

        built = g.filesize(hash_scratch)
        cap = (hp["_end_mib"] - hp["_start_mib"]) * 1024 * 1024
        if built > cap:
            raise RuntimeError(
                "built verity hash tree for '%s' is %d bytes, larger than "
                "its %d-byte partition '%s'"
                % (m["label"], built, cap, hp["label"]))
        g.copy_file_to_device(
            hash_scratch, DEVICE, destoffset=hp["_start_mib"] * 1024 * 1024)
        g.rm(hash_scratch)
        return roothash

    # gpt-auto-generator refuses an unanchored verity pair, so 'usrhash='
    # must be added for '/usr' to mount. Signed only if 'image: secure-boot:'
    # is set. Runs while 'm' and siblings are still mounted.
    def _anchor_uki(self, g, mounts, mounted, m, roothash):
        if m["_prefix"] != "/usr/":
            return
        efi_dirs = []
        for other in mounts:
            if id(other) not in mounted:
                continue
            efi_dir = "%s/EFI/Linux" % other["_prefix"].rstrip("/")
            if g.is_dir(efi_dir):
                efi_dirs.append(efi_dir)

        for efi_dir in efi_dirs:
            for name in sorted(g.ls(efi_dir)):
                if name.endswith(".efi"):
                    self._anchor_one_uki(g, efi_dir, name, roothash)

    # Downloads the '.efi', rebuilds it with 'usrhash=' and uploads it back.
    def _anchor_one_uki(self, g, efi_dir, name, roothash):
        print("Anchoring '%s' with usrhash=%s..." % (name, roothash))
        efi_path = "%s/%s" % (efi_dir, name)
        workdir = tempfile.mkdtemp(dir=self._output_dir, prefix="uki-anchor-")
        original = os.path.join(workdir, "original.efi")
        g.download(efi_path, original)
        result = self._rebuild_uki(workdir, original, "usrhash=%s" % roothash)
        self._upload_uki(g, workdir, result, efi_path)

    # objcopy/ukify/sbsign run as container commands, not inside the
    # appliance. Appends 'extra' to the command line of 'original' (a file
    # of 'workdir'), returns the rebuilt, possibly signed file's name there.
    # 'osrel' (text) replaces the os-release of 'original'.
    # Runs a command of the tools container. 'volumes' maps host
    # directories to where the command sees them (with ':ro' if wanted).
    def _run_tool(self, args, volumes, epoch=None, workdir=None, check=True):
        argv = ["container", "run", "--rm"]
        for host, guest in volumes.items():
            argv += ["-v", "%s:%s" % (host, guest)]
        if epoch is not None:
            argv += ["-e", "SOURCE_DATE_EPOCH=%d" % epoch]
        if workdir:
            argv += ["-w", workdir]
        ContainerEngine.run(argv + [self._extra_tools.name] + args, check=check)

    def _rebuild_uki(self, workdir, original, extra, osrel=None):
        original = os.path.relpath(original, workdir)
        # ukify stamps the PE header's build time from this unless told
        # otherwise, which would make the .efi differ build to build.
        epoch = self.source._epoch()

        def run(args, check=True):
            self._run_tool(args, {workdir: "/work"}, epoch=epoch,
                           workdir="/work", check=check)

        # One objcopy dumps all three sections byte-identical to build time.
        # '/dev/null' as output makes objcopy exit 1 despite success, so use
        # a throwaway file instead.
        run(["objcopy",
             "--dump-section", ".linux=linux.bin",
             "--dump-section", ".initrd=initrd.bin",
             "--dump-section", ".cmdline=cmdline.txt",
             original, "discard.efi"])

        # A UKI without '.osrel' is fine: objcopy fails and we keep none.
        osrel_path = os.path.join(workdir, "osrel.txt")
        if osrel is not None:
            with open(osrel_path, "w") as f:
                f.write(osrel)
        else:
            run(["objcopy", "--dump-section", ".osrel=osrel.txt",
                 original, "discard.efi"], check=False)
        ukify_extra = (("--os-release=@osrel.txt",)
                       if os.path.exists(osrel_path) else ())

        base_cmdline = open(os.path.join(workdir, "cmdline.txt")).read().strip()
        cmdline = ("%s %s" % (base_cmdline, extra)) if base_cmdline else extra

        # A real 'ukify build', not an 'objcopy --update-section' patch, so
        # a future PCR-policy pass only extends 'extra' here instead of a
        # second code path.
        run(ukify_argv("linux.bin", "initrd.bin", cmdline, "rebuilt.efi",
                         extra=ukify_extra))
        # ukify stamps the PE header with the real build time regardless
        # of SOURCE_DATE_EPOCH -- pin it directly, before signing folds
        # it into the signature.
        self._pin_pe_timestamp(os.path.join(workdir, "rebuilt.efi"), epoch)

        if self.source.partitionHandler.secure_boot is not None:
            return self._sign_uki(workdir, epoch)
        return "rebuilt.efi"

    def _upload_uki(self, g, workdir, result, efi_path):
        epoch = self.source._epoch()
        g.upload(os.path.join(workdir, result), efi_path)
        # g.upload() stamps the real time, unlike the mtools rebuild
        # the rest of this FAT tree already went through.
        g.utimens(efi_path, epoch, 0, epoch, 0)

        # The cmdline change invalidates whatever _scan_boot_signers()
        # recorded before rebuilding -- replace it with the truth.
        with open(os.path.join(workdir, result), "rb") as f:
            self._boot_signers[efi_path] = pe_cert.extract_signer_cert(f.read())

    # Deepest mount first, so a mount's children are already unmounted
    # (empty dir, not live content) when read. 'mounts' is one group's
    # list -- create() handles groups one at a time.
    def _build_ro_images(self, g, mounts, mount_devices, part_index, hash_part_for):
        built_sizes = {}
        ro_mounts = [m for m in mounts if m["type"] in RO_FSTYPES]
        if not ro_mounts:
            return built_sizes

        mounted = {id(m) for m in mounts}
        # A rebuilt ext2/3/4 root goes read-only by now -- use '/mnt'
        # instead, a normal FHS directory that survives root's rebuild
        # (SCRATCH_MOUNT itself does not).
        root_m = next((mnt for mnt in mounts if mnt["_prefix"] == "/"), None)
        root_ro = root_m is not None and root_m["type"] in EXT_FSTYPES
        work_dir = ""
        if root_ro:
            work_dir = "/mnt"
            g.mount(SCRATCH_DEVICE, work_dir)
        tools_dir = self._upload_tools(
            g, "%s/.imager-extra-tools" % work_dir, self._extra_tools_files)

        for m in sorted(ro_mounts, key=lambda m: m["_depth"], reverse=True):
            print("Building %s image for '%s'..." % (m["type"], m["label"]))
            for child in mounts:
                if child is not m and id(child) in mounted \
                   and child["_prefix"].startswith(m["_prefix"]):
                    g.umount(child["_prefix"])
                    mounted.discard(id(child))

            tool = "%s/%s" % (tools_dir,
                "mksquashfs" if m["type"] == "squashfs" else "mkfs.erofs")
            run = "LD_LIBRARY_PATH=%s %s" % (tools_dir, tool)

            scratch = "%s/%s.img" % (work_dir, m["label"])
            comp = m.get("compression")
            # Fixed timestamp, not real mtimes: same content must give the
            # same bytes (and verity hash) across rebuilds.
            if m["type"] == "squashfs":
                # '-processors 1': parallel compression can place blocks
                # and fragments in a different order every run, even for
                # identical content.
                g.sh("%s %s %s -noappend -all-time %d -processors 1%s" % (
                    run, m["_prefix"], scratch, FALLBACK_EPOCH,
                    " -comp %s" % comp if comp else ""))
            else:
                # mkfs.erofs takes output before source, the reverse of
                # mksquashfs. '-U' pins the volume UUID -- otherwise
                # random, so /usr's own bytes would never repeat.
                g.sh("%s%s -T%d -U %s %s %s" % (
                    run, " -z%s" % comp if comp else "", FALLBACK_EPOCH,
                    self._uuid_for("erofs", m["label"]), scratch, m["_prefix"]))

            built = g.filesize(scratch)
            built_sizes[id(m)] = built

            # Still while 'm' is mounted: g.sh() chroots via '/bin/sh' at
            # '/', a symlink into '/usr' on usrmerged targets -- unmount
            # first and g.sh() has nothing to run.
            hp = hash_part_for.get(m["label"])
            if hp is not None:
                roothash = self._build_verity(
                    g, tools_dir, work_dir, m, scratch, hp, part_index)
                self._anchor_uki(g, mounts, mounted, m, roothash)

            g.umount(m["_prefix"])
            mounted.discard(id(m))

            if m["_lvm"]:
                dest, offset, cap = mount_devices[id(m)], 0, m["size"]
            else:
                dest, offset = DEVICE, m["_start_mib"] * 1024 * 1024
                cap = (m["_end_mib"] - m["_start_mib"]) * 1024 * 1024
            if built > cap:
                raise RuntimeError(
                    "built %s image for '%s' is %d bytes, larger than its "
                    "%d-byte partition/volume" % (m["type"], m["label"], built, cap))

            # The old ext4 staging filesystem's superblock and journal
            # still sit past 'built', with real timestamps. Pad first so
            # the copy below overwrites the whole partition/volume.
            if built < cap:
                g.truncate_size(scratch, cap)

            g.copy_file_to_device(scratch, dest, destoffset=offset)
            g.rm(scratch)

        g.rm_rf(tools_dir)
        if root_ro:
            g.umount(work_dir)
        return built_sizes
