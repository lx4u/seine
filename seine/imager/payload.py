# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import hashlib
import os
import re

from seine.container import ContainerEngine
from seine.utils import version_key

# The directory a device updates from, served as it is: 'repo/' is an
# archive ostree repo, 'uki/' holds one UKI per shipped version and their
# signed list. It lives next to the disk image and keeps the history.
REPO_DIR = "repo"
UKI_DIR = "uki"
MANIFEST = "SHA256SUMS"

# The ESP holds the booted UKI, the failed or previous one and the one
# being written, plus some room.
ESP_UKI_COUNT = 3
ESP_MARGIN = 16 * 1024 * 1024

# The directory of the payload: 'path' of the settings, else a directory
# named after the image. Relative paths resolve like 'image: filename'.
def payload_dir(image, settings, release):
    path = (settings or {}).get("path")
    if path is None:
        stem = os.path.splitext(os.path.basename(image))[0]
        return os.path.join(os.path.dirname(image), "%s-payload" % stem)
    if os.path.isabs(path):
        return path
    return os.path.join(ContainerEngine.deploy_dir(release), path)

# The name of the ref that keeps the commit of one shipped version.
def version_ref(ref, version):
    return "%s.v%s" % (ref, version)

# The commit a ref of the payload repo points at, or None.
def ref_commit(directory, ref):
    path = os.path.join(directory, REPO_DIR, "refs", "heads", ref)
    if not os.path.isfile(path):
        return None
    with open(path) as f:
        return f.read().strip()

# The versions of the UKIs in the payload, lowest first.
def uki_versions(directory, stateroot):
    pattern = re.compile(r"^%s-([0-9][0-9A-Za-z._]*)\.efi$" % re.escape(stateroot))
    names = os.listdir(os.path.join(directory, UKI_DIR)) \
        if os.path.isdir(os.path.join(directory, UKI_DIR)) else []
    versions = [m.group(1) for m in map(pattern.match, names) if m]
    return sorted(versions, key=version_key)

def uki_name(stateroot, version):
    return "%s-%s.efi" % (stateroot, version)

# Refuses what cannot be shipped, before any disk work. A same-version
# rebuild is judged later, when its commit is known.
def check_payload(directory, ref, stateroot, version, deltas_from):
    versions = uki_versions(directory, stateroot)
    if versions and version_key(versions[-1]) > version_key(version):
        raise RuntimeError(
            "version %s is older than version %s already in '%s'"
            % (version, versions[-1], directory))
    for old in deltas_from:
        if old not in versions or not ref_commit(directory, version_ref(ref, old)):
            raise RuntimeError(
                "'deltas-from: %s' is not in '%s' (needs its UKI and the "
                "ref '%s')" % (old, directory, version_ref(ref, old)))

# The ESP is checked again once the UKI exists and its size is known.
def check_esp(esp_size, uki_size):
    needed = ESP_UKI_COUNT * uki_size + ESP_MARGIN
    if esp_size < needed:
        raise RuntimeError(
            "the ESP holds %d bytes, but %d UKIs of %d bytes and some room "
            "need %d" % (esp_size, ESP_UKI_COUNT, uki_size, needed))

# The commit already shipped as 'version', which a rebuild must repeat.
def check_version(directory, ref, version, checksum):
    shipped = ref_commit(directory, version_ref(ref, version))
    if shipped not in (None, checksum):
        raise RuntimeError(
            "version %s was already shipped with another commit" % version)
    return shipped

# Adds the commit of 'export' (an archive repo from the target's own
# ostree) to the payload repo as 'ref' and 'ref.v<version>', and the
# deltas. 'run(args, volumes)' runs a command in the tools container.
def add_commit(run, directory, export, ref, checksum, version, deltas_from):
    shipped = check_version(directory, ref, version, checksum)
    os.makedirs(directory, exist_ok=True)
    volumes = {os.path.dirname(export): "/src:ro", directory: "/payload"}
    source = "/src/%s" % os.path.basename(export)
    repo = "--repo=/payload/%s" % REPO_DIR

    def ostree(*args):
        run(["ostree"] + list(args), volumes)

    ostree("--repo=%s" % source, "fsck", "--quiet")
    if not os.path.isdir(os.path.join(directory, REPO_DIR, "objects")):
        ostree(repo, "init", "--mode=archive")
    ostree(repo, "pull-local", source, ref)
    if shipped is None:
        ostree(repo, "refs", "--create=%s" % version_ref(ref, version), checksum)
    for old in deltas_from:
        ostree(repo, "static-delta", "generate",
               "--from=%s" % version_ref(ref, old),
               "--to=%s" % version_ref(ref, version))
    ostree(repo, "summary", "-u")

# The command line of a UKI without the 'ostree=' argument, which names
# the deployment and differs between builds.
def uki_cmdline(run, path, workdir):
    volumes = {os.path.dirname(path): "/uki:ro", workdir: "/out"}
    run(["objcopy", "--dump-section", ".cmdline=/out/cmdline.txt",
         "/uki/%s" % os.path.basename(path), "/out/discard.efi"], volumes)
    with open(os.path.join(workdir, "cmdline.txt")) as f:
        return [a for a in f.read().split() if not a.startswith("ostree=")]

# Copies the UKI of the build in, unless the disk layout changed since the
# newest one of the payload: a device could not boot that one.
def add_uki(run, directory, stateroot, version, uki, workdir):
    versions = uki_versions(directory, stateroot)
    if versions:
        newest = os.path.join(directory, UKI_DIR, uki_name(stateroot, versions[-1]))
        if uki_cmdline(run, uki, workdir) != uki_cmdline(run, newest, workdir):
            raise RuntimeError(
                "the disk layout changed since version %s: this needs a "
                "reflash, not an update" % versions[-1])
    target = os.path.join(directory, UKI_DIR, uki_name(stateroot, version))
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(uki, "rb") as src, open(target + ".tmp", "wb") as dst:
        dst.write(src.read())
    os.replace(target + ".tmp", target)

def write_atomic(path, data):
    with open(path + ".tmp", "wb") as f:
        f.write(data)
    os.replace(path + ".tmp", path)

# The 'sha256sum' text of every UKI, sorted. Written last, with its
# signature, so a reader in the middle of a build sees the old list.
def manifest_text(directory):
    uki_dir = os.path.join(directory, UKI_DIR)
    lines = []
    for name in sorted(n for n in os.listdir(uki_dir) if n.endswith(".efi")):
        with open(os.path.join(uki_dir, name), "rb") as f:
            lines.append("%s  %s\n" % (hashlib.file_digest(f, "sha256").hexdigest(), name))
    return "".join(lines)

def write_manifest(directory, sign):
    text = manifest_text(directory).encode()
    base = os.path.join(directory, UKI_DIR, MANIFEST)
    write_atomic(base, text)
    write_atomic(base + ".gpg", sign(text))
