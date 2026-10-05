# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

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
