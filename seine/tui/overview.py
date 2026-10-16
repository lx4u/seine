# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# Overview cockpit: the default screen showing the spec tree on the left,
# node content on the right, and logs/replays swapped into the left pane.

import os

from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import RichLog, Static

from seine.tui.base import BaseScreen, StaticPane
from seine.tui.render import (append_logs_section, render_image_node,
                              render_node, render_overview, render_root_node)
from seine.tui.spectree import SpecTree
from seine.tui.testing import render_test_node

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
    from seine.tui.render import BOX_WIDTH
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
            text = render_overview(self.app.context)
        elif node.parent is tree.root and node.children:
            # A group node always has children; the no-active-spec
            # leaf under tree.root does not. That tells them apart
            # here.
            index = tree.root.children.index(node)
            context = self.app.context
            text = render_root_node(context.groups[index], context.builds[index])
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
