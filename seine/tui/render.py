# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# Text for the Overview/Plan screens, built from the same calls
# 'seine plan'/'seine build --dry-run' make. Kept apart from the widgets
# so it can be tested without a running App.

import contextlib
import io
import hashlib
import json
import os
import re
import time

from seine import analyze
from seine import multiconfig
from seine import packages
from seine import sbom
from seine import utils
from seine.diffing import diff, recall
from seine.imager import payload
from seine.imager.ostree import ostree_ref
from seine.tui import remote_match
from seine.progress import elapsed
from seine.sbuild import BuilderImage
from seine.tui.artifacts import _human_size
from seine.utils import git_status, lock_sibling

# One line on the newest server build of this spec.
def _remote_line(info):
    if info["status"] not in ("completed", "failed"):
        return "remote: %s on the server" % info["status"]
    done = info.get("finished_at") or info["created_at"]
    took = done - (info.get("started_at") or info["created_at"])
    text = "remote: %s %s ago, took %s" % (
        "built" if info["status"] == "completed" else "FAILED",
        elapsed(max(0, time.time() - done)), elapsed(took))
    if info.get("artifacts_expired_at"):
        text += " (artifacts expired)"
    return text

# Summary card for OSTree update payload configuration and release metadata.
def _ostree_payload_box(build, ostree):
    distro = build.spec.get("distribution") or {}
    mode = ostree.get("mode", "standard")
    if mode == "composefs" and ostree.get("composefs-key"):
        mode_str = "composefs (signed)"
    else:
        mode_str = mode

    try:
        ref = ostree_ref(ostree, distro)
    except Exception:
        ref = ostree.get("ref") or "none"

    version = str(ostree.get("version", "none"))
    stateroot = ostree.get("stateroot", "default")
    commit_key = ostree.get("gpg-key") or ostree.get("composefs-key") or "none"
    manifest_key = ostree.get("manifest-key", "none")

    output = build.image._output
    if output:
        pdir = payload.payload_dir(output, ostree.get("payload"), distro.get("release"))
        target_path = f"{utils.display_path(pdir).rstrip('/')}/"
    else:
        target_path = "none"

    deltas = (ostree.get("payload") or {}).get("deltas-from")
    if deltas:
        if deltas == ["1"]:
            deltas_str = '["1"] (static delta against factory baseline release)'
        else:
            deltas_str = json.dumps(deltas)
    else:
        deltas_str = "none"

    col1_1 = f"Mode:        {mode_str}"
    col2_1 = f"Ref:        {ref}"
    col1_2 = f"Version:     {version}"
    col2_2 = f"Stateroot:  {stateroot}"
    col1_3 = f"Commit Key:  {commit_key}"
    col2_3 = f"Manifest:   {manifest_key}"
    line4 = f"Target Path: {target_path}"
    line5 = f"Deltas From: {deltas_str}"

    left_w = max(len(col1_1), len(col1_2), len(col1_3) + 3, 36)
    row1 = f"{col1_1.ljust(left_w)}{col2_1}"
    row2 = f"{col1_2.ljust(left_w)}{col2_2}"
    row3 = f"{col1_3.ljust(left_w)}{col2_3}"

    content_lines = [row1, row2, row3, line4, line5]
    inner_w = max(74, max(len(l) for l in content_lines))

    title = " OSTree Update Payload "
    dashes = inner_w + 1 - len(title)
    top = f"┌─{title}{'─' * dashes}┐"
    bottom = f"└{'─' * (inner_w + 2)}┘"

    box = [top]
    for l in content_lines:
        box.append(f"│ {l.ljust(inner_w)} │")
    box.append(bottom)
    return box

# Overview lines for one build: distro/arch, last build, diff
# status, output path. Shared by render_overview() and
# render_root_node().
def _overview_lines(files, build, name):
    parts = []
    distro = build.spec["distribution"]
    output = build.image._output
    parts.append("%s -- %s/%s" % (name, distro["release"], distro["architecture"]))

    digest = analyze.spec_digest(build.spec)
    history = analyze.runs(digest)
    if history:
        latest = history[0]
        ago = max(0, time.time() - latest["started"])
        status = "built" if latest.get("ok", True) else "FAILED"
        parts.append("  last build: %s %s ago, took %s"
                    % (status, elapsed(ago), elapsed(analyze.spent(latest))))
    else:
        parts.append("  never built from here")

    remote = remote_match.status(build)
    if remote:
        parts.append("  " + _remote_line(remote))

    baseline = recall(files)
    if baseline is None:
        parts.append("  spec: not built from here yet")
    elif build.dump(build.spec) == baseline:
        parts.append("  spec: unchanged since the last build")
    else:
        parts.append("  spec: changed since the last build -- '/plan' shows how")

    if output:
        parts.append("  would write: %s" % output)

    ostree = (build.spec.get("image") or {}).get("ostree")
    if ostree and ostree.get("mode") != "disabled":
        parts.append("")
        parts += _ostree_payload_box(build, ostree)
    return parts

def render_overview(context):
    if not context.active:
        return "no active specification -- '/use SPEC...' picks one\n"
    parts = []
    for files, build in zip(context.groups, context.builds):
        output = build.image._output
        name = output.rsplit("/", 1)[-1].rsplit(".", 1)[0] if output else \
               context.label()
        parts += _overview_lines(files, build, name)
    return "\n".join(parts) + "\n"

# Root/group node: its loaded spec files (git status, plus a trailing
# lock icon -- trailing so names stay aligned -- for a loaded
# .lock.yaml sibling), then the same build stats render_overview() shows.
def render_root_node(files, build):
    lines = ["spec files:"]
    for path in files:
        status = git_status(path) or "  "
        sibling = lock_sibling(path)
        lock = " \U0001F512" if sibling and os.path.isfile(sibling) else ""
        lines.append("  %s %s%s" % (status, path, lock))
    output = build.image._output
    name = (output.rsplit("/", 1)[-1].rsplit(".", 1)[0] if output else
           multiconfig._label(build))
    lines.append("")
    lines += _overview_lines(files, build, name)
    return "\n".join(lines) + "\n"

# Image node: partitions/volumes drawn as proportional stacked boxes.
# seine's spec has no 'swap'/'ESP' concept, so roles are read off what
# a partition/volume actually declares, not invented fields. A
# container (an LVM-PV partition) gets its own color -- purple --
# rather than sharing plain blue 'data'.
_ROLE_STYLES = {"boot": "cyan", "root": "green", "lvm": "purple",
                "data": "blue", "verity": "magenta"}
_ROLE_ORDER = ("boot", "root", "lvm", "data", "verity")

def _role(entry):
    flags = entry.get("flags") or []
    if "boot" in flags or "xbootldr" in flags:
        return "boot"
    if entry.get("type") == "verity-hash":
        return "verity"
    if entry.get("where") == "/":
        return "root"
    # No 'where' at all (only an LVM-PV partition can lack one --
    # PartitionHandler._parse_part() requires it otherwise): the
    # container itself, not a mounted leaf.
    if "where" not in entry:
        return "lvm"
    return "data"

# A partition carries its own 'group' AND has no 'where' only when it's
# the LVM-PV holding a group of volumes -- the one structural signal
# PartitionHandler's own parsing guarantees (see _role() above).
def _is_container(entry):
    return "where" not in entry and entry.get("group") is not None

FLOOR_ROWS = 2  # label + size, the decided two-row box content
# ponytail: an arbitrary but readable spread of extra rows the
# image's largest leaf can earn over the floor; tune if it reads wrong.
EXTRA_ROWS_BUDGET = 16
# Widest a box ever gets. The actual width comes from the pane itself
# (render_image_node()'s 'width'), which is usually narrower than this.
BOX_WIDTH = 45
# Narrowest a box still reads as one: borders plus a few label
# characters. Only matters on a comically narrow terminal; the pane
# hands down something wider in practice.
MIN_BOX_WIDTH = 12

def _extra_rows(sizes, budget=EXTRA_ROWS_BUDGET):
    total = sum(sizes)
    if total <= 0:
        return [0] * len(sizes)
    return [round(budget * size / total) for size in sizes]

# A declared size is fixed. Without one, the size from the last build
# (if any) is shown and flagged dynamic; 0 means not known yet.
def _box_size(entry, actual):
    if entry.get("size"):
        return {"size": entry["size"], "dynamic": False}
    return {"size": actual.get(entry["label"], 0), "dynamic": True}

# Leaves only, budgeted across the WHOLE image in one go, not per
# sibling group: a per-group budget made a small top-level partition
# look as big as a much larger LVM volume in a smaller group.
def _leaf_boxes(entries, actual):
    boxes = [{"label": e["label"], "type": e.get("type", ""),
             "role": _role(e), "children": [], **_box_size(e, actual)}
            for e in entries]
    for box, extra in zip(boxes, _extra_rows([b["size"] for b in boxes])):
        box["rows"] = FLOOR_ROWS + extra
    return boxes

# A container's height is the sum of its own children stacked, not a
# proportional share against its sibling leaves: what it displays is
# already exactly what it contains.
def _layout(image_spec, actual):
    partitions = image_spec.get("partitions") or []
    volumes = image_spec.get("volumes") or []
    by_group = {}
    for volume in volumes:
        by_group.setdefault(volume.get("group"), []).append(volume)

    container_entries = [p for p in partitions if _is_container(p)]
    leaf_entries = [p for p in partitions if not _is_container(p)]
    for entry in container_entries:
        leaf_entries.extend(by_group.get(entry.get("group"), []))

    boxes_by_label = {box["label"]: box for box in _leaf_boxes(leaf_entries, actual)}
    for entry in container_entries:
        children = [boxes_by_label[v["label"]]
                    for v in by_group.get(entry.get("group"), [])]
        rows = 1 + sum(child["rows"] for child in children) if children else FLOOR_ROWS
        boxes_by_label[entry["label"]] = {
            "label": entry["label"], "type": entry.get("type", ""),
            "role": _role(entry), "children": children, "rows": rows,
            **_box_size(entry, actual),
        }
    # On-disk order, not leaves-then-containers.
    return [boxes_by_label[p["label"]] for p in partitions]

# One row per box line, each a list of (segment, role) pairs: a child's
# frame keeps its role, but the outer wrapping chars keep the PARENT's.
# Label/detail are truncated to the box so a long one can't widen it.
def _box_lines(box, width):
    width = max(width, 8)  # nesting subtracts per level; never let the border math go negative
    inner = width - 2
    field = max(inner - 1, 1)
    role = box["role"]
    size_text = _human_size(box["size"]) if box["size"] else "?"
    detail = "%s  %s" % (box["type"], size_text) if box["type"] else size_text
    # Dynamic sizes are italic so they read apart from fixed ones.
    detail_role = f"{role} italic" if box["dynamic"] and box["size"] else role
    lines = [
        [("┌" + "─" * inner + "┐", role)],
        [("│ " + box["label"][:field].ljust(field) + "│", role)],
        [("│ " + detail[:field].ljust(field) + "│", detail_role)],
    ]
    if box["children"]:
        for child in box["children"]:
            for child_row in _box_lines(child, width - 4):
                lines.append([("│ ", role)] + child_row + [(" │", role)])
    else:
        # A leaf's proportional share over the floor (_extra_rows()) is
        # blank filler, not more detail -- the box's height alone is
        # what carries the size comparison, not repeated text.
        blank = "│" + " " * inner + "│"
        for _ in range(box["rows"] - FLOOR_ROWS):
            lines.append([(blank, role)])
    lines.append([("└" + "─" * inner + "┘", role)])
    return lines

def _flatten(boxes):
    for box in boxes:
        yield box
        yield from _flatten(box["children"])

# 'actual' maps label -> bytes from the last build (see Image._write_layout()).
def render_image_node(image_spec, width=BOX_WIDTH, actual=None):
    from rich.text import Text
    width = max(MIN_BOX_WIDTH, min(width, BOX_WIDTH))
    boxes = _layout(image_spec, actual or {})
    text = Text()
    if not boxes:
        text.append("no partitions defined\n")
        return text
    present = {box["role"] for box in _flatten(boxes)}
    text.append("legend: ")
    first = True
    for role in _ROLE_ORDER:
        if role not in present:
            continue
        if not first:
            text.append("  ")
        text.append(role, style=_ROLE_STYLES[role])
        first = False
    text.append("\n\n")
    for box in boxes:
        for row in _box_lines(box, width):
            for segment, role in row:
                base, _, extra = role.partition(" ")
                text.append(segment, style=f"{_ROLE_STYLES.get(base, '')} {extra}".strip())
            text.append("\n")
    return text

# Fallback pane for a node with no own renderer (root/image/logs
# have one; everything else lands here): its own text, plus child
# labels when it is a branch, not a leaf.
def render_node(node):
    if node.children:
        lines = [str(node.data), ""]
        lines += ["  %s" % child.data for child in node.children]
        return "\n".join(lines) + "\n"
    return "%s\n" % node.data

# How many runs' links a task-kind bullet shows -- '[latest] [-1] [-2]',
# not the whole history logindex.KEEP keeps on disk.
LOGS_SHOWN = 3

# One list per distinct task name mapping to 'branch' (spectree.
# branch_for()'s shape), across every entry for this release/arch,
# newest first since logindex.entries() already is.
def _logs_by_task(release, arch, branch):
    from seine import logindex
    from seine.tui.spectree import branch_for
    matches = {}
    for entry in logindex.entries():
        if entry["release"] != release or entry["arch"] != arch:
            continue
        for t in entry.get("tasks", []):
            if branch_for(t["name"]) != branch:
                continue
            matches.setdefault(t["name"], []).append(t)
    return matches

# Green/red for ok/failed, a rocket instead of a link for a cache hit
# (nothing to open). Age-labelled ('[latest]', '[-1]', ...) rather than
# a literal list index, which would read backwards in Python terms.
def _logs_section(release, arch, branch):
    from rich.style import Style
    from rich.text import Text
    from seine import logindex
    matches = _logs_by_task(release, arch, branch)
    if not matches:
        return None
    text = Text()
    text.append("\nLogs:\n")
    for name in sorted(matches):
        text.append("  - %s: " % name)
        for i, run in enumerate(matches[name][:LOGS_SHOWN]):
            label = "[latest]" if i == 0 else "[-%d]" % i
            if run.get("cached"):
                text.append("\U0001F680", style=Style())
                text.append(label + " ", style=Style(dim=True))
                continue
            color = "red" if run["failed"] else "green"
            # A planned-but-not-started task has no log file yet --
            # dimmed rather than a link that opens on a missing file.
            path = logindex.resolve(run["log"]) if run.get("log") else None
            if path is not None and os.path.isfile(path):
                meta = {"log-click": path}
                text.append(label + " ", style=Style(color=color, meta=meta))
            else:
                text.append(label + " ", style=Style(dim=True))
        text.append("\n")
    return text

# Appends a 'Logs:' section to a task-backed node's own content
# ('text' may be a plain string or a Text); leaves it untouched
# when nothing is cataloged yet for this release/arch/branch.
def append_logs_section(text, release, arch, branch):
    from rich.text import Text
    section = _logs_section(release, arch, branch)
    if section is None:
        return text
    combined = Text(text) if isinstance(text, str) else text.copy()
    combined.append_text(section)
    return combined

# 'Image.plan()' prints straight to stdout, like every other 'seine'
# command; captured rather than reimplemented.
def _captured(fn):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        fn()
    return out.getvalue()

def render_plan(context):
    if not context.active:
        return "no active specification -- '/use SPEC...' picks one\n"
    sections = []
    for files, build in zip(context.groups, context.builds):
        text = _captured(build.image.plan)
        baseline = recall(files)
        if baseline is not None:
            changes = diff(baseline, build.dump(build.spec), color=False)
            text = "changed since last build:\n%s\n%s" % (changes, text)
        sections.append(text)
    return "\n\n".join(sections)

# Two data sources, labelled apart: what 'packages:' asked to rebuild
# from source, and what the last '--sbom' run found installed.
def render_packages(context):
    if not context.active:
        return "no active specification -- '/use SPEC...' picks one\n"
    sections = []
    for build in context.builds:
        distro = build.spec["distribution"]
        source_packages = build.image.packages
        lines = ["REBUILT FROM SOURCE (packages: section)", ""]
        if len(source_packages) == 0:
            lines.append("  none")
        else:
            builder = packages.Builder(distro, build.options,
                                       BuilderImage(distro, build.options))
            # 'Builder.current()' already only names a stamp on disk.
            current = {(package.name, architecture)
                      for package, architecture, _ in builder.current(source_packages)}
            for package in source_packages:
                for architecture in builder.architectures(package):
                    mark = "✔" if (package.name, architecture) in current else "○"
                    label = builder.label(package, architecture)
                    source = package.spec.get("source") or "(description only)"
                    lines.append("  %s %-24s %s" % (mark, label, source))
                    origin = package.origins.get("source") or package.origins.get("name")
                    if origin:
                        lines.append("      declared by: %s" % origin)

        lines.append("")
        lines.append("INSTALLED (from the last SBOM)")
        output = build.image._output
        sbom_path = sbom.output_path(output) if output else None
        if sbom_path is None or not os.path.isfile(sbom_path):
            lines.append(
                "  no SBOM for this build: run with --sbom to populate this section")
        else:
            try:
                with open(sbom_path) as f:
                    spdx = json.load(f)
                installed = sorted(spdx.get("packages", []),
                                  key=lambda p: p.get("name", ""))
                lines.append("  %d packages -- %s" % (len(installed), sbom_path))
                for entry in installed:
                    lines.append("  %-32s %s" % (entry.get("name", "?"),
                                                  entry.get("versionInfo", "")))
            except (OSError, ValueError) as e:
                lines.append("  could not read %s: %s" % (sbom_path, e))
        sections.append("\n".join(lines))
    return "\n\n".join(sections) + "\n"

# 'analyze.blame()'/'analyze.critical_chain()' print, captured for the
# newest recorded run. 'ROOTFS SIZE' below is the one section that reads
# more than the newest run, since a single number can't show growth.
def render_analyze(context):
    if not context.active:
        return "no active specification -- '/use SPEC...' picks one\n"
    sections = []
    for build in context.builds:
        digest = analyze.spec_digest(build.spec)
        history = analyze.runs(digest)
        if len(history) == 0:
            sections.append("no recorded run for this specification yet -- "
                            "'/build' it once first")
            continue
        latest = history[0]
        text = _captured(lambda run=latest: analyze.blame(run))
        text += "\n" + _captured(lambda run=latest: analyze.critical_chain(run))
        # Newest first, same order analyze.runs() returns. Older runs
        # made before this field existed just have nothing to add here.
        sized = [run for run in history if run.get("rootfs_size") is not None]
        if len(sized) > 0:
            lines = ["", "ROOTFS SIZE"]
            for i, run in enumerate(sized):
                ago = max(0, time.time() - run["started"])
                line = "  %s ago   %s" % (elapsed(ago), _human_size(run["rootfs_size"]))
                if i + 1 < len(sized):
                    delta = run["rootfs_size"] - sized[i + 1]["rootfs_size"]
                    if delta != 0:
                        line += "  (%s%s)" % ("+" if delta > 0 else "-",
                                              _human_size(abs(delta)))
                lines.append(line)
            text += "\n".join(lines) + "\n"
        sections.append(text)
    return "\n\n".join(sections)

def render_s3_status(context=None):
    from seine import doctor
    options = {}
    if context and context.active and context.builds:
        options = getattr(context.builds[0], "options", {}) or {}
        if "spec" not in options and hasattr(context.builds[0], "spec"):
            options = dict(options, spec=context.builds[0].spec)
    check = doctor.check_s3(options)
    if check is None:
        return ""
    status_label = "online (reachable)" if check.status == "ok" else f"offline ({check.detail})"
    lines = [
        "",
        "REMOTE CACHE (S3)",
        f"  Target:     {check.name}",
        f"  Status:     {status_label}",
    ]
    return "\n".join(lines) + "\n"

# Renders local cache storage usage, followed by remote S3 cache status when configured.
def render_cache(context=None, matching=None):
    import re
    from seine.cache import CACHES, CacheCmd
    pattern = re.compile(matching) if matching else None
    text = _captured(lambda: CacheCmd().info(list(CACHES.keys()), entries=True,
                                             matching=pattern))
    s3_text = render_s3_status(context)
    if s3_text:
        text += s3_text
    return text

def render_cache_explain(targets, options=None):
    from seine.cache import CacheCmd
    return _captured(lambda: CacheCmd().explain(targets, options=options or {}))

# A package can live inside a 'multiconfig:' sub-group's own
# 'packages:', not just the top-level image's -- walk 'subbuilds' the
# same way Image.tasks() does to build the real task graph.
def _find_package(image, name):
    for package in image.packages:
        if package.name == name:
            return image, package
    for sub in image.subbuilds.values():
        found = _find_package(sub.image, name)
        if found is not None:
            return found
    return None

# Why a 'packages:' entry did not come from the cache. Reuses the
# Builder a real build already made when there is one: only that
# instance still has miss_reason() for a miss it already explained.
def render_cache_why(context, name, architecture=None):
    if not context.active:
        return "no active specification -- '/use SPEC...' picks one\n"
    for build in context.builds:
        found = _find_package(build.image, name)
        if found is None:
            continue
        image, package = found
        distro = image.spec["distribution"]
        builder = image._builder
        if builder is None:
            builder = packages.Builder(distro, image.options,
                                       BuilderImage(distro, image.options))
        current = {(p.name, a) for p, a, _ in builder.current(image.packages)}
        archs = [architecture] if architecture else builder.architectures(package)
        lines = []
        for a in archs:
            if (package.name, a) in current:
                lines.append("%s/%s: cached" % (package.name, a))
                continue
            reason = builder.miss_reason(package, a)
            if reason is None:
                lines.append("%s/%s: not cached" % (package.name, a))
            else:
                lines.append("%s/%s: not cached --" % (package.name, a))
                lines += ["  %s" % r for r in reason]
        return "\n".join(lines) + "\n"
    return "'%s' is not in this specification's 'packages:' section\n" % name

def render_doctor(pull=False, options=None):
    import re

    from rich.text import Text

    from seine import doctor
    raw = doctor.render(doctor.run(options=options, pull=pull))
    text = Text(raw)
    for m in re.finditer(r"  ! ", raw):
        text.stylize("dark_orange", m.start() + 2, m.start() + 3)
    return text
