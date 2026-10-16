# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# Overview cockpit: the default screen showing the spec tree on the left,
# node content on the right, and logs/replays swapped into the left pane.

import json
import os
import time

from rich.style import Style
from rich.text import Text
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import RichLog, Static

from seine import analyze
from seine import multiconfig
from seine import utils
from seine.diffing import recall
from seine.imager import payload
from seine.imager.ostree import ostree_ref
from seine.progress import elapsed
from seine.tui import remote_match
from seine.tui.artifacts import _human_size
from seine.tui.base import BaseScreen, StaticPane
from seine.tui.spectree import SpecTree
from seine.tui.testing import render_test_node
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
def _ostree_payload_box(build, ostree, width=None):
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

    max_inner_w = max(24, (width or 70) - 4)
    left_w = max(len(col1_1), len(col1_2)) + 4

    can_pair_1_2 = (left_w + len(col2_1) <= max_inner_w
                    and left_w + len(col2_2) <= max_inner_w)
    if can_pair_1_2:
        rows = [f"{col1_1.ljust(left_w)}{col2_1}",
                f"{col1_2.ljust(left_w)}{col2_2}"]
    else:
        rows = [col1_1, col2_1, col1_2, col2_2]

    left_w3 = max(left_w, len(col1_3) + 2)
    if can_pair_1_2 and left_w3 + len(col2_3) <= max_inner_w:
        rows.append(f"{col1_3.ljust(left_w3)}{col2_3}")
    else:
        rows.extend([col1_3, col2_3])

    content_lines = rows + [line4, line5]
    truncated_lines = []
    for l in content_lines:
        if len(l) > max_inner_w:
            truncated_lines.append(l[:max_inner_w - 3] + "...")
        else:
            truncated_lines.append(l)

    title = " OSTree Update Payload "
    inner_w = max(len(title) + 2, max(len(l) for l in truncated_lines))
    inner_w = min(inner_w, max_inner_w)

    dashes = max(1, inner_w + 1 - len(title))
    top = f"┌─{title}{'─' * dashes}┐"
    bottom = f"└{'─' * (inner_w + 2)}┘"

    box = [top]
    for l in truncated_lines:
        box.append(f"│ {l.ljust(inner_w)[:inner_w]} │")
    box.append(bottom)
    return box

# Overview lines for one build: distro/arch, last build, diff
# status, output path. Shared by render_overview() and
# render_root_node().
def _overview_lines(files, build, name, width=None):
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
        parts.append("  would write: %s" % utils.display_path(output))

    ostree = (build.spec.get("image") or {}).get("ostree")
    if ostree and ostree.get("mode") != "disabled":
        parts.append("")
        parts += _ostree_payload_box(build, ostree, width=width)
    return parts

def render_overview(context, width=None):
    if not context.active:
        return "no active specification -- '/use SPEC...' picks one\n"
    parts = []
    for files, build in zip(context.groups, context.builds):
        output = build.image._output
        name = output.rsplit("/", 1)[-1].rsplit(".", 1)[0] if output else \
               context.label()
        parts += _overview_lines(files, build, name, width=width)
    return "\n".join(parts) + "\n"

# Root/group node: its loaded spec files (git status, plus a trailing
# lock icon -- trailing so names stay aligned -- for a loaded
# .lock.yaml sibling), then the same build stats render_overview() shows.
def render_root_node(files, build, width=None):
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
    lines += _overview_lines(files, build, name, width=width)
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
    section = _logs_section(release, arch, branch)
    if section is None:
        return text
    combined = Text(text) if isinstance(text, str) else text.copy()
    combined.append_text(section)
    return combined

# A read failure shows inline rather than raising, same as
# FilesystemScreen's own preview_failed() does for a bad file.
def _read_log(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError as e:
        return "could not read %s: %s" % (path, e)

# Same widget TestScreen/BuildScreen use for '#tail'. Hidden until a
# log link is clicked, sharing the tree's own slot.
class LogViewer(RichLog):
    def __init__(self, **kwargs):
        super().__init__(markup=False, wrap=True, max_lines=20000, **kwargs)
        self.display = False

# Replay pane: a focusable Static in the same left slot
# SpecTree/LogViewer share. Keys are consumed locally so they
# never reach the hidden tree underneath.
class CastPane(StaticPane):
    can_focus = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.display = False

    def on_key(self, event):
        if self.screen._cast_key(event.key):
            event.stop()
            event.prevent_default()

# Reads the 'log-click'/'cast-play' markers render.py puts in a
# span's meta, since Rich's own '@click' link style is Textual's,
# not ours (same workaround as target_screen.py's TargetStatusStatic).
class BodyStatic(Static):
    def on_click(self, event):
        path = event.style.meta.get("log-click")
        if path:
            self.screen.action_show_log(path)
            return
        cast = event.style.meta.get("cast-play")
        if cast:
            self.screen.action_replay_cast(cast)

# The right pane's own content width, not render.py's BOX_WIDTH
# default, which overflows the pane. A scrollbar not shown yet is
# reserved for, since tall boxes always earn one once they land.
def _body_width(screen):
    body = screen.query_one("#body", Static)
    pane = screen.query_one("#bodypane")
    width = body.content_size.width
    if width <= 0:
        return BOX_WIDTH
    if not pane.show_vertical_scrollbar:
        width -= pane.scrollbar_size_vertical or 2
    return width

class OverviewScreen(BaseScreen):
    HINT_ADD = [("complete", "logback", "Esc back (viewing a log/replay)")]

    # No-op unless a log or replay is being viewed -- same shape as
    # FilesystemScreen's own 'escape' -> action_close_preview.
    BINDINGS = BaseScreen.BINDINGS + [Binding("escape", "close_log", show=False)]

    # The right side is a vertical split: timeline (or node content)
    # up top, playing row's call arguments below -- the arguments
    # pane only shows while a replay with a timeline runs.
    def compose_content(self):
        yield Horizontal(
            SpecTree(id="spectree", classes="spectree"),
            LogViewer(id="logviewer"),
            CastPane(Static(id="cast", markup=False), id="castpane"),
            Vertical(
                StaticPane(BodyStatic(id="body", markup=False), id="bodypane"),
                StaticPane(Static(id="args", markup=False), id="argspane"),
                id="cmd",
            ),
            id="main",
        )

    # Not reported from SeineApp.on_mount(): the status bar isn't
    # mounted yet at that point.
    def on_mount(self):
        # Set first: super().on_mount() refreshes at once and reads these.
        # Path of the picked node; None shows the full overview.
        self._selected_path = None
        # Absolute path of the log currently shown in '#logviewer', or
        # None when the tree is showing instead.
        self._log_path = None
        # Absolute path of the screencast playing in '#cast', or None
        # when nothing is replaying; the player owns the clock.
        self._cast_path = None
        self._player = None
        self._cast_timer = None
        # Timeline row the right pane last showed, so we only
        # repaint when it changes.
        self._timeline_index = None
        # The arguments split only exists while a replay with a
        # timeline runs; hidden otherwise, leaving #bodypane the
        # full height.
        self.query_one("#argspane").display = False
        super().on_mount()
        if self.app._startup_error:
            self.say(self.app._startup_error, error=True)
        # '/replay' from another screen lands here with the cast to
        # play stashed on the app (see commands._replay()).
        pending = getattr(self.app, "_pending_replay", None)
        if pending is not None:
            self.app._pending_replay = None
            self.action_replay_cast(pending)

    def on_unmount(self):
        timer = getattr(self, "_cast_timer", None)
        if timer is not None:
            timer.stop()

    def on_resize(self, event):
        self.update_body()

    # Stored as a path: load() rebuilds all nodes, so a raw
    # reference would go stale. A dead path falls back to
    # the overview in update_body().
    def on_tree_node_selected(self, event):
        self._track_selection(event.node)

    # Cursor moves (up/down) re-render the pane too -- requiring Enter
    # to see a node's content lagged one keystroke behind navigation.
    def on_tree_node_highlighted(self, event):
        self._track_selection(event.node)

    def _track_selection(self, node):
        self._selected_path = self.query_one(SpecTree).path_for(node)
        self.update_body()

    # Swaps the tree for a log's text -- Esc (action_close_log) swaps
    # back. resolve() turns an index.json-relative path back into
    # something open()-able.
    def action_show_log(self, path):
        from seine import logindex
        self.action_close_cast()
        self._log_path = logindex.resolve(path)
        self._refresh_left_pane()

    # Esc closes whichever of the two is showing -- a replay in
    # progress first, since it owns the focused pane.
    def action_close_log(self):
        if self._cast_path is not None:
            self.action_close_cast()
        elif self._log_path is not None:
            self._log_path = None
            self._refresh_left_pane()

    def _refresh_left_pane(self):
        tree = self.query_one(SpecTree)
        viewer = self.query_one(LogViewer)
        cast = self.query_one(CastPane)
        showing_log = self._log_path is not None and self._cast_path is None
        showing_cast = self._cast_path is not None
        if showing_log:
            viewer.clear()
            viewer.write(_read_log(self._log_path))
        tree.display = not (showing_log or showing_cast)
        viewer.display = showing_log
        cast.display = showing_cast
        if showing_cast:
            self._redraw_cast()
            cast.focus()
        else:
            (viewer if showing_log else tree).focus()

    # Backwards-compatible name: the only caller outside this screen
    # is tests pinning the log-swap behaviour.
    def _refresh_log_pane(self):
        self._refresh_left_pane()

    # Starts replaying a .cast in the tree's slot -- Esc
    # (action_close_log) stops it and restores the tree. A read or
    # parse failure shows on the status line rather than raising.
    def action_replay_cast(self, path):
        from seine.tui.cast import CastPlayer, TICK, Unavailable
        player = CastPlayer()
        try:
            player.load(os.path.abspath(path))
        except Unavailable as e:
            self.say(str(e), error=True)
            return
        except (OSError, ValueError) as e:
            self.say("replay: %s" % e, error=True)
            return
        self.action_close_cast()
        self._log_path = None
        self._player = player
        self._cast_path = player.path
        self._cast_timer = self.set_interval(TICK, self._tick_cast)
        self._timeline_index = None
        self.query_one("#argspane").display = bool(player.timeline)
        self._refresh_left_pane()
        self._refresh_timeline()
        self.say("replaying %s -- space pauses, left/right set speed, Esc stops"
                 % os.path.basename(path))
        self.set_timer(2.5, lambda: self.say(""))

    def action_close_cast(self):
        timer = getattr(self, "_cast_timer", None)
        if timer is not None:
            timer.stop()
            self._cast_timer = None
        if self._cast_path is not None or self._player is not None:
            self._cast_path = None
            self._player = None
            self._timeline_index = None
            self.query_one("#argspane").display = False
            self._refresh_left_pane()
            # The right pane showed the replay timeline -- put back
            # whatever the selection held before (same recompute a
            # refresh would do, so Esc is exactly "undo").
            self.update_body()

    # CastPane's key handler: True when the key drove the replay (so
    # the pane stops it there), False for anything else. No-op when
    # nothing is replaying -- the tree's own keys apply instead.
    def _cast_key(self, key):
        if self._cast_path is None or self._player is None:
            return False
        if key == "space":
            self._player.toggle_pause()
        elif key == "left":
            self._player.slower()
        elif key == "right":
            self._player.faster()
        elif key == "escape":
            self.action_close_cast()
            return True
        else:
            return False
        self._redraw_cast()
        return True

    def _tick_cast(self):
        if self._player is None:
            return
        from seine.tui.cast import TICK
        if self._player.tick(TICK):
            self._redraw_cast()
        else:
            self._update_cast_border()
        index = self._player.current_index()
        if index != self._timeline_index:
            self._timeline_index = index
            self._refresh_timeline()

    # Timeline up top, playing row's call arguments below -- nothing
    # when the run predates interactions.json.
    def _refresh_timeline(self):
        if self._player is None or not self._player.timeline:
            return
        self.query_one("#body", Static).update(self._player.render_timeline())
        self.query_one("#args", Static).update(self._player.render_args())
        # Follow the playing row while running; paused, the viewport
        # stays where the user left it for inspection.
        if not self._player.paused:
            current = self._player.current_index()
            if current >= 0:
                pane = self.query_one("#bodypane")
                visible = max(1, pane.size.height - 2)
                pane.scroll_to(y=max(0, current + 2 - visible),
                                animate=False)

    def _redraw_cast(self):
        pane = self.query_one(CastPane)
        available = max(1, pane.size.height - 2)
        self.query_one("#cast", Static).update(
            self._player.render(max_lines=available))
        self._update_cast_border()

    def _update_cast_border(self):
        self.query_one(CastPane).border_subtitle = self._player.status()

    def update_body(self):
        tree = self.query_one(SpecTree)
        node = None
        if self._selected_path is not None:
            node = tree.node_for(self._selected_path)
            if node is None:
                self._selected_path = None
        if node is None:
            text = render_overview(self.app.context, width=_body_width(self))
        elif node.parent is tree.root and node.children:
            # A group node always has children; the no-active-spec
            # leaf under tree.root does not. That tells them apart
            # here.
            index = tree.root.children.index(node)
            context = self.app.context
            text = render_root_node(context.groups[index], context.builds[index],
                                    width=_body_width(self))
        elif node.data == "image" and node.parent is not None and node.parent.parent is tree.root:
            index = tree.root.children.index(node.parent)
            build = self.app.context.builds[index]
            text = render_image_node(build.spec.get("image") or {},
                                     width=_body_width(self),
                                     actual=build.image.read_layout())
            text = append_logs_section(
                text, build.spec["distribution"]["release"],
                build.spec["distribution"]["architecture"], ("image",))
        elif (node.parent is not None and node.parent.parent is tree.root
              and node.data in ("distribution", "packages", "playbook")):
            # Task-backed branches with no dedicated renderer yet: generic
            # fallback content, with their own logs appended below.
            index = tree.root.children.index(node.parent)
            build = self.app.context.builds[index]
            text = append_logs_section(
                render_node(node), build.spec["distribution"]["release"],
                build.spec["distribution"]["architecture"], (node.data,))
        elif (self._selected_path is not None and len(self._selected_path) >= 2
              and self._selected_path[1] == "test"):
            # Anything under the spec's 'test:' branch lists with the
            # same marks the Test screen uses. A scalar field below a
            # case (no test of its own) falls back to the generic listing.
            state = self.app.test_state
            if not getattr(state, "test_paths", None):
                # No run yet this session: derive the paths from the
                # selected group's own spec so unexecuted tests still
                # list with ○ rather than falling back to plain names.
                try:
                    index = next(i for i, c in enumerate(tree.root.children)
                                 if c.data == self._selected_path[0])
                except StopIteration:
                    index = 0
                builds = self.app.context.builds
                if 0 <= index < len(builds):
                    import types
                    from seine.tui.spectree import test_paths as _test_paths
                    state = types.SimpleNamespace(
                        test_paths=_test_paths(builds[index].spec),
                        rows={}, result=None)
            text = render_test_node(
                node, self._selected_path[1:], state)
            if text is None:
                text = render_node(node)
        else:
            text = render_node(node)
        self.query_one("#body", Static).update(text)
