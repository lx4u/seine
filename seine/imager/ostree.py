# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import os
import posixpath
import shlex
import tarfile

from seine.imager.appliance import DEVICE
from seine.imager.appliance import STAGE_DEVICE
from seine.imager.bootloader import detect as detect_bootloader
from seine.imager.bootloader import GrubBootloader

# What an ostree sysroot needs from the rootfs: the tool, its initramfs
# program and the dracut module that puts it in the initramfs.
REQUIRED = (
    "usr/bin/dracut",
    "usr/bin/ostree",
    "usr/lib/ostree/ostree-prepare-root",
    "usr/lib/dracut/modules.d/98ostree/module-setup.sh",
)

# Only dracut has an ostree module, so initramfs-tools must not be there.
FORBIDDEN = "usr/sbin/update-initramfs"

# Packages usually come from a playbook, not 'packages:', so this can
# only be checked once the rootfs tarball exists.
def check_tarball(tarball, what):
    with tarfile.open(tarball) as tar:
        names = {m.name.lstrip("./") for m in tar.getmembers()}
    missing = [path for path in REQUIRED if path not in names]
    if missing:
        raise ValueError(
            "%s cannot become an ostree sysroot: its root file-system "
            "lacks %s -- install 'dracut', 'ostree' and 'ostree-boot'"
            % (what, ", ".join("'/%s'" % path for path in missing)))
    if FORBIDDEN in names:
        raise ValueError(
            "%s cannot become an ostree sysroot: its root file-system has "
            "initramfs-tools, and ostree needs dracut instead" % what)

USRMERGE = ("bin", "sbin", "lib", "lib64")

# Paths the commit turns into links into /var: the link target and the
# mode of the /var directory tmpfiles.d creates for it.
VAR_LINKS = (
    ("home", "var/home", "0755"),
    ("opt", "var/opt", "0755"),
    ("srv", "var/srv", "0755"),
    ("root", "var/roothome", "0700"),
    ("usr/local", "../var/usrlocal", "0755"),
    ("mnt", "var/mnt", "0755"),
)

TMPFILES = "usr/lib/tmpfiles.d/seine-ostree.conf"
FACTORY = "usr/share/factory"

def _has_files(g, path):
    return any(not g.is_dir(posixpath.join(path, name)) for name in g.find(path))

# Reshapes the tree unpacked under 'root' into what 'ostree commit' and
# 'ostree admin deploy' accept. 'mounts' are the guest prefixes of the
# partitions to be mounted ('/efi/', '/var/home/', ...): each needs its
# mount point in the commit.
def sanitize(g, root, mounts=()):
    def at(*parts):
        return posixpath.join(root, *parts)

    for name in USRMERGE:
        if g.is_symlink(at(name)):
            continue
        if g.exists(at(name)):
            raise RuntimeError(
                "'/%s' is a directory: ostree needs a usr-merged root "
                "file-system" % name)
        g.ln_s("usr/%s" % name, at(name))

    g.mkdir_p(at("sysroot"))
    g.ln_s("sysroot/ostree", at("ostree"))

    tmpfiles = []
    for path, target, mode in VAR_LINKS:
        var = posixpath.basename(target)
        if g.is_dir(at(path)) and not g.is_symlink(at(path)) \
                and _has_files(g, at(path)):
            # Content seeds the new /var on first boot, never overwrites.
            seed = at(FACTORY, "var", var)
            g.mkdir_p(posixpath.dirname(seed))
            g.mv(at(path), seed)
            tmpfiles.append("C /var/%s - - - - /%s/var/%s" % (var, FACTORY, var))
        else:
            if g.is_symlink(at(path)) or g.exists(at(path)):
                g.rm_rf(at(path))
            tmpfiles.append("d /var/%s %s root root -" % (var, mode))
        g.ln_s(target, at(path))
    if g.is_symlink(at("tmp")) or g.exists(at("tmp")):
        g.rm_rf(at("tmp"))
    g.ln_s("sysroot/tmp", at("tmp"))

    tmpfiles += _mount_points(g, at, mounts, tmpfiles)

    g.mkdir_p(at(posixpath.dirname(TMPFILES)))
    g.write(at(TMPFILES), ("\n".join(tmpfiles) + "\n").encode())

    # Defaults live in /usr/etc, deploy merges them into each /etc.
    if g.is_dir(at("etc")) and not g.is_symlink(at("etc")):
        if g.exists(at("usr/etc")):
            raise RuntimeError("both '/etc' and '/usr/etc' exist")
        g.mv(at("etc"), at("usr/etc"))

    # Every device makes its own on first boot.
    keys = [n for n in (g.ls(at("usr/etc/ssh")) if g.is_dir(at("usr/etc/ssh")) else [])
            if n.startswith("ssh_host_") and not n.endswith(".pub")]
    if keys:
        raise RuntimeError(
            "the root file-system ships SSH host keys (%s), which every "
            "device would share" % ", ".join(sorted(keys)))
    g.write(at("usr/etc/machine-id"), b"")

    _relocate_kernel(g, at)

# /var is the stateroot's and is seeded once, so a mount point below it is
# made by tmpfiles; any other lives in the commit. Returns tmpfiles lines.
def _mount_points(g, at, mounts, tmpfiles):
    lines = []
    for prefix in mounts:
        path = prefix.rstrip("/")
        if path in ("", "/var", "/boot"):
            continue
        if path.startswith("/var/"):
            line = "d %s 0755 root root -" % path
            if line not in tmpfiles and line not in lines:
                lines.append(line)
        else:
            g.mkdir_p(at(path.lstrip("/")))
    return lines

# 'deploy' only looks for the kernel and initramfs next to the modules.
def _relocate_kernel(g, at):
    entries = g.ls(at("boot")) if g.is_dir(at("boot")) else []
    kernels = [e[len("vmlinuz-"):] for e in entries if e.startswith("vmlinuz-")]
    if len(kernels) != 1:
        raise RuntimeError(
            "expected exactly one kernel under '/boot', found %d" % len(kernels))
    version = kernels[0]
    initrd = "initrd.img-%s" % version
    if initrd not in entries:
        raise RuntimeError("no '/boot/%s' next to the kernel" % initrd)
    modules = at("usr/lib/modules", version)
    g.mkdir_p(modules)
    g.mv(at("boot", "vmlinuz-%s" % version), "%s/vmlinuz" % modules)
    g.mv(at("boot", initrd), "%s/initramfs.img" % modules)
    # Debian's links into /boot would dangle now.
    for link in ("vmlinuz", "vmlinuz.old", "initrd.img", "initrd.img.old"):
        if g.is_symlink(at(link)):
            g.rm_rf(at(link))

SYSROOT = "/sysroot"
OSTREE = "/usr/bin/ostree"

# Both stay under /sysroot, which the commit skips, and are removed
# right after it.
SKIP_LIST = "/sysroot/.seine-skip"
SKELETON = "/sysroot/.seine-skeleton"

# Directories the skip list drops from the commit but the initramfs
# needs (ostree-prepare-root moves /sysroot).
SKIPPED = ("/sysroot", "/proc", "/dev", "/sys", "/run", "/lost+found")

# Where the guest mounts a partition meant for 'prefix' ('/var/', ...).
# /var is the stateroot's, so deploy seeds the partition and not the
# sysroot.
def target_path(prefix, stateroot):
    path = prefix.rstrip("/")
    if path == "/var" or path.startswith("/var/"):
        return "%s/ostree/deploy/%s%s" % (SYSROOT, stateroot, path)
    return SYSROOT + path

def _ostree(g, *args):
    return g.command([OSTREE] + list(args))

# '/sysroot' is the physical sysroot, already mounted.
def init_sysroot(g, stateroot):
    _ostree(g, "admin", "init-fs", SYSROOT)
    _ostree(g, "admin", "os-init", "--sysroot=%s" % SYSROOT, stateroot)
    # With a grub.cfg present, 'deploy' runs grub-mkconfig and fails. The
    # boot configuration is written by the imager, and by an updater later.
    _ostree(g, "config", "--repo=%s/ostree/repo" % SYSROOT, "set",
            "sysroot.bootloader", "none")

# Commits the staged root ('/'), whatever is mounted under it aside.
# Returns the commit checksum.
def commit(g, ref, epoch):
    g.write(SKIP_LIST, ("\n".join(SKIPPED) + "\n").encode())
    for name in ("sysroot", "dev", "proc", "sys", "run"):
        directory = "%s/%s" % (SKELETON, name)
        g.mkdir_p(directory)
        g.chmod(0o755, directory)
    checksum = _ostree(
        g, "--repo=%s/ostree/repo" % SYSROOT, "commit", "-b", ref,
        "--tree=dir=/", "--tree=dir=%s" % SKELETON,
        "--skip-list=%s" % SKIP_LIST, "--timestamp=@%d" % epoch,
        "--no-bindings").strip()
    g.rm_rf(SKIP_LIST)
    g.rm_rf(SKELETON)
    return checksum

# 'kargs' end up in the boot entry, and a later deploy inherits them.
def deploy(g, stateroot, ref, kargs=()):
    _ostree(g, "admin", "deploy", "--sysroot=%s" % SYSROOT,
            "--os=%s" % stateroot, *["--karg=%s" % k for k in kargs], ref)

# 'deploy' makes each deployment root immutable, and the timestamp pass
# that follows (touch) cannot change an immutable directory. The mke2fs
# rebuild would drop the flag anyway; ostree sets it on the next deploy.
def unlock_deployments(g, stateroot):
    base = "%s/ostree/deploy/%s/deploy" % (SYSROOT, stateroot)
    for name in g.ls(base):
        if not name.endswith(".origin"):
            g.set_e2attrs("%s/%s" % (base, name), "i", clear=True)

# The ESP, mounted next to the sysroot's own mounts.
ESP_PREFIX = "/efi/"

# One 'key value' per line of a Boot Loader Specification entry.
def parse_entry(text):
    fields = {}
    for line in text.splitlines():
        key, _, value = line.partition(" ")
        if key and not key.startswith("#"):
            fields[key] = value.strip()
    return fields

# The boot entries 'deploy' wrote, one per deployment.
def read_entries(g):
    base = "%s/boot/loader/entries" % SYSROOT
    return [parse_entry(g.cat("%s/%s" % (base, name)))
            for name in sorted(g.ls(base)) if name.endswith(".conf")]

# GRUB menu entries for the sysroots in 'groups' (the boot owner first):
# each has its 'label', the 'uuid' of the file system that holds its boot
# files, their 'boot' directory ('/boot', or '' on a partition of its
# own) and its 'entries'. The paths of an entry are relative to that
# file system's /boot.
def grub_menuentries(groups):
    several = len(groups) > 1
    text = ""
    for group in groups:
        for entry in group["entries"]:
            title = entry["title"].replace("'", "")
            if several:
                title = "%s: %s" % (group["label"], title)
            text += (
                "menuentry '%s' {\n"
                "    search --no-floppy --fs-uuid --set=root %s\n"
                "    linux %s%s %s\n"
                "    initrd %s%s\n"
                "}\n\n"
            ) % (title, group["uuid"], group["boot"], entry["linux"],
                 entry["options"], group["boot"], entry["initrd"])
    return text

# The ostree flow of an Imager: unpack, commit, deploy and boot a sysroot.
# Part of Imager, like PartitionRebuild: it relies on the imager's own
# helpers (fstab, kernel arguments, boot loader signing, timestamps).
class OstreeSysroot:
    # Unpacks the rootfs onto the stage disk, which becomes the chroot
    # the target's own ostree runs from, then commits and deploys it
    # into the real sysroot mounted under '/sysroot'. Leaves everything
    # mounted, timestamps normalized, and returns the mount devices.
    def _deploy_ostree(self, g, ph, source, mounts, part_devices, vol_devices,
                       part_index, container_devices, boot_owner, boot_entries):
        if container_devices:
            raise NotImplementedError(
                "'containers:' cannot be loaded into an ostree sysroot yet")
        settings = ph.ostree_for(source)
        stateroot = settings["stateroot"]
        distro = self.source.spec["distribution"] if source is None else \
            self.source.subbuilds[source].spec["distribution"]
        ref = settings.get("ref") or "%s/%s" % (stateroot, distro["architecture"])
        print("Staging root file-system for ostree '%s'..." % ref)
        g.mkfs("ext4", STAGE_DEVICE, features="^dir_index")
        g.mount(STAGE_DEVICE, "/")
        g.tar_in_opts(self.source._tarball_for(source), "/", xattrs=True)

        mount_devices = {id(m): part_devices.get(id(m)) or vol_devices.get(id(m))
                         for m in mounts}
        root = next(m for m in mounts if m["_prefix"] == "/")
        # Both read '/etc', which sanitize() moves to '/usr/etc'. fstab
        # holds the physical mounts only: ostree mounts the root itself.
        others = [m for m in mounts if m is not root]
        kargs = ["root=PARTUUID=%s" % self._partuuid(g, part_index, root), "rw"]
        kargs += shlex.split(self._grub_cmdline(g))
        self._write_fstab(g, others, mount_devices, part_index)
        sanitize(g, "/", [m["_prefix"] for m in others])

        self._guest_paths = {id(m): target_path(m["_prefix"], stateroot)
                             for m in mounts}
        g.mount(mount_devices[id(root)], SYSROOT)
        init_sysroot(g, stateroot)
        for m in mounts:
            if m is root:
                continue
            path = self._guest_paths[id(m)]
            if not g.is_dir(path):
                g.mkdir_p(os.path.dirname(path))
                g.mkmountpoint(path)
            g.mount(mount_devices[id(m)], path)

        print("Committing and deploying...")
        checksum = commit(g, ref, self.source._epoch())
        deploy(g, stateroot, ref, kargs)
        print("  %s %s" % (ref, checksum))
        self._boot_ostree(g, source, mounts, mount_devices, stateroot,
                          distro, boot_owner, boot_entries)
        unlock_deployments(g, stateroot)
        self._normalize_mount_timestamps(g, mounts, mount_devices)
        return mount_devices

    # Records this sysroot's boot entries. The boot owner (last) installs
    # the boot loader and writes every group's menu on the shared ESP,
    # as _install_boot_entry() does for a plain layout.
    def _boot_ostree(self, g, source, mounts, mount_devices, stateroot,
                     distro, boot_owner, boot_entries):
        root = next(m for m in mounts if m["_prefix"] == "/")
        boot = next((m for m in mounts if m["_prefix"] == "/boot/"), None)
        boot_entries.append({
            "label": stateroot,
            "uuid": g.vfs_uuid(mount_devices[id(boot or root)]),
            "boot": "" if boot else "/boot",
            "entries": read_entries(g),
        })
        if source is not None and source != boot_owner:
            return
        bootloader = detect_bootloader(g, DEVICE)
        if bootloader is None:
            return
        if distro["architecture"] != "amd64":
            raise NotImplementedError(
                "booting an ostree image is only supported on amd64 for now "
                "(this one is '%s')" % distro["architecture"])
        if not isinstance(bootloader, GrubBootloader):
            raise NotImplementedError(
                "booting an ostree image with systemd-boot is not supported yet")
        esp = next((m for m in mounts if m["_prefix"] == ESP_PREFIX), None)
        if esp is None or not g.is_dir("/usr/lib/grub/x86_64-efi"):
            raise RuntimeError(
                "booting an ostree image needs an '%s' partition and "
                "grub-efi-amd64 in the root file-system" % ESP_PREFIX.rstrip("/"))
        print("Installing boot loader and entries...")
        esp_path = self._guest_paths[id(esp)]
        bootloader.install(g, esp_path, boot_directory=esp_path, removable=True)
        ordered = [boot_entries[-1]] + boot_entries[:-1]
        g.write_append("%s/grub/grub.cfg" % esp_path,
                       grub_menuentries(ordered).encode())
        self._sign_bootloader_files(g, bootloader, esp_path)
