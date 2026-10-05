# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier Apache-2.0

import json
import math
import os
import re
import tarfile

from seine import utils

RO_FSTYPES = {"squashfs", "erofs"}

# Raw dm-verity hash tree, never mounted or mkfs'd. Only valid paired with
# a '/' or '/usr' mount (the only types DPS gives an auto-discovered GUID).
VERITY_HASH_TYPE = "verity-hash"

OSTREE_MODES = ("disabled", "standard", "composefs")

# These live in the commit as symlinks into /var (or, for composefs,
# as plain directories), so a mount there would hide or break them.
OSTREE_RESERVED = ("/home", "/srv", "/root", "/mnt", "/opt", "/usr/local")

# Releases without the dracut ostree path, and releases with an ostree
# built with composefs (trixie's is not).
OSTREE_OLD_RELEASES = ("bullseye", "bookworm", "oldstable")
OSTREE_COMPOSEFS_RELEASES = ("forky", "testing", "sid", "unstable")

# Each retained deployment checks out its own /etc and boot entry.
OSTREE_DEPLOYMENT_OVERHEAD = 2 * 1024 * 1024

# Stateroots and refs end up in file names and ostree ref names.
OSTREE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# Same rule as VaultSigner for the name of a vault key.
OSTREE_KEY_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")

class PartitionHandler:

    START_OFFSET_KB  = 1 * 1024
    DEFAULT_EXTRA_MB = 16
    DEFAULT_TABLE    = "gpt"

    def __init__(self):
        self._min_size = None
        self._table = None
        self.bootlets = []
        self.groups = []
        self.mounts = []
        self.partitions = []
        self.ostree = {"mode": "disabled"}
        self.secure_boot = None
        self.volumes = []
        self.size = None

    def _align_up(self, n, align):
        return math.ceil(n / align) * align

    def _from_human_size(self, size_string):
        try:
            size_string = size_string.lower().replace(',', '')
            size = re.search(r'^(\d+)[a-z]i?b$', size_string).groups()[0]
            suffix = re.search(r'^\d+([kmgtp])i?b$', size_string).groups()[0]
        except AttributeError:
            raise ValueError("%s is not a valid size!" % size_string)
        shft = suffix.translate(str.maketrans('kmgtp', '12345')) + '0'
        return int(size) << int(shft)

    def _to_human_size(self, size):
        if (size == 0):
            return "0B"
        size_name = ("B", "KiB", "MiB", "GiB", "TiB", "PiB", "EiB", "ZiB", "YiB")
        i = int(math.floor(math.log(size, 1024)))
        p = math.pow(1024, i)
        s = round(size / p, 2)
        return '%s%s' % (s, size_name[i])

    def _to_rounded_mib(self, size):
        return math.ceil(size / 1024 / 1024)

    def _parse_part_flags(self, part):
        valid_flags = [ "boot", "lvm", "xbootldr", "primary", "extended", "logical" ]
        incompatible_flags = [
            [ "primary", "extended", "logical" ]
        ]

        for f in part["flags"]:
            if f not in valid_flags:
                raise ValueError("'%s' is not a valid partition flag!" % f)
            if f == "lvm":
                part["_lvm"] = True
            if f in ("boot", "xbootldr") and part["type"] != "vfat":
                raise ValueError(
                    "partition '%s' has flag '%s', which UEFI firmware can "
                    "only read from a 'vfat' partition (this one is '%s')"
                    % (part["label"], f, part["type"]))

        for set in incompatible_flags:
            matched = []
            for f in part["flags"]:
                if f in set:
                    matched.append(f)
            if len(matched) > 1:
                 raise ValueError("the following partition flags may not be used together: %s!" % (" ".join(matched)))

    def _parse_bootlet(self, bootlet):
        if "file" not in bootlet:
            raise ValueError("one of the bootlets does not have a 'file' defined!")

        if "align" not in bootlet:
            bootlet["_align"] = 1
        else:
            bootlet["_align"] = int(bootlet["align"])

        if "priority" not in bootlet:
            bootlet["priority"] = 500

        return bootlet

    # Disk-wide signing key/cert for any UKI the imager anchors. Paths
    # resolve like 'multiconfig: files:'; a 'private-key' starting
    # with 'vault:' names a vault sbsign key instead, cert included.
    def _parse_secure_boot(self, secure_boot):
        if type(secure_boot) != type({}):
            raise ValueError("'image: secure-boot' shall be a mapping")
        if "private-key" not in secure_boot:
            raise ValueError(
                "'image: secure-boot' needs 'private-key' and "
                "'public-cert' -- 'private-key' is missing")
        for field in ("private-key", "public-cert"):
            if field in secure_boot and type(secure_boot[field]) != type(""):
                raise ValueError(
                    "'image: secure-boot: %s' shall be a string" % field)
        if secure_boot["private-key"].startswith("vault:"):
            return secure_boot
        if "public-cert" not in secure_boot:
            raise ValueError(
                "'image: secure-boot' needs both 'private-key' and "
                "'public-cert' -- 'public-cert' is missing")
        return secure_boot

    def _parse_common(self, part):
        part["_blksz"] = 4096
        part["_depth"] = 0

        if "priority" not in part:
            part["priority"] = 500

        if "extra" in part:
            part["_size"] = self._from_human_size(part["extra"])
        else:
            part["_size"] = self._from_human_size("%dMiB" % PartitionHandler.DEFAULT_EXTRA_MB)
        part["_slack"] = part["_size"]

        if "where" in part:
            prefix = os.path.normpath(part["where"])
            if prefix.endswith("/") == False:
                prefix = prefix + "/"
            depth = prefix.count("/") - 1
            part["_depth"] = depth
            part["_prefix"] = prefix
        if "size" in part:
            part["size"] = self._from_human_size(part["size"])
        if "type" not in part:
            part["type"] = "ext4"

        return part

    def _parse_part(self, part):
        if "label" not in part:
            raise ValueError("one of the partitions does not have a 'label' defined!")
        label = part["label"]

        part = self._parse_common(part)
        part["_lvm"] = False
        is_verity_hash = part["type"] == VERITY_HASH_TYPE

        if "flags" in part:
            self._parse_part_flags(part)

        if "where" not in part and part["_lvm"] == False and not is_verity_hash:
            raise ValueError("'where' not defined in partition '%s'!" % label)
        if is_verity_hash and "where" in part:
            raise ValueError(
                "partition '%s' has type 'verity-hash', which is never "
                "mounted -- drop its 'where'" % label)
        if part["type"] in RO_FSTYPES and part["_lvm"] == False and self._table != "gpt":
            raise ValueError(
                "partition '%s' has a read-only type ('%s'), which needs a 'gpt' "
                "partition table to be identified in /etc/fstab (this image's "
                "table is '%s')" % (label, part["type"], self._table))
        if is_verity_hash and self._table != "gpt":
            raise ValueError(
                "partition '%s' has type 'verity-hash', which needs a 'gpt' "
                "partition table (this image's table is '%s')"
                % (label, self._table))
        if is_verity_hash and part["_lvm"]:
            raise ValueError(
                "partition '%s' has type 'verity-hash' and flag 'lvm', "
                "which may not be used together -- a verity-hash partition "
                "is always a plain GPT partition" % label)
        if part["_lvm"] == True:
            if "group" not in part:
                raise ValueError("target 'group' not defined for partition '%s'!" % label)
            elif part["group"] not in self.groups:
                self.groups.append(part["group"])
            if "size" in part:
                part["_size"] = part["size"]
        if is_verity_hash:
            if "verity-for" not in part:
                raise ValueError(
                    "partition '%s' has type 'verity-hash', which needs a "
                    "'verity-for' naming the partition it protects" % label)
            if "size" not in part:
                raise ValueError(
                    "'size' of verity-hash partition '%s' was not defined "
                    "(its hash tree's size is only known once built, so it "
                    "cannot be inferred)" % label)
            part["_size"] = part["size"]
        if "verity" in part:
            if type(part["verity"]) != type(True):
                raise ValueError("partition '%s': 'verity' shall be true or false" % label)
            if part["verity"] and part["type"] not in RO_FSTYPES:
                raise ValueError(
                    "partition '%s' has 'verity: true', which needs a "
                    "read-only type ('squashfs'/'erofs') (this one is '%s')"
                    % (label, part["type"]))
            if part["verity"] and part.get("identify") == "partuuid":
                raise ValueError(
                    "partition '%s' has 'verity: true', which is never "
                    "identified in /etc/fstab (it has no fstab entry at all "
                    "-- drop 'identify: partuuid')" % label)
        return part

    def _parse_vol(self, vol):
        if "label" not in vol:
            raise ValueError("one of the volumes does not have a 'label' defined!")
        label = vol["label"]

        vol = self._parse_common(vol)
        vol["_lvm"] = True

        if "group" not in vol:
            raise ValueError("no 'group' defined for volume '%s'!" % label)
        if "where" not in vol:
            raise ValueError("'where' not defined in volume '%s'!" % label)
        return vol

    def _size_file(self, f, part):
        blksz = part["_blksz"]
        size = math.floor((f.size + blksz - 1) / blksz) * blksz
        return size if size > 0 else blksz

    def disk_size(self):
        if self._min_size is None:
            raise RuntimeError("partitions sizes shall be computed first!")

        if self.size is None or self._min_size > self.size:
            return self._min_size
        else:
            return self.size

    # 'source' picks which 'multiconfig:' group's rootfs 'f' came from
    # ('None' = this spec's own). Only mounts/bootlets with a matching
    # 'source' can claim the file, so groups never share one size.
    def distribute(self, f, source=None):
        if f.name.startswith("/") == False:
            name = "/" + f.name
        else:
            name = f.name

        if source is None:
            for bootlet in self.bootlets:
                if name == bootlet["file"]:
                    bootlet["_size"] = f.size
                    break

        for mount in self.mounts:
            if mount.get("source") != source:
                continue
            if mount["_prefix"] is not None and name.startswith(mount["_prefix"]):
                mount["_size"] = mount["_size"] + self._size_file(f, mount)
                return mount
        return None

    def _inspect_container_archive(self, archive_path):
        uncompressed_bytes = 0
        inodes = 0
        with tarfile.open(archive_path, "r") as tar:
            try:
                manifest_member = tar.getmember("manifest.json")
            except KeyError:
                return 0, 0
            manifest_file = tar.extractfile(manifest_member)
            if manifest_file is None:
                return 0, 0
            manifest = json.loads(manifest_file.read().decode("utf-8"))
            if not isinstance(manifest, list):
                manifest = [manifest]
            for item in manifest:
                for layer_tar_name in item.get("Layers", []):
                    try:
                        layer_member = tar.getmember(layer_tar_name)
                    except KeyError:
                        continue
                    layer_file = tar.extractfile(layer_member)
                    if layer_file is None:
                        continue
                    with tarfile.open(fileobj=layer_file, mode="r|*") as layer_tar:
                        for member in layer_tar:
                            inodes += 1
                            size = member.size
                            aligned = math.ceil(size / 4096) * 4096 if size > 0 else 4096
                            uncompressed_bytes += aligned
        return uncompressed_bytes, inodes

    # Sizes container storage from layer payload blocks (4KB-aligned),
    # inode allocation overhead, and 15% ext4 metadata slack, routing
    # storage to the partition matching each archive's target root.
    def distribute_container_archives(self, archives, target_mount_path="/var/lib/docker", source=None):
        groups = {}
        for item in archives:
            if isinstance(item, (tuple, list)):
                archive_path, root = item
            else:
                archive_path, root = item, target_mount_path
            groups.setdefault(root, []).append(archive_path)

        matched_mounts = []
        for target_root, root_archives in groups.items():
            total_uncompressed = 0
            total_inodes = 0
            for archive_path in root_archives:
                bytes_count, inodes = self._inspect_container_archive(archive_path)
                total_uncompressed += bytes_count
                total_inodes += inodes

            archive_bytes = sum(os.path.getsize(a) for a in root_archives if os.path.exists(a))
            inode_overhead = total_inodes * 256
            slack_overhead = int((total_uncompressed + archive_bytes) * 0.20) + 128 * 1024 * 1024
            total_required = total_uncompressed + archive_bytes + inode_overhead + slack_overhead

            target_norm = os.path.normpath(target_root)
            if not target_norm.endswith("/"):
                target_norm += "/"

            for mount in self.mounts:
                if mount.get("source") != source:
                    continue
                prefix = mount.get("_prefix")
                if prefix and target_norm.startswith(prefix):
                    mount["_size"] += total_required
                    if mount not in matched_mounts:
                        matched_mounts.append(mount)
                    break

        if not matched_mounts:
            return None
        return matched_mounts[0] if len(matched_mounts) == 1 else matched_mounts

    def compute_sizes(self):
        for bootlet in self.bootlets:
            if "_size" not in bootlet:
                raise RuntimeError("bootlet '%s' was not found in the image!" % bootlet["file"])

        if self._table == "msdos":
            start = 1      # MBR: 512 bytes, rounded up to 1 KiB
        elif self._table == "gpt":
            start = 34 * 4 # GPT: 34 LBAs of 4KiB each
        else:
            raise RuntimeError("'%s' is not a supported partition table!" % self._table)

        for bootlet in self.bootlets:
            start = self._align_up(start, bootlet["_align"])
            bootlet["_seek"] = start
            size = math.ceil(bootlet["_size"] / 1024) # KiB
            start = start + size

        if start < PartitionHandler.START_OFFSET_KB:
            start = PartitionHandler.START_OFFSET_KB

        start = self._to_rounded_mib(start)
        self._start_offset = start

        self._add_ostree_room()

        for mount in self.mounts:
            mount["_size"] = self._to_rounded_mib(mount["_size"]) * 1024 * 1024
            if "size" in mount and mount["size"] > mount["_size"]:
                mount["_size"] = mount["size"]

        # Compute sizes for LVM physical volume partitions and unmounted partitions
        mounted = {id(m) for m in self.mounts}
        for part in self.partitions:
            if part.get("_lvm"):
                group = part.get("group")
                vols = [v for v in self.volumes if v.get("group") == group]
                pv_min = sum(v["_size"] for v in vols) + 16 * 1024 * 1024
                if "extra" in part:
                    pv_min += self._from_human_size(part["extra"])
                if "size" in part and part["size"] > pv_min:
                    part["_size"] = part["size"]
                else:
                    part["_size"] = self._to_rounded_mib(pv_min) * 1024 * 1024
            elif id(part) not in mounted:
                part["_size"] = self._to_rounded_mib(part["_size"]) * 1024 * 1024

        # self.mounts and self.partitions share the same dicts, so
        # every part's '_size' is now final -- lay out start/end in MiB.
        layout_start = self._start_offset
        for part in self.partitions:
            part["_start_mib"] = layout_start
            layout_start = layout_start + self._to_rounded_mib(part["_size"])
            part["_end_mib"] = layout_start

        # +1 MiB at the end of the disk for the backup GPT
        self._min_size = (layout_start + 1) * 1024 * 1024

    # An ostree sysroot holds the repo (the root's own content, plus the
    # /var content it keeps for seeding) and room for one more full
    # deployment, so a first upgrade does not fail on a full disk. An
    # explicit 'size:' still wins.
    def _add_ostree_room(self):
        for source, mounts in self.rooted_sources().items():
            if self.ostree_for(source)["mode"] == "disabled":
                continue
            root = next(m for m in mounts if m["_prefix"] == "/")
            var = next(m for m in mounts if m["_prefix"] == "/var/")
            content = root["_size"] - root["_slack"]
            seed = var["_size"] - var["_slack"]
            root["_size"] += content + seed + OSTREE_DEPLOYMENT_OVERHEAD

    def print_stats(self):
        print("prologue:\t%s" % self._to_human_size(self._start_offset))
        print("mounts:")
        print("-------")
        size = 0
        for mount in self.mounts:
            print("%s\t%s" % (mount["where"], self._to_human_size(mount["_size"])))
            size = size + mount["_size"]
        print("total\t%s\n" % self._to_human_size(size))
        print("disk\t%s" % self._to_human_size(self.disk_size()))

    def parse(self, spec):
        if "image" not in spec:
            raise ValueError("'image' not found in provided specification!")
        if spec["image"] is None:
            raise ValueError("empty 'image' definition!")
        image = spec["image"]
        if "partitions" not in image:
            raise ValueError("no 'partitions' defined in the 'image' section of the specification!")
        if "size" in image:
            self.size = self._from_human_size(image["size"])
        if "table" in image:
            self._table = image["table"]
            if self._table not in [ "msdos", "gpt" ]:
                raise ValueError("'%s' is not a supported partition table!" % self._table)
        else:
            self._table = PartitionHandler.DEFAULT_TABLE

        if "bootlets" in image:
            bootlets = image["bootlets"]
            for bootlet in bootlets:
                bootlet = self._parse_bootlet(bootlet)
                self.bootlets.append(bootlet)
            self.bootlets = sorted(self.bootlets, key=lambda b: b["priority"])
        image["bootlets"] = self.bootlets

        if "secure-boot" in image:
            self.secure_boot = self._parse_secure_boot(image["secure-boot"])

        partitions = image["partitions"]
        for part in partitions:
            part = self._parse_part(part)
            self.partitions.append(part)
            if "where" in part:
                self.mounts.append(part)
        image["partitions"] = sorted(self.partitions, key=lambda p: p["priority"])

        if "volumes" in image:
            volumes = image["volumes"]
            for vol in volumes:
                vol = self._parse_vol(vol)
                self.mounts.append(vol)
                self.volumes.append(vol)
            image["volumes"] = sorted(self.volumes, key=lambda p: p["priority"])

        self.mounts = sorted(self.mounts, key=lambda vol: vol["_depth"], reverse=True)
        self._validate_sources(spec)
        self._validate_verity(spec)
        if "ostree" in image:
            self.ostree = self._parse_ostree(image["ostree"])
        self._validate_ostree(spec)
        return spec

    def _parse_ostree_settings(self, where, settings):
        if type(settings) != type({}):
            raise ValueError("'%s' shall be a mapping" % where)
        allowed = ("mode", "stateroot", "ref", "gpg-key")
        if where == "image: ostree":
            allowed += ("sources",)
        for key in settings:
            if key not in allowed:
                raise ValueError("'%s' has no '%s' attribute" % (where, key))
        mode = settings.get("mode")
        if mode is not None and mode not in OSTREE_MODES:
            raise ValueError(
                "'%s: mode: %s' is not one of: %s"
                % (where, mode, ", ".join(OSTREE_MODES)))
        for key in ("stateroot", "ref"):
            if key not in settings:
                continue
            value = settings[key]
            ok = type(value) == type("") and all(
                OSTREE_NAME.match(part) for part in value.split("/"))
            if not ok or (key == "stateroot" and "/" in value):
                raise ValueError(
                    "'%s: %s: %s' is not a valid name" % (where, key, value))
        key = settings.get("gpg-key")
        if key is not None and not (
                type(key) == type("") and key.startswith("vault:")
                and OSTREE_KEY_NAME.match(key[len("vault:"):])):
            raise ValueError(
                "'%s: gpg-key: %s' shall be 'vault:<name>'" % (where, key))
        return dict(settings)

    def _parse_ostree(self, ostree):
        parsed = self._parse_ostree_settings("image: ostree", ostree)
        parsed.setdefault("mode", "disabled")
        sources = parsed.get("sources", {})
        if type(sources) != type({}):
            raise ValueError("'image: ostree: sources' shall be a mapping")
        parsed["sources"] = {
            name: self._parse_ostree_settings(
                "image: ostree: sources: %s" % name, settings)
            for name, settings in sources.items()}
        return parsed

    # What applies to one 'source:' (None is the image's own root):
    # the block's own settings, overridden by its 'sources:' entry.
    def ostree_for(self, source):
        settings = {k: v for k, v in self.ostree.items() if k != "sources"}
        settings.update(self.ostree.get("sources", {}).get(source, {}))
        if "stateroot" not in settings:
            settings["stateroot"] = source or "debian"
        return settings

    # Mounts of each rooted 'source:' (None is the image's own root).
    def rooted_sources(self):
        sources = {}
        for mount in self.mounts:
            sources.setdefault(mount.get("source"), []).append(mount)
        return {name: mounts for name, mounts in sources.items()
                if any(m["_prefix"] == "/" for m in mounts)}

    # With ostree on, each rooted source gets a physical sysroot ('/')
    # and a persistent '/var'; the commit itself holds no partition.
    def _validate_ostree(self, spec):
        groups = spec.get("multiconfig") or {}
        for name in self.ostree.get("sources", {}):
            if name not in groups:
                raise ValueError(
                    "'image: ostree: sources' names '%s', which is not one "
                    "of the declared 'multiconfig:' groups (%s)"
                    % (name, ", ".join(sorted(groups)) if groups else "none"))
        rooted = self.rooted_sources()
        enabled = {name: self.ostree_for(name)
                   for name in rooted
                   if self.ostree_for(name)["mode"] != "disabled"}
        if not enabled:
            return

        modes = {settings["mode"] for settings in enabled.values()}
        release = utils.distribution(spec)["release"]
        if release in OSTREE_OLD_RELEASES:
            raise ValueError(
                "'image: ostree' needs dracut, which seine only supports "
                "on trixie or newer (this build is '%s')" % release)
        if "composefs" in modes and release not in OSTREE_COMPOSEFS_RELEASES:
            raise ValueError(
                "'image: ostree: mode: composefs' needs an ostree built "
                "with composefs, which '%s' does not ship (forky or newer "
                "does)" % release)
        if self._table != "gpt":
            raise ValueError(
                "'image: ostree' needs a 'gpt' partition table (this "
                "image's table is '%s')" % self._table)

        if self.bootlets:
            raise ValueError(
                "'image: bootlets' cannot be used with 'image: ostree' yet")
        stateroots = {}
        for name, settings in enabled.items():
            who = "'multiconfig:' group '%s'" % name if name else "the image"
            other = stateroots.setdefault(settings["stateroot"], who)
            if other != who:
                raise ValueError(
                    "%s and %s share the ostree stateroot '%s' -- give "
                    "each its own in 'image: ostree: sources'"
                    % (other, who, settings["stateroot"]))
            if not OSTREE_NAME.match(settings["stateroot"]):
                raise ValueError(
                    "%s cannot name an ostree stateroot -- set 'stateroot' "
                    "in 'image: ostree: sources'" % who)
            mounts = rooted[name]
            for part in mounts:
                if part.get("verity") and part["_prefix"] in ("/", "/usr/"):
                    raise ValueError(
                        "partition '%s' has 'verity: true' on '%s', which "
                        "'image: ostree' does not support (the sysroot "
                        "stays writable)"
                        % (part["label"], part["_prefix"].rstrip("/") or "/"))
                if part["type"] in RO_FSTYPES:
                    raise ValueError(
                        "'%s' is a read-only partition, which 'image: "
                        "ostree' cannot fill yet" % part["label"])
                where = part["_prefix"].rstrip("/")
                if where in OSTREE_RESERVED:
                    raise ValueError(
                        "'%s' mounts '%s', which 'image: ostree' keeps in "
                        "the commit -- mount the data under '/var' instead "
                        "(e.g. 'where: /var%s')" % (part["label"], where, where))
                if where == "/boot" and part["type"] in ("vfat", "msdos"):
                    raise ValueError(
                        "'%s' mounts a vfat '/boot', where ostree cannot "
                        "deploy -- use ext4 (the ESP carries the boot "
                        "loader)" % part["label"])
            root = next(m for m in mounts if m["_prefix"] == "/")
            if root["type"] != "ext4" or root["_lvm"]:
                raise ValueError(
                    "'%s' is %s's ostree sysroot, which needs a plain "
                    "'ext4' partition" % (root["label"], who))
            if not any(m["_prefix"] == "/var/" for m in mounts):
                raise ValueError(
                    "'image: ostree' needs a '/var' partition or volume "
                    "for %s" % who)

    # Each 'multiconfig:' group a mount's 'source:' names needs exactly
    # one root ('where: "/"') -- groups are side-by-side OSes, not
    # partitions of one, so zero or several roots is an error.
    def _validate_sources(self, spec):
        groups = spec.get("multiconfig") or {}
        referenced = {}
        for mount in self.mounts:
            source = mount.get("source")
            if source is None:
                continue
            if source not in groups:
                raise ValueError(
                    "'%s' names 'source: %s', which is not one of the "
                    "declared 'multiconfig:' groups (%s)"
                    % (mount["label"], source,
                       ", ".join(sorted(groups)) if groups else "none"))
            referenced.setdefault(source, []).append(mount)
        for name, mounts in referenced.items():
            roots = [m for m in mounts if m["_prefix"] == "/"]
            if len(roots) != 1:
                raise ValueError(
                    "'multiconfig:' group '%s' needs exactly one partition "
                    "or volume with 'source: %s' and 'where: \"/\"' (found %d)"
                    % (name, name, len(roots)))

    # Every 'verity: true' partition needs one 'verity-hash' partition
    # naming it via 'verity-for:', sharing its 'source:', mounted at
    # '/' or '/usr' (see imager/imager.py's GPT_TYPE_ROOT_VERITY/_USR_VERITY).
    def _validate_verity(self, spec):
        by_label = {p["label"]: p for p in self.partitions}
        protected = {p["label"] for p in self.partitions if p.get("verity")}
        paired = set()
        for part in self.partitions:
            if part["type"] != VERITY_HASH_TYPE:
                continue
            label = part["label"]
            target = part["verity-for"]
            data = by_label.get(target)
            if data is None:
                raise ValueError(
                    "partition '%s' names 'verity-for: %s', which is not "
                    "one of the declared partitions" % (label, target))
            if not data.get("verity"):
                raise ValueError(
                    "partition '%s' names 'verity-for: %s', which does not "
                    "have 'verity: true' set" % (label, target))
            if target in paired:
                raise ValueError(
                    "partition '%s' has more than one 'verity-hash' "
                    "partition naming it in 'verity-for:'" % target)
            paired.add(target)
            if data.get("source") != part.get("source"):
                raise ValueError(
                    "partition '%s' and '%s' must share the same 'source:' "
                    "to be verity-paired" % (label, target))
            if data["_prefix"] not in ("/", "/usr/"):
                raise ValueError(
                    "'verity: true' is only supported on '/' or '/usr' "
                    "partitions (DPS defines no auto-discovered Verity "
                    "partition type for '%s')" % data["_prefix"])
            if data["_lvm"]:
                raise ValueError(
                    "'verity: true' is not supported on '%s': it is an LVM "
                    "logical volume, which has no GPT partition UUID for "
                    "DPS auto-discovery to pair a verity-hash partition "
                    "against" % target)
        missing = protected - paired
        if missing:
            raise ValueError(
                "the following partitions have 'verity: true' but no "
                "'verity-hash' partition names them in 'verity-for:': %s"
                % ", ".join(sorted(missing)))

