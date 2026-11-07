# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import os

from seine.imager.appliance import ARCH_INFO

# The Runway store is mounted at /boot by its FUSE driver.
RUNWAY_FSTAB_LINE = ("auto /boot runway "
                     "rw,nosuid,nodev,allow_other,default_permissions 0 0\n")

# ext2 must load explicitly: part_gpt/fat come built into core.img,
# but ext2 does not, and we need it to read another group's /boot.
GRUB_CFG_HEADER = """insmod part_gpt
insmod fat
insmod ext2
set timeout=5
set default=0

"""


class Bootloader:
    def __init__(self, device, arch=None):
        self.device = device
        self.arch = arch

    def detect(self, g):
        raise NotImplementedError

    def install(self, g, esp_mount, **opts):
        raise NotImplementedError

    def add_entry(self, g, group_label=None, kernel=None, initrd=None,
                  root_partuuid=None, **opts):
        raise NotImplementedError

    # Paths under 'esp_mount' to sign with the disk's secure-boot key.
    # Empty unless a subclass installs its own binaries.
    def paths_to_sign(self, esp_mount):
        return []


class GrubBootloader(Bootloader):
    def detect(self, g):
        return g.is_file("/usr/sbin/grub-install")

    # No 'boot_directory' means grub-install's default: the mounted
    # root's own /boot. A 'multiconfig:' build passes the ESP instead.
    # 'removable' has grub-install write EFI/boot/bootx64.efi itself,
    # for a root without the /etc/default/grub that names its own path.
    def install(self, g, esp_mount, **opts):
        boot_directory = opts.get("boot_directory")
        removable = opts.get("removable")
        options = ""
        if g.is_dir("/usr/lib/grub/x86_64-efi"):
            options = "--target x86_64-efi --efi-directory=%s" % esp_mount
            if boot_directory:
                options += " --boot-directory=%s" % boot_directory
            if removable:
                options += " --removable"
        g.sh("grub-install %s %s" % (options, self.device))
        if g.is_dir("/usr/lib/grub/x86_64-efi") and not removable:
            g.mkdir_p("%s/EFI/boot" % esp_mount)
            g.mv("%s/EFI/debian/grubx64.efi" % esp_mount,
                 "%s/EFI/boot/bootx64.efi" % esp_mount)
        if boot_directory:
            self._cfg_path = "%s/grub/grub.cfg" % boot_directory
            g.write(self._cfg_path, GRUB_CFG_HEADER.encode())

    # No 'root_partuuid' keeps update-grub's normal auto-discovery. A
    # 'multiconfig:' group passes one instead: grub finds the partition
    # by label (its 'search' has no '--part-uuid' option).
    def add_entry(self, g, group_label=None, kernel=None, initrd=None,
                  root_partuuid=None, cmdline="", **opts):
        if root_partuuid is None:
            g.sh("update-grub")
            return
        entry = (
            "menuentry '%s' {\n"
            "    search --no-floppy --set=root --label %s\n"
            "    linux %s root=PARTUUID=%s ro%s\n"
            "    initrd %s\n"
            "}\n\n"
        ) % (group_label, opts["root_label"], kernel, root_partuuid,
             " %s" % cmdline if cmdline else "", initrd)
        g.write_append(self._cfg_path, entry.encode())

    # Only the EFI target moves a binary onto the ESP; the caller
    # checks it exists, so a BIOS-only install is a no-op here.
    def paths_to_sign(self, esp_mount):
        if self.arch == "arm64":
            return [f"{esp_mount}/EFI/boot/bootaa64.efi"]
        return [f"{esp_mount}/EFI/boot/bootx64.efi"]


# A Unified Kernel Image needs no boot entry: the Boot Loader
# Specification finds '/boot/EFI/Linux/*.efi' on its own.
class SystemdBootBootloader(Bootloader):
    def detect(self, g):
        return g.is_file("/usr/bin/bootctl")

    def install(self, g, esp_mount, **opts):
        # An ostree image passes boot_path=None: the boot files sit on
        # the ESP, and bootctl refuses a /boot that is no file system root.
        boot_path = opts.get("boot_path", "/boot")
        g.sh("bootctl install --esp-path=%s%s" % (
            esp_mount, " --boot-path=%s" % boot_path if boot_path else ""))
        # A real random seed, freshly generated every build -- every
        # device flashed from this same image would share it. Drop it;
        # systemd-boot makes its own the first time it actually boots.
        seed = "%s/loader/random-seed" % esp_mount
        if g.is_file(seed):
            g.rm(seed)

    def add_entry(self, g, **opts):
        pass

    # 'bootctl install' writes both the removable fallback path and its
    # own, neither signed on its own.
    def paths_to_sign(self, esp_mount):
        if self.arch == "arm64":
            return [f"{esp_mount}/EFI/BOOT/BOOTAA64.EFI",
                    f"{esp_mount}/EFI/systemd/systemd-bootaa64.efi"]
        return [f"{esp_mount}/EFI/BOOT/BOOTX64.EFI",
                f"{esp_mount}/EFI/systemd/systemd-bootx64.efi"]


# Never detected: an image opts in with 'ostree: bootloader: runway'.
# The ESP only carries RUNWAY.EFI. The UKIs live in the Runway store.
class RunwayBootloader(Bootloader):
    def detect(self, g):
        return False

    def _boot_name(self):
        return "BOOTAA64.EFI" if self.arch == "arm64" else "BOOTX64.EFI"

    def install(self, g, esp_mount, **opts):
        src = runway_path(g, self.arch or "amd64", "RUNWAY.EFI")
        if src is None:
            raise RuntimeError("RUNWAY.EFI not found in the root file-system")
        for dst in (f"{esp_mount}/EFI/BOOT/{self._boot_name()}",
                    f"{esp_mount}/EFI/RUNWAY/RUNWAY.EFI"):
            g.mkdir_p(os.path.dirname(dst))
            g.cp(src, dst)

    def add_entry(self, g, **opts):
        pass

    def paths_to_sign(self, esp_mount):
        return [f"{esp_mount}/EFI/BOOT/{self._boot_name()}",
                f"{esp_mount}/EFI/RUNWAY/RUNWAY.EFI"]


# Where the root file-system keeps a Runway file or directory, if it does.
def runway_path(g, arch, name):
    triplet = ARCH_INFO[arch]["triplet"]
    for directory in (f"/usr/lib/{triplet}/runway", "/usr/lib/runway"):
        path = f"{directory}/{name}"
        if g.is_file(path) or g.is_dir(path):
            return path
    return None


REGISTRY = [GrubBootloader, SystemdBootBootloader]


def detect(g, device, arch=None):
    for cls in REGISTRY:
        bootloader = cls(device, arch=arch)
        if bootloader.detect(g):
            return bootloader
    return None
