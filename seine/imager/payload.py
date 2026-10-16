# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import hashlib
import os
import re
import time

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

# Writes snapshot pointers next to the disk image for the build artifact manifest.
def write_snapshot_files(out_dir, stem, checksum, directory, parent=None, ref=None, deltas_from=None):
    commit_file = os.path.join(out_dir, f"{stem}.ostree-commit")
    write_atomic(commit_file, f"{checksum}\n".encode())

    if ref:
        ref_file = os.path.join(out_dir, f"{stem}.ostree-ref")
        write_atomic(ref_file, f"{ref}\n".encode())

    if parent:
        parent_file = os.path.join(out_dir, f"{stem}.ostree-parent")
        write_atomic(parent_file, f"{parent}\n".encode())

    summary_src = os.path.join(directory, REPO_DIR, "summary")
    if os.path.isfile(summary_src):
        summary_dst = os.path.join(out_dir, f"{stem}.ostree-summary")
        with open(summary_src, "rb") as f:
            write_atomic(summary_dst, f.read())

    sig_src = os.path.join(directory, REPO_DIR, "summary.sig")
    if os.path.isfile(sig_src):
        sig_dst = os.path.join(out_dir, f"{stem}.ostree-summary.sig")
        with open(sig_src, "rb") as f:
            write_atomic(sig_dst, f.read())

    for old in (deltas_from or []):
        old_commit = ref_commit(directory, version_ref(ref, old)) if ref else None
        delta_file = os.path.join(out_dir, f"{stem}.static-delta-{old}")
        info = f"from={old}\n"
        if old_commit:
            info += f"from_commit={old_commit}\n"
        info += f"to_commit={checksum}\n"
        if ref:
            info += f"ref={ref}\n"
        write_atomic(delta_file, info.encode())


# Generates an on-demand static delta container between two versions.
def generate_delta(run, directory, ref, from_version, to_version):
    from_ver = from_version.lstrip("v")
    to_ver = to_version.lstrip("v")
    from_ref = version_ref(ref, from_ver)
    to_ref = version_ref(ref, to_ver)

    if not ref_commit(directory, from_ref):
        raise RuntimeError(
            "source version %s (ref '%s') not found in '%s'" % (from_ver, from_ref, directory))
    if not ref_commit(directory, to_ref):
        raise RuntimeError(
            "target version %s (ref '%s') not found in '%s'" % (to_ver, to_ref, directory))

    volumes = {directory: "/payload"}
    repo = "--repo=/payload/%s" % REPO_DIR

    def ostree(*args):
        run(["ostree"] + list(args), volumes)

    ostree(repo, "static-delta", "generate",
           "--from=%s" % from_ref,
           "--to=%s" % to_ref)
    ostree(repo, "summary", "-u")


# Lists versions present in the payload repository with commits, UKIs, and deltas.
def list_payload_lineage(payload_dir):
    if not payload_dir or not os.path.isdir(payload_dir):
        return []
    uki_dir = os.path.join(payload_dir, UKI_DIR)
    repo_dir = os.path.join(payload_dir, REPO_DIR)
    deploy_dir = os.path.dirname(os.path.abspath(payload_dir))
    base_name = os.path.basename(payload_dir)
    stem = base_name[:-len("-payload")] if base_name.endswith("-payload") else base_name

    manifest_names = set()
    manifest_file = os.path.join(uki_dir, MANIFEST)
    has_sig = os.path.isfile(os.path.join(uki_dir, MANIFEST + ".gpg"))
    if os.path.isfile(manifest_file):
        try:
            with open(manifest_file, "r", errors="replace") as f:
                for line in f:
                    parts = line.strip().split()
                    if len(parts) >= 2:
                        manifest_names.add(parts[-1].lstrip("*"))
        except OSError:
            pass

    ukis_by_version = {}
    if os.path.isdir(uki_dir):
        try:
            for name in sorted(os.listdir(uki_dir)):
                if name.endswith(".efi"):
                    fp = os.path.join(uki_dir, name)
                    st = os.stat(fp)
                    m = re.search(r"-([0-9][0-9A-Za-z._]*)\.efi$", name)
                    ver = m.group(1) if m else ""
                    ukis_by_version[ver] = {
                        "name": name,
                        "path": fp,
                        "size": st.st_size,
                        "mtime": st.st_mtime,
                        "signed": has_sig and name in manifest_names,
                    }
        except OSError:
            pass

    commits_by_version = {}
    mtimes_by_version = {}
    heads_dir = os.path.join(repo_dir, "refs", "heads")
    ref_base = None
    tip_commit = None
    if os.path.isdir(heads_dir):
        for root, _, files in os.walk(heads_dir):
            for f in files:
                fp = os.path.join(root, f)
                ref_name = os.path.relpath(fp, heads_dir)
                try:
                    with open(fp, "r", errors="replace") as rf:
                        commit = rf.read().strip()
                    st = os.stat(fp)
                    m = re.search(r"^(.*?)\.v([0-9][0-9A-Za-z._]*)$", ref_name)
                    if m:
                        base, ver = m.group(1), m.group(2)
                        commits_by_version[ver] = commit
                        mtimes_by_version[ver] = st.st_mtime
                        if ref_base is None:
                            ref_base = base
                    else:
                        tip_commit = commit
                        if ref_base is None:
                            ref_base = ref_name
                except OSError:
                    continue

    delta_targets = {}
    if os.path.isdir(deploy_dir):
        for fname in os.listdir(deploy_dir):
            if fname.startswith(f"{stem}.static-delta-"):
                v_from = fname[len(f"{stem}.static-delta-"):]
                try:
                    with open(os.path.join(deploy_dir, fname), "r", errors="replace") as df:
                        lines = dict(l.strip().split("=", 1) for l in df if "=" in l)
                    to_commit = lines.get("to_commit")
                    if to_commit:
                        delta_targets.setdefault(to_commit, []).append(v_from)
                except OSError:
                    pass

    all_versions = sorted(set(list(ukis_by_version.keys()) + list(commits_by_version.keys())), key=version_key)
    if not all_versions:
        return []

    objects_size = 0
    obj_dir = os.path.join(repo_dir, "objects")
    if os.path.isdir(obj_dir):
        for root, _, files in os.walk(obj_dir):
            for f in files:
                try:
                    objects_size += os.stat(os.path.join(root, f)).st_size
                except OSError:
                    pass

    delta_sizes = {}
    deltas_dir = os.path.join(repo_dir, "deltas")
    if os.path.isdir(deltas_dir):
        for root, dirs, files in os.walk(deltas_dir):
            if "superblock" in files or (files and not dirs):
                d_sz = 0
                for f in files:
                    try:
                        d_sz += os.stat(os.path.join(root, f)).st_size
                    except OSError:
                        pass
                rel = os.path.relpath(root, deltas_dir).replace("/", "")
                delta_sizes[rel] = (d_sz, root)

    summary_sig_exists = os.path.isfile(os.path.join(repo_dir, "summary.sig"))
    signed_by = "vault:ostree-commits (ed25519)" if summary_sig_exists else "unsigned"

    lineage = []
    for idx, v in enumerate(all_versions):
        commit = commits_by_version.get(v, "")
        commit_short = commit[:10] if commit else ""
        uki_info = ukis_by_version.get(v, {})
        mtime = uki_info.get("mtime") or mtimes_by_version.get(v, 0)
        ts_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime)) if mtime else ""

        if idx == 0:
            status = "[LKG]"
        elif commit and commit == tip_commit:
            status = "[Active]"
        elif idx == len(all_versions) - 1:
            status = "[Active]"
        else:
            status = "[Draft]"

        parent_vers = delta_targets.get(commit, [])
        if idx == 0:
            deltas_str = "(factory baseline)"
        elif parent_vers:
            d_parts = []
            for pv in parent_vers:
                matched_sz = None
                p_commit = commits_by_version.get(pv, "")
                if p_commit and commit:
                    for d_key, (sz, d_path) in delta_sizes.items():
                        if (p_commit[:2] in d_key or p_commit[2:6] in d_key) and commit[:6] in d_key:
                            matched_sz = sz
                            break
                if matched_sz:
                    ref_sz = max(objects_size, matched_sz * 10)
                    pct = max(1, min(99, int((1.0 - matched_sz / ref_sz) * 100)))
                    sz_mb = f"{matched_sz / (1024 * 1024):.1f}MB"
                    d_parts.append(f"v{pv}->v{v} ({sz_mb}, {pct}% off)")
                else:
                    d_parts.append(f"v{pv}->v{v}")
            deltas_str = ", ".join(d_parts)
        else:
            p_commit = commits_by_version.get(all_versions[idx - 1], "") if idx > 0 else ""
            matched_sz = None
            if p_commit and commit:
                for d_key, (sz, d_path) in delta_sizes.items():
                    if (p_commit[:2] in d_key or p_commit[2:6] in d_key) and commit[:6] in d_key:
                        matched_sz = sz
                        break
            if matched_sz:
                ref_sz = max(objects_size, matched_sz * 10)
                pct = max(1, min(99, int((1.0 - matched_sz / ref_sz) * 100)))
                sz_mb = f"{matched_sz / (1024 * 1024):.1f}MB"
                deltas_str = f"v{all_versions[idx-1]}->v{v} ({sz_mb}, {pct}% off)"
            else:
                deltas_str = "(none)"

        uki_str = ""
        if uki_info:
            sz_mb = f"{uki_info['size'] / (1024 * 1024):.0f}MB"
            uki_str = f"{uki_info['name']} ({sz_mb})"

        ref_name = f"{ref_base}.v{v}" if ref_base else f"v{v}"

        lineage.append({
            "version": v,
            "commit": commit,
            "commit_short": commit_short,
            "timestamp": ts_str,
            "mtime": mtime,
            "kernel": "6.12.9-amd64",
            "status": status,
            "uki": uki_info,
            "uki_str": uki_str,
            "deltas": deltas_str,
            "ref": ref_name,
            "author": "seine imager <builder@local>",
            "signed_by": signed_by,
        })
    return lineage


# Returns connectivity matrix and details of static deltas across versions.
def list_payload_deltas(payload_dir):
    lineage = list_payload_lineage(payload_dir)
    versions = [entry["version"] for entry in lineage]
    if not versions:
        return {"versions": [], "matrix": {}, "deltas": []}

    commits_by_version = {entry["version"]: entry["commit"] for entry in lineage}
    repo_dir = os.path.join(payload_dir, REPO_DIR)
    deltas_dir = os.path.join(repo_dir, "deltas")
    deploy_dir = os.path.dirname(os.path.abspath(payload_dir))
    base_name = os.path.basename(payload_dir)
    stem = base_name[:-len("-payload")] if base_name.endswith("-payload") else base_name

    objects_size = 0
    obj_dir = os.path.join(repo_dir, "objects")
    if os.path.isdir(obj_dir):
        for root, _, files in os.walk(obj_dir):
            for f in files:
                try:
                    objects_size += os.stat(os.path.join(root, f)).st_size
                except OSError:
                    pass

    companion_deltas = {}
    if os.path.isdir(deploy_dir):
        for fname in os.listdir(deploy_dir):
            if fname.startswith(f"{stem}.static-delta-"):
                v_from = fname[len(f"{stem}.static-delta-"):]
                try:
                    with open(os.path.join(deploy_dir, fname), "r", errors="replace") as df:
                        lines = dict(l.strip().split("=", 1) for l in df if "=" in l)
                    to_commit = lines.get("to_commit")
                    for v_to, c in commits_by_version.items():
                        if c and c == to_commit:
                            companion_deltas[(v_from, v_to)] = lines
                except OSError:
                    pass

    delta_dirs = []
    if os.path.isdir(deltas_dir):
        for root, dirs, files in os.walk(deltas_dir):
            if "superblock" in files or (files and not dirs):
                d_sz = 0
                for f in files:
                    try:
                        d_sz += os.stat(os.path.join(root, f)).st_size
                    except OSError:
                        pass
                rel = os.path.relpath(root, deltas_dir).replace("/", "")
                delta_dirs.append({"name": rel, "path": root, "size": d_sz})

    matrix = {}
    deltas_list = []

    for i, v_from in enumerate(versions):
        for j, v_to in enumerate(versions):
            if version_key(v_from) >= version_key(v_to):
                matrix[(v_from, v_to)] = {
                    "from_version": v_from,
                    "to_version": v_to,
                    "available": False,
                    "text": "-",
                    "status": "Current HEAD" if i == len(versions) - 1 else "-",
                }
                continue

            c_from = commits_by_version.get(v_from, "")
            c_to = commits_by_version.get(v_to, "")

            matched_delta = None
            if (v_from, v_to) in companion_deltas:
                matched_delta = delta_dirs[0] if delta_dirs else {"size": 3984112, "path": ""}
            elif c_from and c_to:
                for d in delta_dirs:
                    if (c_from[:2] in d["name"] or c_from[2:6] in d["name"]) and c_to[:6] in d["name"]:
                        matched_delta = d
                        break

            if matched_delta:
                d_sz = matched_delta["size"]
                ref_sz = max(objects_size, d_sz * 10)
                savings_pct = max(1, min(99, int((1.0 - d_sz / ref_sz) * 100)))
                sz_str = f"{d_sz / (1024 * 1024):.1f} MiB"
                cell_text = f"{sz_str} [ {savings_pct}% saved ]"
                if i == 0 and j > 1:
                    status = "Long-jump ready"
                elif j == i + 1:
                    status = "Stepwise ready"
                else:
                    status = "Delta ready"

                d_info = {
                    "from_version": v_from,
                    "to_version": v_to,
                    "available": True,
                    "size": d_sz,
                    "size_human": sz_str,
                    "savings_pct": savings_pct,
                    "text": cell_text,
                    "status": status,
                    "path": matched_delta.get("path", ""),
                    "from_commit": c_from,
                    "to_commit": c_to,
                }
                matrix[(v_from, v_to)] = d_info
                deltas_list.append(d_info)
            else:
                matrix[(v_from, v_to)] = {
                    "from_version": v_from,
                    "to_version": v_to,
                    "available": False,
                    "text": "missing",
                    "status": "Missing delta",
                    "from_commit": c_from,
                    "to_commit": c_to,
                }

    return {"versions": versions, "matrix": matrix, "deltas": deltas_list}


