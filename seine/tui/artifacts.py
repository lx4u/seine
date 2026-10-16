# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# Text for the Artifacts screen and payload inspection helpers,
# kept apart from the widgets so it can be tested without a running App.

import os
import re
import time

from seine.container import ContainerEngine


def _human_size(size):
    for unit in ["B", "KiB", "MiB", "GiB", "TiB"]:
        if size < 1024 or unit == "TiB":
            return f"{size:.1f}{unit}" if unit != "B" else f"{int(size)}B"
        size /= 1024


# Inspects an OSTree update payload directory for UKIs, commits, deltas, and manifests.
def inspect_payload(payload_dir):
    if not os.path.isdir(payload_dir):
        return None
    uki_dir = os.path.join(payload_dir, "uki")
    repo_dir = os.path.join(payload_dir, "repo")
    if not (os.path.isdir(uki_dir) or os.path.isdir(repo_dir) or payload_dir.endswith("-payload")):
        return None

    total_size = 0
    latest_mtime = 0
    try:
        latest_mtime = os.stat(payload_dir).st_mtime
    except OSError:
        pass

    for root, _, files in os.walk(payload_dir):
        for f in files:
            fp = os.path.join(root, f)
            try:
                st = os.stat(fp)
                total_size += st.st_size
                if st.st_mtime > latest_mtime:
                    latest_mtime = st.st_mtime
            except OSError:
                continue

    # 1. Inspect UKIs and manifest
    manifest_info = None
    signature_info = None
    manifest_names = set()
    ukis = []
    if os.path.isdir(uki_dir):
        manifest_file = os.path.join(uki_dir, "SHA256SUMS")
        manifest_sig = os.path.join(uki_dir, "SHA256SUMS.gpg")
        if os.path.isfile(manifest_file):
            try:
                st = os.stat(manifest_file)
                manifest_info = {
                    "name": "SHA256SUMS",
                    "path": manifest_file,
                    "size": st.st_size,
                    "mtime": st.st_mtime,
                }
                with open(manifest_file, "r", errors="replace") as mf:
                    for line in mf:
                        parts = line.strip().split()
                        if len(parts) >= 2:
                            manifest_names.add(parts[-1].lstrip("*"))
            except OSError:
                pass

        if os.path.isfile(manifest_sig):
            try:
                st = os.stat(manifest_sig)
                signature_info = {
                    "name": "SHA256SUMS.gpg",
                    "path": manifest_sig,
                    "size": st.st_size,
                    "mtime": st.st_mtime,
                }
            except OSError:
                pass

        try:
            for name in sorted(os.listdir(uki_dir)):
                if name.endswith(".efi"):
                    fp = os.path.join(uki_dir, name)
                    st = os.stat(fp)
                    m = re.search(r"-([0-9][0-9A-Za-z._]*)\.efi$", name)
                    version = m.group(1) if m else ""
                    signed = signature_info is not None and name in manifest_names
                    ukis.append({
                        "name": name,
                        "path": fp,
                        "version": version,
                        "size": st.st_size,
                        "mtime": st.st_mtime,
                        "signed": signed,
                    })
        except OSError:
            pass

    # 2. Inspect OSTree repository
    summary_info = None
    summary_sig_info = None
    refs = []
    objects_count = 0
    objects_size = 0
    deltas = []

    if os.path.isdir(repo_dir):
        sum_path = os.path.join(repo_dir, "summary")
        if os.path.isfile(sum_path):
            try:
                st = os.stat(sum_path)
                summary_info = {"name": "summary", "path": sum_path, "size": st.st_size, "mtime": st.st_mtime}
            except OSError:
                pass
        sig_path = os.path.join(repo_dir, "summary.sig")
        if os.path.isfile(sig_path):
            try:
                st = os.stat(sig_path)
                summary_sig_info = {"name": "summary.sig", "path": sig_path, "size": st.st_size, "mtime": st.st_mtime}
            except OSError:
                pass

        heads_dir = os.path.join(repo_dir, "refs", "heads")
        commit_by_version = {}
        version_by_commit = {}
        if os.path.isdir(heads_dir):
            for root, _, files in os.walk(heads_dir):
                for f in files:
                    fp = os.path.join(root, f)
                    ref_name = os.path.relpath(fp, heads_dir)
                    try:
                        st = os.stat(fp)
                        with open(fp, "r", errors="replace") as rf:
                            commit = rf.read().strip()
                        m = re.search(r"\.v([0-9][0-9A-Za-z._]*)$", ref_name)
                        if m:
                            kind = "Ref Version"
                            commit_by_version[m.group(1)] = commit
                            version_by_commit[commit] = m.group(1)
                        else:
                            kind = "Ref Tip"
                        refs.append({
                            "ref": ref_name,
                            "path": fp,
                            "commit": commit,
                            "kind": kind,
                            "size": st.st_size,
                            "mtime": st.st_mtime,
                        })
                    except OSError:
                        continue
        refs.sort(key=lambda r: (r["kind"] != "Ref Tip", r["ref"]))

        obj_dir = os.path.join(repo_dir, "objects")
        if os.path.isdir(obj_dir):
            for root, _, files in os.walk(obj_dir):
                for f in files:
                    fp = os.path.join(root, f)
                    try:
                        st = os.stat(fp)
                        objects_count += 1
                        objects_size += st.st_size
                    except OSError:
                        continue

        deltas_dir = os.path.join(repo_dir, "deltas")
        if os.path.isdir(deltas_dir):
            for root, dirs, files in os.walk(deltas_dir):
                if "superblock" in files or (files and not dirs):
                    d_size = 0
                    d_mtime = 0
                    for f in files:
                        fp = os.path.join(root, f)
                        try:
                            st = os.stat(fp)
                            d_size += st.st_size
                            if st.st_mtime > d_mtime:
                                d_mtime = st.st_mtime
                        except OSError:
                            continue
                    rel = os.path.relpath(root, deltas_dir)
                    d_name = rel.replace("/", "")
                    deltas.append({
                        "name": d_name,
                        "path": root,
                        "size": d_size,
                        "mtime": d_mtime,
                    })

    # 3. Companion snapshots in parent deploy directory
    deploy_dir = os.path.dirname(os.path.abspath(payload_dir))
    base_name = os.path.basename(payload_dir)
    stem = base_name[:-len("-payload")] if base_name.endswith("-payload") else base_name
    snapshots = []
    static_delta_files = {}

    if os.path.isdir(deploy_dir):
        for fname in sorted(os.listdir(deploy_dir)):
            if not fname.startswith(stem + "."):
                continue
            fp = os.path.join(deploy_dir, fname)
            if not os.path.isfile(fp):
                continue
            try:
                st = os.stat(fp)
            except OSError:
                continue
            ext = fname[len(stem) + 1:]
            kind = "Snapshot"
            details = ext
            if ext == "ostree-commit":
                details = "SHA256 commit hash"
            elif ext == "ostree-ref":
                details = "Active branch ref"
            elif ext == "ostree-parent":
                details = "Parent commit hash"
            elif ext == "ostree-summary":
                details = "Signed branch index"
            elif ext == "ostree-summary.sig":
                details = "Detached PGP signature"
            elif ext.startswith("static-delta-"):
                v_from = ext[len("static-delta-"):]
                details = f"Delta source mapping (v{v_from})"
                try:
                    with open(fp, "r", errors="replace") as df:
                        lines = dict(l.strip().split("=", 1) for l in df if "=" in l)
                    static_delta_files[v_from] = lines
                except OSError:
                    pass
            elif ext == "img.digest":
                kind = "Checksum"
                details = "SHA256 image digest"
            elif ext == "sbom.json":
                kind = "SBOM"
                details = "SPDX 2.3 package bill"

            snapshots.append({
                "name": fname,
                "path": fp,
                "kind": kind,
                "details": details,
                "size": st.st_size,
                "mtime": st.st_mtime,
            })

    # Resolve delta version pairs
    for d in deltas:
        pair = None
        for v_from, mapping in static_delta_files.items():
            to_commit = mapping.get("to_commit", "")
            to_ver = version_by_commit.get(to_commit, "")
            if to_ver:
                pair = f"v{v_from} -> v{to_ver}"
                break
            pair = f"from v{v_from}"
        d["pair"] = pair

    return {
        "path": payload_dir,
        "name": base_name,
        "stem": stem,
        "size": total_size,
        "mtime": latest_mtime,
        "uki": {
            "dir": uki_dir,
            "manifest": manifest_info,
            "signature": signature_info,
            "ukis": ukis,
        },
        "repo": {
            "dir": repo_dir,
            "summary": summary_info,
            "summary_sig": summary_sig_info,
            "refs": refs,
            "objects_count": objects_count,
            "objects_size": objects_size,
            "deltas": deltas,
        },
        "snapshots": snapshots,
    }


# What a build left behind: a stat-based listing of
# 'ContainerEngine.deploy_root()/<release>/', the same directory named
# by the "would write:" line on Overview.
def render_artifacts(context):
    if not context.active:
        return "no active specification -- '/use SPEC...' picks one\n"
    sections = []
    for build in context.builds:
        release = build.spec["distribution"]["release"]
        root = ContainerEngine.deploy_dir(release)
        lines = [f"{root}/"]
        entries = sorted(os.listdir(root)) if os.path.isdir(root) else []
        if len(entries) == 0:
            lines.append("  nothing built here yet")
        for name in entries:
            path = os.path.join(root, name)
            try:
                st = os.stat(path)
            except OSError:
                continue
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))
            if os.path.isdir(path):
                payload_info = inspect_payload(path)
                if payload_info is not None:
                    p_size = _human_size(payload_info["size"])
                    lines.append(f"  {name + '/':<32} {p_size:>10}  {when}")
                    u_dir = payload_info["uki"]
                    if u_dir["ukis"] or u_dir["manifest"]:
                        lines.append("    uki/")
                        if u_dir["manifest"]:
                            m = u_dir["manifest"]
                            m_when = time.strftime("%Y-%m-%d %H:%M", time.localtime(m["mtime"]))
                            lines.append(f"      {m['name']:<28} {_human_size(m['size']):>10}  {m_when}")
                        if u_dir["signature"]:
                            s = u_dir["signature"]
                            s_when = time.strftime("%Y-%m-%d %H:%M", time.localtime(s["mtime"]))
                            lines.append(f"      {s['name']:<28} {_human_size(s['size']):>10}  {s_when}")
                        for u in u_dir["ukis"]:
                            u_when = time.strftime("%Y-%m-%d %H:%M", time.localtime(u["mtime"]))
                            lines.append(f"      {u['name']:<28} {_human_size(u['size']):>10}  {u_when}")
                    r_dir = payload_info["repo"]
                    lines.append("    repo/")
                    if r_dir["summary"]:
                        sm = r_dir["summary"]
                        sm_when = time.strftime("%Y-%m-%d %H:%M", time.localtime(sm["mtime"]))
                        lines.append(f"      {'summary':<28} {_human_size(sm['size']):>10}  {sm_when}")
                    if r_dir["summary_sig"]:
                        sms = r_dir["summary_sig"]
                        sms_when = time.strftime("%Y-%m-%d %H:%M", time.localtime(sms["mtime"]))
                        lines.append(f"      {'summary.sig':<28} {_human_size(sms['size']):>10}  {sms_when}")
                    for r in r_dir["refs"]:
                        r_when = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["mtime"]))
                        lines.append(f"      {'refs/heads/' + r['ref']:<28} {_human_size(r['size']):>10}  {r_when}")
                    if r_dir["objects_count"]:
                        obj_label = f"objects/ ({r_dir['objects_count']} objects)"
                        lines.append(f"      {obj_label:<28} {_human_size(r_dir['objects_size']):>10}")
                    if r_dir["deltas"]:
                        lines.append("      deltas/")
                        for d in r_dir["deltas"]:
                            d_when = time.strftime("%Y-%m-%d %H:%M", time.localtime(d["mtime"]))
                            d_lbl = f"{d['name']} ({d['pair']})" if d.get("pair") else d["name"]
                            lines.append(f"        {d_lbl:<26} {_human_size(d['size']):>10}  {d_when}")
                    continue
            lines.append(f"  {name:<32} {_human_size(st.st_size):>10}  {when}")
        sections.append("\n".join(lines))
    return "\n\n".join(sections) + "\n"
