# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# Reads and rewrites an already-built .deb in place: the shared ar/tar
# plumbing behind post-build signing (kmod_sign.py, uki_sign.py) and
# any future step that needs to patch a binary package after the fact.

import gzip
import hashlib
import io
import lzma
import os
import tarfile

# The xz header's integrity-check type (low 4 bits of byte 7) must
# match the original's, not lzma's CHECK_CRC64 default -- some
# decompressors only understand a few check types.
def _xz_check(data):
    return data[7] & 0x0F

# gzip's own timestamp field is zeroed ('mtime=0') so recompression
# stays deterministic across runs; xz carries no such field.
def decompress(name, data):
    if name.endswith(".gz"):
        return gzip.decompress(data), ".gz", None
    if name.endswith(".xz"):
        return lzma.decompress(data), ".xz", _xz_check(data)
    # Bare (no compression suffix): passed through as-is.
    return data, "", None

def compress(suffix, data, check=None):
    if suffix == ".gz":
        return gzip.compress(data, compresslevel=9, mtime=0)
    if suffix == ".xz":
        return lzma.compress(data, format=lzma.FORMAT_XZ, preset=6,
                              check=check)
    return data

def ar_read(path):
    with open(path, "rb") as f:
        data = f.read()
    if data[:8] != b"!<arch>\n":
        raise ValueError("'%s' is not a .deb: no ar magic" % path)
    pos = 8
    members = []
    while pos < len(data):
        header = data[pos:pos + 60]
        if len(header) < 60:
            break
        name = header[0:16].decode().strip()
        mtime = header[16:28].decode().strip()
        uid = header[28:34].decode().strip()
        gid = header[34:40].decode().strip()
        mode = header[40:48].decode().strip()
        size = int(header[48:58].decode().strip())
        pos += 60
        content = data[pos:pos + size]
        pos += size + (size % 2)
        members.append([name, mtime, uid, gid, mode, content])
    return members

def ar_write(path, members):
    with open(path, "wb") as f:
        f.write(b"!<arch>\n")
        for name, mtime, uid, gid, mode, content in members:
            f.write(("%-16s%-12s%-6s%-6s%-8s%-10d`\n"
                    % (name, mtime, uid, gid, mode, len(content))).encode())
            f.write(content)
            if len(content) % 2 == 1:
                f.write(b"\n")

def tar_member(members, prefix):
    for i, (name, *_rest) in enumerate(members):
        if name.startswith(prefix):
            return i
    raise ValueError("no '%s*' member -- not a Debian binary package" % prefix)

def deb_member(members, prefix):
    i = tar_member(members, prefix)
    return members[i][0], members[i][5]

def repatch_md5sums(control_tar_bytes, changed):
    src = tarfile.open(fileobj=io.BytesIO(control_tar_bytes), mode="r:")
    out = io.BytesIO()
    dst = tarfile.open(fileobj=out, mode="w:", format=tarfile.GNU_FORMAT)
    for info in src.getmembers():
        content = src.extractfile(info).read() if info.isfile() else None
        if info.isfile() and info.name in ("md5sums", "./md5sums"):
            lines = []
            for line in content.decode().splitlines():
                md5, _, path = line.partition("  ")
                lines.append("%s  %s" % (changed.get(path, md5), path))
            content = ("\n".join(lines) + "\n").encode()
            info.size = len(content)
        dst.addfile(info, io.BytesIO(content) if content is not None else None)
    dst.close()
    return out.getvalue()

# A repack changes a .deb's bytes after dpkg-buildpackage already
# described the old ones in .changes; that file gets clearsigned next
# (packages.py._deploy), so its hashes must match first.
def patch_changes(changes_path, output_dir, filenames):
    if len(filenames) == 0:
        return
    digests = {}
    for name in filenames:
        with open(os.path.join(output_dir, name), "rb") as f:
            data = f.read()
        digests[name] = {
            "size": len(data),
            "md5": hashlib.md5(data).hexdigest(),
            "sha1": hashlib.sha1(data).hexdigest(),
            "sha256": hashlib.sha256(data).hexdigest(),
        }

    with open(changes_path, "r") as f:
        lines = f.readlines()

    section = None
    for i, line in enumerate(lines):
        if line[:1] not in (" ", "\t"):
            section = line.split(":", 1)[0]
            continue
        parts = line.split()
        if len(parts) == 0:
            continue
        filename = parts[-1]
        digest = digests.get(filename)
        if digest is None:
            continue
        if section == "Checksums-Sha1":
            lines[i] = " %s %d %s\n" % (digest["sha1"], digest["size"], filename)
        elif section == "Checksums-Sha256":
            lines[i] = " %s %d %s\n" % (digest["sha256"], digest["size"], filename)
        elif section == "Files":
            _, _, category, priority, _ = parts
            lines[i] = " %s %d %s %s %s\n" % (
                digest["md5"], digest["size"], category, priority, filename)

    with open(changes_path, "w") as f:
        f.writelines(lines)
