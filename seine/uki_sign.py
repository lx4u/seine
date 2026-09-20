# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# Post-build UKI signing through the vault, same shape as kmod_sign.py
# and the same reason: sbuild's chroot has no network to reach a vault
# from. Simpler than kmod_sign.py -- '.efi'/'.addon.efi' ship
# uncompressed in data.tar, no per-member (de)compression to handle.

import hashlib
import io
import re
import tarfile

from seine.deb import repack

# Matches both a parent UKI ('foo.efi') and a cmdline addon
# ('foo.addon.efi').
_UKI_SUFFIX = re.compile(r"\.efi$")

def _resign_data_tar(vault, key, epoch, data_tar_bytes):
    src = tarfile.open(fileobj=io.BytesIO(data_tar_bytes), mode="r:")
    out = io.BytesIO()
    dst = tarfile.open(fileobj=out, mode="w:", format=tarfile.GNU_FORMAT)
    changed = {}
    for info in src.getmembers():
        content = src.extractfile(info).read() if info.isfile() else None
        if info.isfile() and _UKI_SUFFIX.search(info.name):
            signed = vault.sbsign_sign(key, content, epoch)
            if signed != content:
                path = info.name[2:] if info.name.startswith("./") else info.name
                changed[path] = hashlib.md5(signed).hexdigest()
                content = signed
                info.size = len(content)
        dst.addfile(info, io.BytesIO(content) if content is not None else None)
    dst.close()
    return out.getvalue(), changed

# Cheap pre-check so a build's non-UKI .debs skip the unpack entirely.
def has_uki(deb_path):
    members = repack.ar_read(deb_path)
    data_tar, _, _ = repack.decompress(*repack.deb_member(members, "data.tar"))
    with tarfile.open(fileobj=io.BytesIO(data_tar), mode="r:") as tf:
        return any(_UKI_SUFFIX.search(m.name)
                  for m in tf.getmembers() if m.isfile())

# Re-signs every '.efi'/'.addon.efi' inside deb_path with the vault
# key, in place. Returns changed paths -- empty if nothing needed it.
def resign(deb_path, vault, key, epoch):
    members = repack.ar_read(deb_path)
    data_idx = repack.tar_member(members, "data.tar")
    data_name = members[data_idx][0]
    data_tar, data_suffix, data_check = repack.decompress(
        data_name, members[data_idx][5])

    new_data_tar, changed = _resign_data_tar(vault, key, epoch, data_tar)
    if not changed:
        return set()

    control_idx = repack.tar_member(members, "control.tar")
    control_name = members[control_idx][0]
    control_tar, control_suffix, control_check = repack.decompress(
        control_name, members[control_idx][5])
    new_control_tar = repack.repatch_md5sums(control_tar, changed)

    members[data_idx][5] = repack.compress(data_suffix, new_data_tar, data_check)
    members[control_idx][5] = repack.compress(
        control_suffix, new_control_tar, control_check)
    repack.ar_write(deb_path, members)
    return set(changed)
