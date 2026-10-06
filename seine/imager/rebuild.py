# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import os
import shlex
import struct
import tempfile

from seine.imager.appliance import SCRATCH_DEVICE
from seine.partition import RO_FSTYPES

# An ext or FAT rebuild needs room for a content copy plus the new
# image at once, more than any partition has spare. A throwaway second
# disk (SCRATCH_DEVICE) gives that room without touching real partitions;
# it is mounted here, or already holds the root of an ostree build.
SCRATCH_MOUNT = "/.ext-scratch-disk"

# The only types _normalize_ext_mount() (mke2fs/debugfs based) knows how
# to capture and rebuild byte-reproducibly. Adding another (btrfs, xfs,
# f2fs, ...) needs its own rebuild logic first, not just a wider tuple.
EXT_FSTYPES = ("ext2", "ext3", "ext4")

# What '--reproducible' does to the partitions an Imager has filled: every
# file time reset to the build epoch, and each ext and FAT file system
# rebuilt so that no creation time or on-disk layout depends on when, or
# in which order, it was written. Part of Imager; it relies on its
# source, options and uuid helpers.
class PartitionRebuild:
    # What _normalize_fat_tree() writes later, set now so a boot loader
    # installed in between (grub embeds the serial of its ESP) names the
    # volume the image will have, not the random one mkfs picked.
    def _pin_fat_serial(self, g, dev, serial):
        # The volume ID sits at 0x43 on FAT32 and at 0x27 on FAT12/16.
        offset = 0x43 if g.pread_device(dev, 8, 0x52) == b"FAT32   " else 0x27
        g.pwrite_device(dev, struct.pack("<I", int(serial, 16)), offset)

    # Where the guest has this mount: its own prefix, unless an ostree
    # build mounted it under /sysroot.
    def _where(self, m):
        return self._guest_paths.get(id(m), m["_prefix"])

    # g.write() cannot carry more than the protocol's message size.
    def _upload_bytes(self, g, data, path):
        with tempfile.NamedTemporaryFile(dir=self._output_dir) as f:
            f.write(data)
            f.flush()
            g.upload(f.name, path)

    # Files the imager writes directly (fstab, grub.cfg, EFI binaries)
    # get a real 'now' timestamp. Reset any such file back to a fixed
    # time, so two builds of the same spec match.
    def _normalize_mount_timestamps(self, g, mounts, mount_devices):
        epoch = self.source._epoch()
        started = int(self.source._started)
        # ext2/3/4 has no syscall for ctime/crtime either, so it's
        # rebuilt with mke2fs -d, same idea as FAT above. Deepest first:
        # root goes last, once its children are already rebuilt.
        ext_mounts = [m for m in mounts if m["type"] in EXT_FSTYPES]
        fat_mounts = [m for m in mounts if m["type"] in ("vfat", "msdos")]
        # FAT staging needs a full copy of the partition, which does not
        # fit in root's own auto-sized slack -- use the scratch disk,
        # mounted only when create() actually added one.
        scratch_mounted = False
        if self.reproducible and (ext_mounts or fat_mounts):
            # Unmounted again by this source's own g.umount_all(), same as
            # every other mount here -- freshly (re)mounted per source.
            g.mkdir_p(SCRATCH_MOUNT)
            # An ostree build has it mounted at '/' already (the stage).
            if SCRATCH_DEVICE not in g.mountpoints():
                g.mount(SCRATCH_DEVICE, SCRATCH_MOUNT)
            scratch_mounted = True
        staging = SCRATCH_MOUNT if scratch_mounted else ""
        for m in mounts:
            if m["type"] in RO_FSTYPES:
                continue
            if self.verbose:
                print("  normalizing timestamps under '%s'..." % self._where(m))
            # '-xdev': skip /proc and /sys, mounted here too but not
            # part of the disk image.
            g.sh("find %s -xdev -newermt '@%d' -exec touch --no-dereference "
                 "--date=@%d {} +" % (self._where(m), started, epoch))
            if self.reproducible and m["type"] in ("vfat", "msdos"):
                self._normalize_fat_tree(g, m, mount_devices[id(m)], staging=staging)
        if scratch_mounted and not ext_mounts \
                and g.mountpoints().get(SCRATCH_DEVICE) == SCRATCH_MOUNT:
            g.umount(SCRATCH_MOUNT)
        if self.reproducible:
            for m in sorted(ext_mounts, key=lambda m: m["_depth"], reverse=True):
                self._normalize_ext_mount(g, m, mounts, mount_devices)

    def _normalize_ext_mount(self, g, m, mounts, mount_devices):
        dev = mount_devices[id(m)]
        prefix = self._where(m)
        epoch = self.source._epoch()
        print("Rebuilding %s file-system for '%s'..." % (m["type"], m.get("label") or dev))
        # A parent (usually root) must let go of any mounted child
        # before capturing its own content, or the capture would
        # wrongly include that child's own, separately-rebuilt files.
        children = [c for c in mounts if c is not m
                   and self._where(c) != prefix and self._where(c).startswith(prefix)]
        for c in children:
            g.umount(self._where(c))

        # Capture onto the scratch disk, not the partition: both copies
        # need room at once. It also keeps 'prefix' mounted for g.sh()'s
        # chroot until its device is replaced.
        tag = m.get("label") or os.path.basename(dev)
        content = "%s/content-%s" % (SCRATCH_MOUNT, tag)
        g.mkdir_p(content)
        # One entry at a time, in a fixed sorted order: a plain 'cp -a'
        # would follow prefix's own random per-build hash-seed order,
        # reordering an otherwise identical rebuild.
        scratch_prefix = "/%s" % os.path.basename(SCRATCH_MOUNT)
        # g.find() drops each entry's own leading '/' whenever the path
        # given to it ends in '/' -- true of every '_prefix' here,
        # root ('/') included, so put the '/' back rather than avoid it.
        root = prefix.rstrip("/")
        raw = (e if e.startswith("/") else "/" + e for e in g.find(prefix))
        entries = sorted(e for e in raw
                          if e != scratch_prefix and not e.startswith(scratch_prefix + "/"))
        # Remount now that g.find() is done: g.sh() below needs '/bin/sh',
        # which lives under '/usr' on a usrmerged target.
        # An ext child was rebuilt already: mounted read-only, so the
        # kernel cannot stamp its root with a real access time.
        for c in children:
            if c["type"] in EXT_FSTYPES:
                g.mount_ro(mount_devices[id(c)], self._where(c))
            else:
                g.mount(mount_devices[id(c)], self._where(c))
        if self.verbose:
            print("  copying %d entries..." % len(entries))
        # One RPC per entry used to mean tens of thousands of round-trips
        # for a real rootfs. Batch it into one script, one g.sh() call.
        tools_dir = self._upload_tools(g, "%s/tools" % SCRATCH_MOUNT, self._extra_tools_files)
        if id(m) in self._guest_paths:
            lines = self._tar_copy_script(g, root, content, entries, tag, tools_dir, epoch)
        else:
            lines = self._cp_copy_script(root, content, entries, tag, tools_dir, epoch)
        script_path = "%s/copy-%s.sh" % (SCRATCH_MOUNT, tag)
        g.write(script_path, ("\n".join(lines) + "\n").encode())
        g.sh("LD_LIBRARY_PATH=%s sh %s" % (tools_dir, script_path))
        g.rm(script_path)

        label = m.get("label")
        identifier = self._uuid_for("fs", label or dev)
        # The real device size, not the spec's nominal 'size': LVM rounds
        # volumes up to its extent size, so the two can differ.
        size = g.blockdev_getsize64(dev)
        image = "%s/image-%s.img" % (SCRATCH_MOUNT, tag)
        # SOURCE_DATE_EPOCH fixes file timestamps, E2FSPROGS_FAKE_TIME
        # fixes the superblock's own creation time; the htree hash seed
        # isn't time-based, so it needs its own fixed value here.
        hash_seed = self._uuid_for("fs-hash-seed", label or dev)
        env = ("SOURCE_DATE_EPOCH=%d E2FSPROGS_FAKE_TIME=%d LD_LIBRARY_PATH=%s"
               % (epoch, epoch, tools_dir))
        if self.verbose:
            print("  running mke2fs...")
        # lazy_itable_init/lazy_journal_init default to an SSD-vs-not
        # guess, deferring (and randomizing) group descriptor state.
        g.sh("%s %s/mke2fs -q -F -t %s -b 4096 -U %s "
             "-E hash_seed=%s,lazy_itable_init=0,lazy_journal_init=0%s "
             "-d %s %s %d" % (
            env, tools_dir, m["type"], identifier, hash_seed,
            " -L %s" % label if label else "", content, image, size // 4096))
        # mke2fs always stamps ctime with the real time, and atime
        # sometimes too. 'lost+found' also needs mtime fixed: mke2fs
        # makes that one itself, so the earlier touch pass never saw it.
        # '/dev/*' needs it too: g.sh()'s chroot bind-mounts the
        # appliance's own live /dev over the target's, so 'cp -a' copies
        # a real device node (real mtime) instead of the stored one.
        lines = ["set_inode_field %s %s @%d" % (e, field, epoch)
                 for e in entries for field in ("ctime", "atime")]
        lines += ["set_inode_field %s mtime @%d" % (e, epoch)
                  for e in entries if e == "/dev" or e.startswith("/dev/")]
        lines += ["set_inode_field /lost+found %s @%d" % (field, epoch)
                  for field in ("ctime", "atime", "mtime")]
        script = "\n".join(lines)
        script_path = "%s/ctimefix-%s" % (SCRATCH_MOUNT, tag)
        self._upload_bytes(g, script.encode(), script_path)
        if self.verbose:
            print("  fixing up inode timestamps...")
        # debugfs echoes every command it runs -- for root's ~15000
        # entries that reply can exceed the guestfs protocol's own
        # message-size limit, so it's discarded rather than returned.
        g.sh("%s %s/debugfs -w -f %s %s > /dev/null 2>&1" % (
            env, tools_dir, script_path, image))
        g.rm(script_path)

        # Pulled onto the host first: if 'm' is root, the scratch disk
        # (mounted under it) must be unmounted before root can be, and
        # 'image' stops being reachable by path once that happens.
        host_copy = tempfile.NamedTemporaryFile(delete=False, dir=self._output_dir)
        host_copy.close()
        g.download(image, host_copy.name)
        g.rm_rf(content)
        g.rm(image)
        g.rm_rf(tools_dir)

        # Root is always the last ext mount processed, so nothing here
        # still needs the scratch disk once its content is off it.
        if prefix == "/":
            g.umount(SCRATCH_MOUNT)
        # Re-unmount 'children' (remounted above for g.sh()'s sake) --
        # 'prefix' can't unmount while they're still mounted under it.
        for c in children:
            g.umount(self._where(c))
        g.umount(prefix)
        if self.verbose:
            print("  writing image back (%s)..." % self.source.partitionHandler._to_human_size(size))
        # pwrite_device's RPC has a hard message-size cap well under 32M.
        chunk_size = 1024 * 1024
        with open(host_copy.name, "rb") as f:
            offset = 0
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                g.pwrite_device(dev, chunk, offset)
                offset = offset + len(chunk)
                if self.verbose and offset % (100 * chunk_size) == 0:
                    print("  wrote %s..." % self.source.partitionHandler._to_human_size(offset))
        os.remove(host_copy.name)
        # Read-only: fstab and grub are already written, and a
        # read-write mount would stamp the superblock's own mount/write
        # time with the real time, undoing the fix just made above.
        g.mount_ro(dev, prefix)
        # Only an ext2/3/4 child shares that concern -- everything else
        # (FAT, a still-staged RO_FSTYPES mount) goes back read-write,
        # since later steps still write to it.
        for c in sorted(children, key=lambda c: c["_depth"]):
            if c["type"] in EXT_FSTYPES:
                g.mount_ro(mount_devices[id(c)], self._where(c))
            else:
                g.mount(mount_devices[id(c)], self._where(c))

    def _cp_copy_script(self, root, content, entries, tag, tools_dir, epoch):
        # A fork per entry (~20k execs) is slow under an emulated CPU.
        # Classify with shell builtins (free), then batch each list
        # through one xargs call -- same order, same destination paths.
        dirlist = "%s/dirs-%s.list" % (SCRATCH_MOUNT, tag)
        filelist = "%s/files-%s.list" % (SCRATCH_MOUNT, tag)
        lines = ["set -e", "cd %s" % shlex.quote(root or "/"),
                 ": > %s" % shlex.quote(dirlist), ": > %s" % shlex.quote(filelist)]
        for e in entries:
            src = shlex.quote("%s%s" % (root, e))
            dst = shlex.quote("%s%s" % (content, e))
            rel = shlex.quote(e.lstrip("/"))
            lines.append("if [ -d %s ] && [ ! -L %s ]; then printf '%%s\\0' %s >> %s; "
                          "else printf '%%s\\0' %s >> %s; fi"
                          % (src, src, dst, shlex.quote(dirlist), rel, shlex.quote(filelist)))
        lines.append("if [ -s %s ]; then xargs -0 %s/mkdir -p -- < %s; fi"
                      % (shlex.quote(dirlist), tools_dir, shlex.quote(dirlist)))
        lines.append("if [ -s %s ]; then xargs -0 %s/cp -a --parents -t %s -- < %s; fi"
                      % (shlex.quote(filelist), tools_dir, shlex.quote(content), shlex.quote(filelist)))
        lines.append("rm -f %s %s" % (shlex.quote(dirlist), shlex.quote(filelist)))
        # Directory mtimes only now, after every copy: creating a file
        # bumps its parent directory's own mtime, and 'mkdir' (unlike
        # 'cp -a' for files) never preserved it anyway.
        lines.append("%s/find %s -mindepth 1 -type d -exec %s/touch -d @%d {} +"
                      % (tools_dir, shlex.quote(content), tools_dir, epoch))
        return lines

    # One tar stream, in the sorted order, keeps hardlinks (the ostree
    # repo and its deployments share files): xargs would split the list
    # over several cp calls, and a link only survives within one call.
    def _tar_copy_script(self, g, root, content, entries, tag, tools_dir, epoch):
        listing = "%s/list-%s" % (SCRATCH_MOUNT, tag)
        archive = "%s/copy-%s.tar" % (SCRATCH_MOUNT, tag)
        self._upload_bytes(g, b"".join(e.lstrip("/").encode() + b"\0" for e in entries), listing)
        return [
            "set -e", "cd %s" % shlex.quote(root or "/"),
            "tar --no-recursion --xattrs --numeric-owner --null -T %s -cf %s"
            % (shlex.quote(listing), shlex.quote(archive)),
            "tar -C %s --xattrs --numeric-owner -xpf %s"
            % (shlex.quote(content), shlex.quote(archive)),
            "rm -f %s %s" % (shlex.quote(listing), shlex.quote(archive)),
            "%s/find %s -mindepth 1 -type d -exec %s/touch -d @%d {} +"
            % (tools_dir, shlex.quote(content), tools_dir, epoch),
        ]

    # ext4's on-disk checksum is plain crc32c with no final '~crc' --
    # a textbook CRC-32C applies that step, this must not.
    def _crc32c(self, data):
        crc = 0xffffffff
        for byte in data:
            crc ^= byte
            for _ in range(8):
                crc = (crc >> 1) ^ 0x82f63b78 if crc & 1 else crc >> 1
        return crc

    # Mounting, even read-only, stamps ext4 mtime, wtime and
    # kbytes_written. Patch raw bytes, not via debugfs (it re-stamps
    # wtime), then redo the metadata_csum checksum after.
    def _pin_ext_mtimes(self, g, mounts, mount_devices):
        epoch = self.source._epoch()
        for m in mounts:
            if m["type"] in EXT_FSTYPES:
                dev = mount_devices[id(m)]
                sb = bytearray(g.pread_device(dev, 1024, 1024))
                struct.pack_into("<I", sb, 44, epoch)      # mtime
                struct.pack_into("<I", sb, 48, epoch)      # wtime
                struct.pack_into("<Q", sb, 376, 0)         # kbytes_written
                struct.pack_into("<I", sb, 1020, self._crc32c(bytes(sb[:1020])))
                g.pwrite_device(dev, bytes(sb), 1024)

    # The vfat volume serial, matching what _normalize_fat_tree() will
    # write with mformat -- also needed early by _write_fstab(), since
    # g.mkfs() can't set a real vfat UUID for it to read back.
    def _fat_serial(self, label, dev):
        return self._uuid_for("fs", label or dev).replace("-", "")[:8].upper()

    # FAT's volume serial and file timestamps are set once at write
    # time and can't be touched again through the mount, so the whole
    # partition is rebuilt from scratch with mtools instead.
    # 'staging' is the scratch disk from _normalize_mount_timestamps().
    # Empty means no scratch device exists, so the copy goes to '/' as
    # before.
    def _normalize_fat_tree(self, g, m, dev, staging=""):
        scratch = "%s/.fat-scratch" % staging
        g.mkdir_p(scratch)
        where = self._where(m)
        g.cp_a(where, scratch)
        base = "%s/%s" % (scratch, os.path.basename(where.rstrip("/")))
        # Sorted, and read while still mounted: 'mcopy -s' would instead
        # walk the scratch copy's own random per-build hash-seed order,
        # allocating FAT clusters differently for identical content.
        entries = sorted(g.find(where))
        dirs = {e for e in entries if g.is_dir("%s/%s" % (where, e))}
        g.umount(where)

        # mformat only rewrites the boot sector, FAT and root directory
        # -- old data (real timestamps included) survives a reformat
        # wherever this build's files don't land on the same clusters.
        g.zero_device(dev)

        tools_dir = self._upload_tools(g, "%s/.imager-extra-tools" % staging, self._extra_tools_files)
        # Set before mformat too: '-v' makes it write a volume-label
        # entry of its own, timestamped like any other.
        env = "LD_LIBRARY_PATH=%s SOURCE_DATE_EPOCH=%d" % (
            tools_dir, self.source._epoch())
        label = m.get("label")
        serial = self._fat_serial(label, dev)
        # mtools can't work out a partition device's own geometry on its
        # own ("Hidden ... does not match sectors"); its disk offset is
        # the one BPB field that has to be told, not left to guessing.
        hidden = 0 if m["_lvm"] else m["_start_mib"] * 2048
        g.sh("%s %s/mformat -i %s -H %d -N %s%s ::" % (
            env, tools_dir, dev, hidden, serial,
            " -v %s" % label.upper()[:11] if label else ""))

        for entry in entries:
            target = "::/%s" % entry
            if entry in dirs:
                g.sh("%s %s/mmd -i %s %s" % (env, tools_dir, dev, target))
            else:
                g.sh("%s %s/mcopy -Q -i %s %s/%s %s" % (
                    env, tools_dir, dev, base, entry, target))

        g.rm_rf(scratch)
        g.rm_rf(tools_dir)
        g.mount(dev, where)

    # Uploads 'host_files' into a scratch dir, ready to run via
    # 'LD_LIBRARY_PATH=<dir> <dir>/<name>'.
    def _upload_tools(self, g, tools_dir, host_files):
        g.mkdir_p(tools_dir)
        for host_path in host_files:
            remote_path = "%s/%s" % (tools_dir, os.path.basename(host_path))
            g.upload(host_path, remote_path)
            g.chmod(0o755, remote_path)
        return tools_dir
