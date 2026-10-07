# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import os

from seine           import packages
from seine.bootstrap import Bootstrap
from seine.container import ContainerEngine
from seine.utils import APT_CLEANUP
from seine.utils import feed_auth_entries
from seine.utils import IMAGER_KIND
from seine.utils import NETRC_MOUNT
from seine.utils import netrc_for
from seine.utils import release_feeds

# The disks the appliance boots with, as the guest sees them: the image
# being built, and a scratch disk for the ext and FAT rebuilds, which an
# ostree build also unpacks its root file system onto.
DEVICE = "/dev/sda"
SCRATCH_DEVICE = "/dev/sdb"

# Fallback kernel per arch when the spec has no 'imager: kernel:'.
DEFAULT_PACKAGES = {
    "amd64": "linux-image-amd64",
    "arm64": "linux-image-arm64",
    "armhf": "linux-image-armmp",
    "i386":  "linux-image-686",
}

# Files libguestfs expects in a LIBGUESTFS_PATH "fixed appliance" directory.
APPLIANCE_FILES = ["kernel", "initrd", "root", "README.fixed"]

# Tag segment for a kernel package: 'linux-image-amd64' -> 'amd64'.
def kernel_slug(package):
    prefix = "linux-image-"
    return package[len(prefix):] if package.startswith(prefix) else package

# Debian multiarch triplet and supermin --host-cpu value per architecture.
ARCH_INFO = {
    "amd64": {"triplet": "x86_64-linux-gnu",   "host_cpu": "x86_64"},
    "arm64": {"triplet": "aarch64-linux-gnu",  "host_cpu": "aarch64"},
    "armhf": {"triplet": "arm-linux-gnueabihf", "host_cpu": "armv7l"},
    "i386":  {"triplet": "i386-linux-gnu",      "host_cpu": "i686"},
}

# Run as container commands, not extracted like BINARIES below.
APT_PACKAGES = ["squashfs-tools", "erofs-utils", "binutils", "sbsigntool",
                "cryptsetup-bin", "mtools", "e2fsprogs", "findutils", "efibootguard"]
# Minimal runtime tools required in the shipped appliance container image for
# UKI anchoring and signing (imager/imager.py runs objcopy, ukify, and sbsign as
# container commands against this image) and for the update payload's ostree
# repo (imager/payload.py).
RUNTIME_APT_PACKAGES = ["binutils", "sbsigntool", "libfaketime", "ostree", "efibootguard"]
# Needed inside the built appliance itself (LVM_WRAPPER_SCRIPT's
# interpreter and LD_PRELOAD library). Listed both here, so supermin can
# resolve them, and in its own hint directory, so it bundles them in.
EXTRA_APPLIANCE_PACKAGES = ["libfaketime", "python3"]
BINARIES = [
    "/usr/bin/mksquashfs", "/usr/bin/mkfs.erofs",
    # Rebuilds a verity hash tree, see imager/imager.py's _build_verity().
    "/usr/sbin/veritysetup",
    # Rebuild a FAT partition deterministically, see
    # imager/imager.py's _normalize_fat_tree().
    "/usr/bin/mformat", "/usr/bin/mcopy", "/usr/bin/mmd",
    # Rebuild an ext2/3/4 partition deterministically, see
    # imager/imager.py's _normalize_ext_mount().
    "/usr/sbin/mke2fs", "/usr/sbin/debugfs",
    # Capture a mount's content deterministically, see
    # imager/imager.py's _normalize_ext_mount().
    "/usr/bin/cp", "/usr/bin/mkdir", "/usr/bin/find", "/usr/bin/touch",
    "/usr/bin/xargs",
]

# UKI needs systemd 257; not available for bookworm.
UKI_APT_PACKAGES = ["systemd-ukify", "systemd-boot-efi"]

CONTAINER_KMOD_HOSTFILES = [
    "/usr/lib/modules/*/kernel/net/9p/*",
    "/usr/lib/modules/*/kernel/fs/9p/*",
    "/lib/modules/*/kernel/net/9p/*",
    "/lib/modules/*/kernel/fs/9p/*",
]

# Always installed, even with no containers: docker.io already pulls
# in containerd/runc/ctr, so this covers both container targets.
DOCKER_APT_PACKAGES = [
    "docker.io", "containerd", "runc", "iptables"
]
DOCKER_SUPERMIN_PACKAGES = ["docker.io", "containerd", "runc"]
DOCKER_HOSTFILES = [
    "/usr/bin/docker",
    "/usr/sbin/dockerd",
    "/usr/bin/containerd",
    "/usr/bin/containerd-shim-runc-v2",
    "/usr/bin/runc",
    "/usr/bin/ctr",
    "/usr/bin/bbolt-normalize",
] + CONTAINER_KMOD_HOSTFILES
CONTAINER_MEMSIZE = 2048

# supermin defaults to a 4GB ext2 appliance regardless of actual content;
# measured usage is ~730MB, so this leaves real headroom without the size.
APPLIANCE_SIZE = "1G"

# The appliance only ever boots inside libguestfs's own qemu VM, so real-
# hardware driver categories are dead weight. Filesystem drivers stay.
# 'scsi' is pruned separately below (KEPT_SCSI_MODULES), not dropped whole.
PRUNED_DRIVERS = [
    "net", "gpu", "media", "infiniband", "usb", "iio", "staging",
    "hwmon", "hid", "comedi", "bluetooth", "mtd", "watchdog", "nfc", "isdn",
    "atm", "pcmcia", "firewire", "thunderbolt", "w1", "soundwire",
    "memstick", "gnss",
]

# drivers/scsi files an appliance boot actually insmods; the rest are
# real-HBA or other-hypervisor drivers.
KEPT_SCSI_MODULES = ["scsi_mod.ko", "scsi_common.ko", "sd_mod.ko", "virtio_scsi.ko"]

# One appliance per (source, release, architecture, kernel). Images
# that share those four values share one cached appliance, instead of
# rebuilding it each time an image's container settings differ.
class ImagerAppliance(Bootstrap):
    kind = IMAGER_KIND

    def __init__(self, source):
        self.source = source
        self.keep = source.options["keep"]
        distro = source.spec["distribution"]
        imager_spec = source.spec.get("imager") or {}
        self.package = imager_spec.get("kernel") or DEFAULT_PACKAGES.get(distro["architecture"])
        if self.package is None:
            raise ValueError(
                "no 'imager: kernel:' package configured in the specification and no "
                "default is known for architecture '%s'" % distro["architecture"])
        self.rebuild = imager_spec.get("rebuild", "missing")
        if self.rebuild not in ("missing", "different", "always"):
            raise ValueError(
                "'imager: rebuild:' must be one of missing, different, always "
                "(got %r)" % self.rebuild)
        super().__init__(distro, source.options)

    def container_targets(self):
        targets = set()
        for c in getattr(self.source, "containers", []) or []:
            targets.add(getattr(c, "target", "docker"))
        spec_containers = (getattr(self.source, "spec", {}) or {}).get("containers")
        if isinstance(spec_containers, list):
            for c in spec_containers:
                if isinstance(c, dict):
                    targets.add(c.get("target", "docker"))
        elif isinstance(spec_containers, dict):
            sec_target = spec_containers.get("target", "docker")
            targets.add(sec_target)
            for c in spec_containers.get("images", []) or []:
                if isinstance(c, dict):
                    targets.add(c.get("target") or sec_target)

        for build in (getattr(self.source, "subbuilds", {}) or {}).values():
            for c in getattr(getattr(build, "image", None), "containers", []) or []:
                targets.add(getattr(c, "target", "docker"))
            sub_spec = (getattr(build, "spec", {}) or {}).get("containers")
            if isinstance(sub_spec, list):
                for c in sub_spec:
                    if isinstance(c, dict):
                        targets.add(c.get("target", "docker"))
            elif isinstance(sub_spec, dict):
                sec_target = sub_spec.get("target", "docker")
                targets.add(sec_target)
                for c in sub_spec.get("images", []) or []:
                    if isinstance(c, dict):
                        targets.add(c.get("target") or sec_target)

        return targets

    def has_containers(self):
        return len(self.container_targets()) > 0

    def has_docker_target(self):
        return "docker" in self.container_targets()

    def memsize(self):
        return CONTAINER_MEMSIZE if self.has_containers() else None

    def defaultName(self):
        return os.path.join("imager-appliance", self.distro["source"],
                            self.distro["release"], self.distro["architecture"],
                            kernel_slug(self.package))

    def create(self):
        arch = self.distro["architecture"]
        info = ARCH_INFO.get(arch)
        if info is None:
            raise NotImplementedError(
                "building the imager appliance for architecture "
                "'%s' is not yet supported (unknown multiarch triplet)" % arch)

        options = list(packages.build_volumes(self.distro) or [])
        from seine.containers.tools import ensure_bbolt_normalize
        bbolt_bin = ensure_bbolt_normalize(arch)
        if not bbolt_bin or not os.path.isfile(bbolt_bin):
            raise RuntimeError(
                "bbolt-normalize binary for architecture '%s' is required to "
                "normalize containerd storage but could not be built or found" % arch
            )
        import hashlib
        with open(bbolt_bin, "rb") as bf:
            bbolt_hash = hashlib.sha256(bf.read()).hexdigest()
        options += ["-v", "%s:/seine-tools:ro" % os.path.dirname(bbolt_bin)]
        bbolt_step = (
            f"RUN echo '{bbolt_hash}' > /seine-bbolt.hash && "
            "cp /seine-tools/bbolt-normalize /usr/bin/bbolt-normalize && "
            "chmod +x /usr/bin/bbolt-normalize\n\n"
        )

        container_apt_packages = (
            DOCKER_APT_PACKAGES + (["docker-cli"] if self.distro["release"] != "bookworm" else [])
        )
        apt_packages = (APT_PACKAGES + EXTRA_APPLIANCE_PACKAGES
                        + container_apt_packages
                        + (UKI_APT_PACKAGES if self.distro["release"] != "bookworm" else []))
        extra_packages = EXTRA_APPLIANCE_PACKAGES + DOCKER_SUPERMIN_PACKAGES
        hostfiles = ["/usr/sbin/.lvm-real/lvm"] + DOCKER_HOSTFILES
        runtime_packages = (
            RUNTIME_APT_PACKAGES
            + (UKI_APT_PACKAGES if self.distro["release"] != "bookworm" else [])
        )

        entries = feed_auth_entries(self.distro, entries=release_feeds(self.distro))
        with netrc_for(entries) as netrc_path:
            netrc_mount, netrc_aptopt = "", ""
            if netrc_path:
                options = options + ["--secret", "id=seine-netrc,src=%s" % netrc_path]
                netrc_mount = (" --mount=type=secret,id=seine-netrc,target=%s"
                               % NETRC_MOUNT)
                netrc_aptopt = ' -o Dir::Etc::netrc="%s"' % NETRC_MOUNT
            return self.build(
                IMAGER_APPLIANCE_SCRIPT.format(
                    base=self.source.targetBootstrap.name,
                    apt_setup=packages.apt_setup_layer(self.distro),
                    apt_cleanup=APT_CLEANUP,
                    pruned_drivers=" ".join(PRUNED_DRIVERS),
                    kept_scsi_modules=" ".join(
                        "! -name '%s*'" % m for m in KEPT_SCSI_MODULES),
                    kernel=self.package,
                    apt_packages=" ".join(apt_packages),
                    runtime_packages=" ".join(runtime_packages),
                    extra_packages=" ".join(extra_packages),
                    hostfiles=" ".join(hostfiles),
                    appliance_size=APPLIANCE_SIZE,
                    host_cpu=info["host_cpu"],
                    triplet=info["triplet"],
                    binaries=" ".join(BINARIES),
                    lvm_wrapper=LVM_WRAPPER_SCRIPT,
                    bbolt_step=bbolt_step,
                    netrc_mount=netrc_mount,
                    netrc_aptopt=netrc_aptopt),
                base=self.source.targetBootstrap.name,
                options=options,
                rebuild=self.rebuild)

    # Flat, not real paths like /usr/bin: /usr may be the mount being
    # packed away on a usrmerged system.
    def extract(self, output_dir):
        ContainerEngine.extractImage(self.name, output_dir, lambda n:
            n.startswith("appliance/") or n.startswith("extra-tools/"))

        appliance_dir = os.path.join(output_dir, "appliance")
        readme_path = os.path.join(appliance_dir, "README.fixed")
        if not os.path.isfile(readme_path):
            with open(readme_path, "w") as f:
                f.write(APPLIANCE_README)

        missing = [f for f in APPLIANCE_FILES if not os.path.isfile(os.path.join(appliance_dir, f))]
        if missing:
            raise RuntimeError(
                "fixed appliance for architecture '%s' is missing: %s"
                % (self.distro["architecture"], missing))

        tools_root = os.path.join(output_dir, "extra-tools")
        extra_tools_files = [os.path.join(dirpath, filename)
                             for dirpath, _, filenames in os.walk(tools_root)
                             for filename in filenames]
        return appliance_dir, extra_tools_files

APPLIANCE_README = """\
This is a "fixed appliance" for libguestfs, built by seine using supermin
directly (see seine/imager/appliance.py). Point LIBGUESTFS_PATH at this
directory to use it in place of libguestfs's own supermin auto-build.
"""

# lvm2 dispatches pvcreate/vgcreate/lvcreate through one 'lvm' binary
# chosen by argv[0]'s basename, and gives each a random UUID with no
# override -- this wrapper freezes the time and pins the UUIDs instead.
LVM_WRAPPER_SCRIPT = r"""#!/usr/bin/python3
import hashlib
import os
import re
import subprocess
import sys
import time

REAL = "/usr/sbin/.lvm-real/lvm"
BACKUP = "/tmp/seine-vgcfg-restore"

def cmdline(name):
    with open("/proc/cmdline") as f:
        m = re.search(r"\b%s=(\S+)" % name, f.read())
    return m.group(1) if m else None

epoch = cmdline("faketime")
seed = cmdline("seed")

if epoch:
    os.environ["TZ"] = "UTC"
    os.environ["LD_PRELOAD"] = "@LIBFAKETIME@"
    os.environ["FAKETIME"] = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(int(epoch)))

def run(*args):
    subprocess.run([REAL] + list(args), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

# A stable UUID derived from the spec's own seed plus a name, so the same
# spec always gets the same ids and two different specs never collide.
def uuid_for(key):
    digest = hashlib.md5(("%s:%s" % (seed, key)).encode()).hexdigest()
    parts = [digest[0:6], digest[6:10], digest[10:14],
             digest[14:18], digest[18:22], digest[22:26], digest[26:32]]
    return "-".join(parts)

# Replaces only the id value after "<name> {", so every other byte (and
# so every offset and checksum) stays exactly where it was.
def pin(blob, name, new_id):
    pattern = rb'(' + re.escape(name).encode() + rb' \{\s*\n[ \t]*id = ")[0-9A-Za-z-]+(")'
    return re.sub(pattern, lambda m: m.group(1) + new_id.encode() + m.group(2), blob)

def pin_ids(vg):
    run("vgcfgbackup", "-f", BACKUP, vg)
    with open(BACKUP, "rb") as f:
        text = f.read()

    device = re.search(rb'device = "([^"]+)"', text)
    pe_start = re.search(rb'pe_start = (\d+)', text)
    # A PV entry sits at the same indentation as an LV entry, so only
    # look for LV names after "logical_volumes {" -- otherwise a PV's
    # already-correct id gets overwritten with an LV-keyed one.
    after_lvs = text.split(b"logical_volumes {", 1)
    lvs = [m.decode() for m in re.findall(rb'\n\t\t([A-Za-z0-9_.+-]+) \{\n', after_lvs[1])] \
        if len(after_lvs) > 1 else []

    for name in [vg] + lvs:
        key = "vg:%s" % name if name == vg else "lv:%s:%s" % (vg, name)
        text = pin(text, name, uuid_for(key))
    with open(BACKUP, "wb") as f:
        f.write(text)
    run("vgcfgrestore", "--force", "--yes", "-f", BACKUP, vg)
    os.remove(BACKUP)

    # vgcfgrestore fixes only the live copy. lvm2 appends, never
    # overwrites, so the old unpinned copy stays in the PV area
    # and needs scrubbing too.
    if device and pe_start:
        ring_size = int(pe_start.group(1)) * 512
        with open(device.group(1), "r+b") as f:
            ring = f.read(ring_size)
            for name in [vg] + lvs:
                key = "vg:%s" % name if name == vg else "lv:%s:%s" % (vg, name)
                ring = pin(ring, name, uuid_for(key))
            f.seek(0)
            f.write(ring)

args = sys.argv[1:]
sub = args[0] if args else None

if not seed or sub not in ("pvcreate", "vgcreate", "lvcreate"):
    os.execv(REAL, [REAL] + args)

if sub == "pvcreate":
    dev = args[-1]
    os.execv(REAL, [REAL] + args + ["--uuid", uuid_for("pv:%s" % dev), "--norestorefile"])

# guestfsd always calls "vgcreate <vgname> <pvdev...>" and
# "lvcreate --yes -L <size> -n <lvname> <vgname>", so the VG name sits
# at a different argv position for each.
vg = args[1] if sub == "vgcreate" else args[-1]
rc = subprocess.run([REAL] + args).returncode
if rc == 0:
    pin_ids(vg)
sys.exit(rc)
"""

# Only vmlinuz and modules are needed, so skip the initramfs build.
#
# Split into separate RUN steps: a heredoc can't sit inside a
# backslash-continued RUN, and the moved-aside real 'lvm' binary must be
# listed as a supermin hostfile so it isn't dropped from the build.
#
# Two stages: the builder stage installs everything needed to build the
# appliance (kernel, supermin, libguestfs0, container tooling) and the
# extra host-tool binaries. The final stage keeps only the minimal runtime
# tools required by UKI anchoring and signing (objcopy, ukify, sbsign,
# libfaketime) alongside /appliance and /extra-tools, dropping build-time
# kernels, qemu, and container engines to keep the image footprint slim.
IMAGER_APPLIANCE_SCRIPT = """
FROM {base} AS builder
{apt_setup}RUN{netrc_mount} apt-get update -qqy{netrc_aptopt} && \\
    INITRD=No apt-get install -qqy{netrc_aptopt} --no-install-recommends \\
        {kernel} supermin libguestfs0 {apt_packages} && \\
    {apt_cleanup} && \\
    for d in {pruned_drivers}; do rm -rf /lib/modules/*/kernel/drivers/$d; done && \\
    find /lib/modules/*/kernel/drivers/scsi -mindepth 1 -type f \\
        {kept_scsi_modules} -delete && \\
    find /lib/modules/*/kernel/drivers/scsi -mindepth 1 -type d -empty -delete && \\
    for kdir in /lib/modules/*; do depmod -a "$(basename "$kdir")"; done && \\
    mkdir -p /appliance /extra-tools /seine-hints /usr/sbin/.lvm-real && \\
    mv /usr/sbin/lvm /usr/sbin/.lvm-real/lvm && \\
    printf '%s\\n' {hostfiles} >/seine-hints/hostfiles && \\
    printf '%s\\n' {extra_packages} >/seine-hints/packages

RUN <<'SEINE_LVM_WRAPPER' cat >/usr/sbin/lvm
{lvm_wrapper}SEINE_LVM_WRAPPER

RUN libfaketime=$(dpkg -L libfaketime | grep -E '/libfaketime\\.so\\.[0-9]+$') && \\
    sed -i "s#@LIBFAKETIME@#$libfaketime#" /usr/sbin/lvm && \\
    chmod +x /usr/sbin/lvm

{bbolt_step}RUN supermin --build --verbose --copy-kernel -f ext2 --size {appliance_size} \\
        --host-cpu {host_cpu} /usr/lib/{triplet}/guestfs/supermin.d /seine-hints -o /appliance && \\
    for bin in {binaries}; do \\
        cp --parents "$bin" /extra-tools; \\
        for lib in $(ldd "$bin" 2>/dev/null | grep -oE '/[^ ]+'); do \\
            case "$lib" in \\
                */libc.so*|*/libm.so*|*/libpthread.so*|*/librt.so*| \\
                */libdl.so*|*/libresolv.so*|*/libutil.so*|*/libnsl.so*| \\
                */ld-linux*) continue ;; \\
            esac; \\
            [ -f "$lib" ] && cp --parents "$lib" /extra-tools || true; \\
        done; \\
    done && \\
    apt-get clean

FROM {base} AS base
{apt_setup}RUN{netrc_mount} apt-get update -qqy{netrc_aptopt} && \\
    INITRD=No apt-get install -qqy{netrc_aptopt} --no-install-recommends \\
        {runtime_packages} && \\
    {apt_cleanup}
COPY --from=builder /appliance /appliance
COPY --from=builder /extra-tools /extra-tools
CMD /bin/true
"""
