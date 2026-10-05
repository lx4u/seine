# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

from seine.imager.appliance import DEVICE
from seine.partition import VERITY_HASH_TYPE

# GPT partition type GUIDs.
GPT_TYPE_ESP = "C12A7328-F81F-11D2-BA4B-00A0C93EC93B"
GPT_TYPE_LVM = "E6D6D379-F507-44C2-A23C-238F2A3DF928"
GPT_TYPE_XBOOTLDR = "BC13C2FF-59E6-4262-A352-B275FD6F7172"

# DPS: root and '/usr' GUIDs are arch-specific (auto-discovery must not
# pick the wrong kernel's root on a mixed-arch disk); '/var', '/var/tmp',
# '/home', '/srv' are universal. Keyed by 'distribution: architecture:'.
GPT_TYPE_ROOT = {
    "amd64": "4F68BCE3-E8CD-4DB1-96E7-FBCAF984B709",
    "arm64": "B921B045-1DF0-41C3-AF44-4C6F280D3FAE",
    "armhf": "69DAD710-2CE4-4E3C-B16C-21A1D49ABED3",
    "i386":  "44479540-F297-41B2-9AF7-D131D5F0458A",
}
GPT_TYPE_USR = {
    "amd64": "8484680C-9521-48C6-9C11-B0720656F69E",
    "arm64": "B0E01050-EE5F-4390-949A-9101B17104E9",
    "armhf": "7D0359A3-02B3-4F0A-865C-654403E70625",
    "i386":  "75250D76-8CC6-458E-BD66-BD47CC81A812",
}
GPT_TYPE_VAR = "4D21B016-B534-45C2-A9FB-5C16E091FD2D"
GPT_TYPE_VAR_TMP = "7EC6F557-3BC5-4ACA-B293-16EF5DF639D1"
GPT_TYPE_HOME = "933AC7E1-2EB4-4F13-B844-0E14E2AEF915"
GPT_TYPE_SRV = "3B8F8425-20E0-4F3B-907F-1A25A76F98E8"

# DPS verity GUIDs for root/'/usr', the only mountpoints it defines.
# No signature partition or cmdline roothash= yet: _build_verity() sets
# each pair's GPT GUID to the root hash's two halves instead (systemd-repart's scheme).
GPT_TYPE_ROOT_VERITY = {
    "amd64": "2C7357ED-EBD2-46D9-AEC1-23D437EC2BF5",
    "arm64": "DF3300CE-D69F-4C92-978C-9BFB0F38D820",
    "armhf": "7386CDF2-203C-47A9-A498-F2ECCE45A2D6",
    "i386":  "D13C5D3B-B5D1-422A-B29F-9454FDC89D76",
}
GPT_TYPE_USR_VERITY = {
    "amd64": "77FF5F63-E7B6-4633-ACF4-1565B864C0E6",
    "arm64": "6E11A4E7-FBCA-4DED-B9E9-E1A512BB664E",
    "armhf": "C215D751-7BCD-4649-BE90-6627490A4C05",
    "i386":  "8F461B0D-14EE-4E81-9AA9-049B6FB97ABD",
}

# Partitions and volumes as GPT lays them out: Discoverable Partitions
# Specification type GUIDs, per-partition GUIDs from the spec, file systems.
# Part of Imager, which gives it its source, uuid and mkfs helpers.
class GptLayout:
    # DPS role GUID for a plain (non-ESP/LVM/XBOOTLDR) partition.
    # 'None' means no DPS role -- parted keeps its generic default GUID.
    DPS_UNIVERSAL_ROLES = {
        "/var/":      GPT_TYPE_VAR,
        "/var/tmp/":  GPT_TYPE_VAR_TMP,
        "/home/":     GPT_TYPE_HOME,
        "/srv/":      GPT_TYPE_SRV,
    }

    def _dps_gpt_type(self, part, target_arch, partitions_by_label):
        if part.get("type") == VERITY_HASH_TYPE:
            data = partitions_by_label[part["verity-for"]]
            roles = GPT_TYPE_ROOT_VERITY if data["_prefix"] == "/" else GPT_TYPE_USR_VERITY
            return roles.get(target_arch)
        prefix = part.get("_prefix")
        if prefix == "/":
            return GPT_TYPE_ROOT.get(target_arch)
        if prefix == "/usr/":
            return GPT_TYPE_USR.get(target_arch)
        return self.DPS_UNIVERSAL_ROLES.get(prefix)

    def _partition_device(self, g, table, part, index, target_arch, partitions_by_label):
        start_sect = part["_start_mib"] * 2048
        end_sect = part["_end_mib"] * 2048 - 1
        flags = part.get("flags", [])
        prlogex = "primary"
        if table == "msdos":
            if "extended" in flags:
                prlogex = "extended"
            if "logical" in flags:
                prlogex = "logical"
        g.part_add(DEVICE, prlogex, start_sect, end_sect)
        if table == "gpt":
            g.part_set_name(DEVICE, index, part["label"])
            # A verity partition's own GUID is set again later, from its
            # root hash (see _build_verity()) -- this one is overwritten.
            g.part_set_gpt_guid(DEVICE, index, self._uuid_for("partition", part["label"]))
            if "boot" in flags:
                g.part_set_gpt_type(DEVICE, index, GPT_TYPE_ESP)
            elif "lvm" in flags:
                g.part_set_gpt_type(DEVICE, index, GPT_TYPE_LVM)
            elif "xbootldr" in flags:
                g.part_set_gpt_type(DEVICE, index, GPT_TYPE_XBOOTLDR)
            else:
                dps_type = self._dps_gpt_type(part, target_arch, partitions_by_label)
                if dps_type is not None:
                    g.part_set_gpt_type(DEVICE, index, dps_type)
        elif "boot" in flags:
            g.part_set_bootable(DEVICE, index, True)
        return DEVICE + str(index)

    # 'm' must be a physical partition (in 'part_index') -- LVM volumes have
    # no GPT entry to read a PARTUUID from. Lower-cased to match udev's
    # by-partuuid symlinks.
    def _partuuid(self, g, part_index, m):
        return g.part_get_gpt_guid(DEVICE, part_index[id(m)]).lower()

    # systemd-repart's scheme: a verity pair's root hash is its two GPT
    # GUIDs concatenated (data = high 128 bits, hash = low 128). Not
    # authentication alone -- pinning it needs a signed UKI (seine/extends/uki.py).
    def _hex_to_gpt_guid(self, hexstr):
        return "%s-%s-%s-%s-%s" % (
            hexstr[0:8], hexstr[8:12], hexstr[12:16], hexstr[16:20], hexstr[20:32])

    def _create_partitions(self, g, ph):
        print("Partitioning (%s)..." % ph._table)
        g.part_init(DEVICE, ph._table)
        if ph._table == "gpt":
            g.part_set_disk_guid(DEVICE, self._uuid_for("disk"))
        target_arch = self.source.spec["distribution"]["architecture"]
        partitions_by_label = {p["label"]: p for p in ph.partitions}
        hash_part_for = {p["verity-for"]: p for p in ph.partitions
                         if p["type"] == VERITY_HASH_TYPE}
        part_devices = {}
        part_index = {}
        index = 1
        for part in ph.partitions:
            dev = self._partition_device(
                g, ph._table, part, index, target_arch, partitions_by_label)
            part_devices[id(part)] = dev
            part_index[id(part)] = index
            if part["_lvm"]:
                g.pvcreate(dev)
            elif part["type"] != VERITY_HASH_TYPE:
                self._mkfs(g, part, dev)
            index = index + 1
        return part_devices, part_index, hash_part_for

    def _create_volumes(self, g, ph, part_devices):
        for group in ph.groups:
            pvs = [part_devices[id(p)] for p in ph.partitions
                   if p["_lvm"] and p.get("group") == group]
            if not pvs:
                raise RuntimeError("no physical volume found for LVM group '%s'!" % group)
            g.vgcreate(group, pvs)

        vol_devices = {}
        for vol in ph.volumes:
            g.lvcreate(vol["label"], vol["group"], ph._to_rounded_mib(vol["_size"]))
            voldev = "/dev/%s/%s" % (vol["group"], vol["label"])
            self._mkfs(g, vol, voldev)
            vol_devices[id(vol)] = voldev
        return vol_devices
