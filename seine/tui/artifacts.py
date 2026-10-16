# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# Artifacts screen, inspection of update payloads, and rendering helpers.

import hashlib
import os
import re
import time

from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Static, Tree

from seine.container import ContainerEngine
from seine.tui.base import BaseScreen
from seine.tui.spectree import SpecTree


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


ARTIFACTS_HEADER = (
    f"{'Artifact Name':<48}  {'Kind':<14}  {'Format / Details':<26}  {'Size':>10}  {'Modified':<16}\n"
    f"{'─' * 120}"
)


def format_artifact_row(name, kind="", details="", size="", modified="", depth=0):
    indent = depth * 4
    name_w = max(10, 48 - indent)
    kind_str = f"{kind:<14}" if kind else " " * 14
    details_str = f"{details:<26}" if details else " " * 26
    size_str = f"{size:>10}" if size else " " * 10
    mod_str = f"{modified:<16}" if modified else ""
    return f"{name:<{name_w}}  {kind_str}  {details_str}  {size_str}  {mod_str}".rstrip()


class ArtifactPreviewPane(VerticalScroll):
    pass


class ArtifactsTree(Tree):
    BINDINGS = Tree.BINDINGS + [
        Binding("space", "toggle_node", "Toggle", show=False),
        Binding("right", "expand_node", "Expand", show=False),
        Binding("left", "collapse_node", "Collapse", show=False),
    ]

    def action_expand_node(self):
        node = self.cursor_node
        if node is not None and node.allow_expand:
            node.expand()

    def action_collapse_node(self):
        node = self.cursor_node
        if node is not None and node.allow_expand and node.is_expanded:
            node.collapse()

    def load(self, context):
        self.clear()
        self.show_root = False
        if not context.active:
            text = "no active specification -- '/use SPEC...' picks one"
            self.root.add_leaf(text, data={"text": text})
            return

        for build in context.builds:
            release = build.spec["distribution"]["release"]
            root = ContainerEngine.deploy_dir(release)
            if not os.path.isdir(root):
                text = "  nothing built here yet"
                self.root.add_leaf(text, data={"text": text})
                continue

            entries = sorted(os.listdir(root))
            if not entries:
                text = "  nothing built here yet"
                self.root.add_leaf(text, data={"text": text})
                continue

            payloads = {}
            for name in entries:
                fp = os.path.join(root, name)
                if os.path.isdir(fp):
                    info = inspect_payload(fp)
                    if info:
                        payloads[name] = info

            snapshot_claimed = set()
            for p_info in payloads.values():
                for s in p_info.get("snapshots", []):
                    snapshot_claimed.add(s["name"])

            for name in entries:
                if name in snapshot_claimed:
                    continue
                fp = os.path.join(root, name)
                try:
                    st = os.stat(fp)
                except OSError:
                    continue
                when = time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))

                if name in payloads:
                    p_info = payloads[name]
                    p_size = _human_size(p_info["size"])
                    lbl = format_artifact_row(name + "/", "Payload Repo", "OSTree Archive + UKIs", p_size, when, depth=0)
                    p_node = self.root.add(lbl, expand=True, data={"name": name, "path": fp, "kind": "Payload Repo", "details": "OSTree Archive + UKIs", "size": p_info["size"], "info": p_info})

                    u_info = p_info["uki"]
                    if u_info["ukis"] or u_info["manifest"]:
                        uki_lbl = format_artifact_row("uki/", "Directory", "Signed UKI binaries & checksums", "", "", depth=1)
                        uki_node = p_node.add(uki_lbl, expand=True, data={"name": "uki", "path": u_info["dir"], "kind": "Directory"})
                        if u_info["manifest"]:
                            m = u_info["manifest"]
                            m_when = time.strftime("%Y-%m-%d %H:%M", time.localtime(m["mtime"]))
                            m_lbl = format_artifact_row(m["name"], "Manifest", "Sysupdate checksum list", _human_size(m["size"]), m_when, depth=2)
                            uki_node.add_leaf(m_lbl, data={"name": m["name"], "path": m["path"], "kind": "Manifest", "size": m["size"]})
                        if u_info["signature"]:
                            s = u_info["signature"]
                            s_when = time.strftime("%Y-%m-%d %H:%M", time.localtime(s["mtime"]))
                            s_lbl = format_artifact_row(s["name"], "Signature", "Detached PGP signature", _human_size(s["size"]), s_when, depth=2)
                            uki_node.add_leaf(s_lbl, data={"name": s["name"], "path": s["path"], "kind": "Signature", "size": s["size"]})
                        for u in u_info["ukis"]:
                            u_when = time.strftime("%Y-%m-%d %H:%M", time.localtime(u["mtime"]))
                            u_details = f"PE32+ (version {u['version']})" if u["version"] else "PE32+ UKI binary"
                            u_lbl = format_artifact_row(u["name"], "UKI Binary", u_details, _human_size(u["size"]), u_when, depth=2)
                            uki_node.add_leaf(u_lbl, data={"name": u["name"], "path": u["path"], "kind": "UKI Binary", "version": u["version"], "size": u["size"]})

                    r_info = p_info["repo"]
                    repo_lbl = format_artifact_row("repo/", "OSTree Repo", "archive-z2 repository", "", "", depth=1)
                    repo_node = p_node.add(repo_lbl, expand=True, data={"name": "repo", "path": r_info["dir"], "kind": "OSTree Repo"})
                    if r_info["summary"] and r_info["summary_sig"]:
                        sm_when = time.strftime("%Y-%m-%d %H:%M", time.localtime(r_info["summary"]["mtime"]))
                        sm_lbl = format_artifact_row("summary & summary.sig", "Summary", "Signed branch index", _human_size(r_info["summary"]["size"]), sm_when, depth=2)
                        repo_node.add_leaf(sm_lbl, data={"name": "summary", "path": r_info["summary"]["path"], "kind": "Summary", "size": r_info["summary"]["size"]})
                    elif r_info["summary"]:
                        sm = r_info["summary"]
                        sm_when = time.strftime("%Y-%m-%d %H:%M", time.localtime(sm["mtime"]))
                        sm_lbl = format_artifact_row("summary", "Summary", "Signed branch index", _human_size(sm["size"]), sm_when, depth=2)
                        repo_node.add_leaf(sm_lbl, data={"name": "summary", "path": sm["path"], "kind": "Summary", "size": sm["size"]})

                    for r in r_info["refs"]:
                        r_when = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["mtime"]))
                        ref_det = f"commit {r['commit'][:10]}" if r.get("commit") else "commit"
                        r_lbl = format_artifact_row(f"refs/heads/{r['ref']}", r["kind"], ref_det, _human_size(r["size"]), r_when, depth=2)
                        repo_node.add_leaf(r_lbl, data={"name": r["ref"], "path": r["path"], "kind": r["kind"], "commit": r["commit"], "size": r["size"]})

                    if r_info["objects_count"]:
                        obj_name = f"objects/ ({r_info['objects_count']:,} objects)"
                        obj_lbl = format_artifact_row(obj_name, "Objects", "Content-addressed files", _human_size(r_info["objects_size"]), "", depth=2)
                        repo_node.add_leaf(obj_lbl, data={"name": "objects", "kind": "Objects", "size": r_info["objects_size"]})

                    if r_info["deltas"]:
                        deltas_lbl = format_artifact_row("deltas/", "Static Deltas", "Compressed containers", "", "", depth=2)
                        deltas_node = repo_node.add(deltas_lbl, expand=True, data={"name": "deltas", "kind": "Static Deltas"})
                        for d in r_info["deltas"]:
                            d_when = time.strftime("%Y-%m-%d %H:%M", time.localtime(d["mtime"]))
                            d_name = f"{d['name'][:12]}... ({d['pair']})" if d.get("pair") else d["name"]
                            d_lbl = format_artifact_row(d_name, "Delta Blob", "lzma/zstd delta", _human_size(d["size"]), d_when, depth=3)
                            deltas_node.add_leaf(d_lbl, data={"name": d["name"], "path": d["path"], "kind": "Delta Blob", "size": d["size"]})

                    for s in p_info.get("snapshots", []):
                        s_when = time.strftime("%Y-%m-%d %H:%M", time.localtime(s["mtime"]))
                        s_lbl = format_artifact_row(s["name"], s["kind"], s["details"], _human_size(s["size"]), s_when, depth=0)
                        self.root.add_leaf(s_lbl, data=s)

                elif name.endswith(".img") and not os.path.isdir(fp):
                    partitions = build.spec.get("image", {}).get("partitions", [])
                    img_details = "GPT disk image"
                    if partitions:
                        part_types = [p.get("label", "") for p in partitions if p.get("label")]
                        img_details = f"GPT ({' + '.join(part_types[:2])})"
                    img_lbl = format_artifact_row(name, "Disk Image", img_details, _human_size(st.st_size), when, depth=0)
                    img_node = self.root.add(img_lbl, expand=True, data={"name": name, "path": fp, "kind": "Disk Image", "details": img_details, "size": st.st_size})
                    for idx, part in enumerate(partitions, start=1):
                        plabel = part.get("label", f"part{idx}")
                        pwhere = part.get("where", "")
                        pname = f"partition {idx}: {plabel} ({pwhere})" if pwhere else f"partition {idx}: {plabel}"
                        ptype = part.get("type", "ext4").upper()
                        prole = "systemd-boot + UKI" if "efi" in pwhere or "esp" in plabel else ("ostree physical deploy" if "ostree" in build.spec.get("image", {}) and pwhere == "/" else part.get("role", "filesystem"))
                        psize = _human_size(part.get("size", 0)) if part.get("size") else ""
                        part_lbl = format_artifact_row(pname, ptype, prole, psize, when, depth=1)
                        img_node.add_leaf(part_lbl, data={"name": pname, "kind": ptype, "details": prole, "size": part.get("size", 0)})

                else:
                    lbl = format_artifact_row(name, "Artifact", "", _human_size(st.st_size), when, depth=0)
                    self.root.add_leaf(lbl, data={"name": name, "path": fp, "kind": "Artifact", "size": st.st_size})


class ArtifactsScreen(BaseScreen):
    DEFAULT_CSS = """
    #artifactspath { height: 2; padding: 1 2 0 2; }
    #artifactheader { height: 2; padding: 0 2; }
    #artifactstree { width: 100%; height: 1fr; background: $background; padding: 0 1; }
    #artifactpreviewpane { width: 100%; height: 1fr; background: $background; padding: 1 2; border: round $foreground 40%; }
    #artifactpreviewpane:focus { border: round $border; }
    """

    BINDINGS = BaseScreen.BINDINGS + [
        Binding("escape", "close_preview", show=False),
    ]

    HINT_ADD = [
        ("complete", "inspect", "Enter Inspect file"),
        ("inspect", "expand", "Space Expand/Collapse"),
    ]

    def compose_content(self):
        yield Horizontal(
            SpecTree(id="spectree", classes="spectree"),
            Vertical(
                Static(id="artifactspath", markup=False),
                Static(id="artifactheader", markup=False),
                ArtifactsTree("artifacts", id="artifactstree"),
                ArtifactPreviewPane(Static(id="artifactpreview", markup=False), id="artifactpreviewpane"),
                Static(id="body", markup=False),
                id="cmd",
            ),
            id="main",
        )

    def on_mount(self):
        super().on_mount()
        pane = self.query_one("#artifactpreviewpane", ArtifactPreviewPane)
        pane.display = False
        body = self.query_one("#body", Static)
        body.display = False

    def update_body(self):
        context = self.app.context
        self.query_one("#body", Static).update(render_artifacts(context))
        if not context.active:
            self.query_one("#artifactspath", Static).update("")
            self.query_one("#artifactheader", Static).update("")
            self.query_one(ArtifactsTree).load(context)
            return

        release = context.builds[0].spec["distribution"]["release"]
        root = ContainerEngine.deploy_dir(release)
        self.query_one("#artifactspath", Static).update(f"{root}/")
        entries = os.listdir(root) if os.path.isdir(root) else []
        if not entries:
            self.query_one("#artifactheader", Static).update("")
        else:
            self.query_one("#artifactheader", Static).update(ARTIFACTS_HEADER)
        self.query_one(ArtifactsTree).load(context)

    def on_tree_node_selected(self, event: Tree.NodeSelected):
        node = event.node
        data = node.data
        if not data or not isinstance(data, dict):
            return
        text = self._build_preview(data)
        if text:
            self.query_one("#artifactpreview", Static).update(text)
            self.query_one("#artifactpreviewpane", ArtifactPreviewPane).display = True
            self.query_one("#artifactstree", ArtifactsTree).display = False
            self.query_one("#artifactheader", Static).display = False
            self.query_one("#artifactpreviewpane", ArtifactPreviewPane).focus()

    def action_close_preview(self):
        pane = self.query_one("#artifactpreviewpane", ArtifactPreviewPane)
        if pane.display:
            pane.display = False
            self.query_one("#artifactstree", ArtifactsTree).display = True
            header = self.query_one("#artifactheader", Static)
            if header.renderable:
                header.display = True
            self.query_one("#artifactstree", ArtifactsTree).focus()

    def _build_preview(self, data):
        path = data.get("path")
        lines = []
        name = data.get("name", "Artifact")
        kind = data.get("kind", "")
        lines.append(f"Artifact: {name}")
        if kind:
            lines.append(f"Kind:     {kind}")
        if path:
            lines.append(f"Path:     {path}")
        if data.get("size"):
            lines.append(f"Size:     {_human_size(data['size'])} ({data['size']} bytes)")
        if data.get("commit"):
            lines.append(f"Commit:   {data['commit']}")
        if data.get("details"):
            lines.append(f"Details:  {data['details']}")
        lines.append("─" * 72)

        if path and os.path.isfile(path):
            try:
                sz = os.path.getsize(path)
                if sz <= 64 * 1024:
                    with open(path, "rb") as f:
                        raw = f.read()
                    if b"\0" not in raw:
                        text = raw.decode("utf-8", errors="replace")
                        lines.append(text)
                        return "\n".join(lines)
                digest = hashlib.sha256()
                with open(path, "rb") as f:
                    for chunk in iter(lambda: f.read(65536), b""):
                        digest.update(chunk)
                lines.append(f"SHA256:   {digest.hexdigest()}")
            except Exception as e:
                lines.append(f"error reading file: {e}")
        return "\n".join(lines) + "\n"
