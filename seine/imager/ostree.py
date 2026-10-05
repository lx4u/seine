# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import posixpath
import tarfile

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
# 'ostree admin deploy' accept.
def sanitize(g, root):
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

def deploy(g, stateroot, ref):
    _ostree(g, "admin", "deploy", "--sysroot=%s" % SYSROOT,
            "--os=%s" % stateroot, ref)
