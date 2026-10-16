# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# The TUI application: Overview/Plan/Build screens over one prompt, a
# command registry shared with Tab completion and the command palette,
# and a '!' shell escape. Everything is read from the engine on open --
# no separate TUI model, beyond build_state which outlives its screen.

import asyncio
import contextlib
import os
import subprocess
import socket
import threading
import time
import json

from textual import command
from textual.app import App
from textual.css.query import NoMatches
from textual.widgets import Static

from seine.tui import ai, commands
from seine.tui.base import (BaseScreen, DownloadIndicator, Indicators, VendorIndicator,
                            Prompt, RemoteIndicator, TargetIndicator)
from seine.tui.download import DownloadState
from seine.tui.remote_session import RemoteSession
from seine.tui.build import BuildScreen, BuildState
from seine.tui.chat import ChatScreen
from seine.tui.context import Context
from seine.tui.filesystem import FilesystemScreen, FilesystemState
from seine.tui.history import History
from seine.tui.issues import IssuesScreen
from seine.tui.overview import OverviewScreen
from seine.tui.vendor import VendorScreen, VendorState
from seine.tui.artifacts import ArtifactsScreen
from seine.tui.cache import render_cache
from seine.tui.render import (render_analyze, render_doctor,
                              render_packages, render_plan)
from seine.tui.remote_screen import RemoteScreen
from seine.tui.target import TargetState
from seine.tui.target_screen import TargetScreen
from seine.tui.testing import TestScreen, TestState

class PlanScreen(BaseScreen):
    def update_body(self):
        self.query_one("#body", Static).update(render_plan(self.app.context))


class PackagesScreen(BaseScreen):
    def update_body(self):
        self.query_one("#body", Static).update(render_packages(self.app.context))

class AnalyzeScreen(BaseScreen):
    def update_body(self):
        self.query_one("#body", Static).update(render_analyze(self.app.context))

# Not spec-scoped: cache and environment are shared by every build.
class CacheScreen(BaseScreen):
    def update_body(self):
        text = getattr(self.app, "cache_text", None)
        if text:
            self.query_one("#body", Static).update(text)
        else:
            self.query_one("#body", Static).update(render_cache(self.app.context))

class DoctorScreen(BaseScreen):
    def update_body(self):
        options = {}
        if self.app.context and self.app.context.active and self.app.context.builds:
            options = getattr(self.app.context.builds[0], "options", {}) or {}
            if "spec" not in options and hasattr(self.app.context.builds[0], "spec"):
                options = dict(options, spec=self.app.context.builds[0].spec)
        self.query_one("#body", Static).update(render_doctor(options=options))

# What /diff last computed -- not spec-scoped, so reads app.diff_text.
class DiffScreen(BaseScreen):
    def update_body(self):
        text = self.app.diff_text or (
            "no diff yet -- '/diff OLD.spdx.json NEW.spdx.json'\n")
        self.query_one("#body", Static).update(text)

SCREENS = {"overview": OverviewScreen, "plan": PlanScreen, "build": BuildScreen,
          "artifacts": ArtifactsScreen, "filesystem": FilesystemScreen,
          "packages": PackagesScreen, "analyze": AnalyzeScreen,
          "cache": CacheScreen, "doctor": DoctorScreen, "diff": DiffScreen,
          "issues": IssuesScreen, "chat": ChatScreen, "target": TargetScreen,
          "test": TestScreen, "vendor": VendorScreen, "remote": RemoteScreen}

# Offers the command registry through Ctrl+P. Selecting one fills the
# prompt rather than running it; a deliberate Enter runs it.
class RegistryProvider(command.Provider):
    async def discover(self):
        for c in commands.REGISTRY.values():
            yield command.DiscoveryHit(c.name, self._fill(c.name), help=c.help)

    async def search(self, query):
        matcher = self.matcher(query)
        for c in commands.REGISTRY.values():
            score = matcher.match(c.name)
            if score > 0:
                yield command.Hit(score, matcher.highlight(c.name),
                                  self._fill(c.name), help=c.help)

    def _fill(self, name):
        def callback():
            prompt = self.app.screen.query_one(Prompt)
            prompt.value = "/" + name + " "
            prompt.cursor_position = len(prompt.value)
            prompt.focus()
        return callback

class _AppContext(Context):
    def __init__(self, app):
        super().__init__()
        self._app = app
        self._builds_val = None

    @property
    def builds(self):
        if self._builds_val is None and not getattr(self._app, "is_running", False) and not getattr(self._app, "_loading_spec", False):
            if getattr(self._app, "_pending_files", None):
                self._app.load_pending_spec()
        return self._builds_val

    @builds.setter
    def builds(self, value):
        self._builds_val = value

class SeineApp(App):
    TITLE = "seine"
    # Commands and the infobar ask the app which screens it has.
    SCREENS = SCREENS
    COMMANDS = App.COMMANDS | {RegistryProvider}
    CSS = """
    #main, #buildrow { height: 1fr; }
    #screen-body { height: 1fr; }
    #screen-content { width: 1fr; height: 100%; }
    /* One cell wide, black like #castpane. */
    #navbar { width: 1; height: 100%; background: black; }
    .nav-icon { width: 1; height: 1; color: $foreground 70%; }
    .nav-icon:hover { color: $accent; }
    .nav-icon.nav-active { color: #ff8700; text-style: bold; }
    .nav-icon.nav-disabled { color: $text-muted; }
    /* 'round', not Input's default 'tall': 'tall' uses eighth-block
       glyphs some terminal fonts lack, breaking the border. */
    #spectree { width: 1fr; min-width: 30; max-width: 42; height: 100%; }
    #tail, #logviewer, #castpane { width: 2fr; height: 100%; }
    #prompt, #spectree, #tail, #fslist, #previewpane, #logviewer, #castpane, #artifactpreviewpane {
        border: round $foreground 40%;
    }
    #prompt:focus, #spectree:focus, #tail:focus, #fslist:focus,
    #previewpane:focus, #logviewer:focus, #castpane:focus, #artifactpreviewpane:focus {
        border: round $border;
    }
    /* Black like the target screen's console pane. Scrolls
       horizontally rather than wrapping the fixed 80-column grid. */
    #castpane { background: black; border-subtitle-align: right; border-subtitle-color: $warning; overflow-x: auto; }
    #cast { width: 80; height: auto; max-height: 40; }
    /* While startup commands run the prompt is shut (see
       _run_startup_commands() below) with a progress line in place
       of the default placeholder, italic so it reads as status. */
    #prompt.startup > .input--placeholder { text-style: italic; }
    #cmd, #tasks { width: 2fr; height: 100%; border: round $foreground 40%; }
    #body { padding: 1 2; }
    #artifactspath { height: 2; padding: 1 2 0 2; }
    #artifactheader { height: 2; padding: 0 2; }
    #artifactstree { width: 100%; height: 1fr; background: $background; padding: 0 1; }
    #artifactpreviewpane { width: 100%; height: 1fr; background: $background; padding: 1 2; }
    /* Timeline up top, playing row's arguments below, 2:1 -- the
       arguments pane only shows while a replay with a timeline
       runs, leaving the full height otherwise. */
    #bodypane { height: 2fr; }
    #argspane { height: 1fr; border-top: solid $foreground 40%; }
    #args { padding: 1 2; }
    #tasklist { padding: 1 2; }
    #tail, #logviewer { padding: 0 1; }
    /* Vendor screen: own ids, 1fr:1fr both rows (others are 2fr:1fr). */
    #vendormain, #vendorrow { height: 1fr; }
    #vendorspectree, #vendortail, #vendorstatspane, #vendortaskspane {
        width: 1fr; height: 100%; border: round $foreground 40%;
    }
    #vendorspectree:focus, #vendortail:focus {
        border: round $border;
    }
    #vendorstats, #vendortasks { padding: 1 2; }
    #vendortail { padding: 0 1; }
    #fslist { height: 1fr; }
    #previewpane { height: 1fr; padding: 1 2; }
    #remotemain { width: 3fr; height: 100%; border: round $foreground 40%; }
    #remotemain:focus { border: round $border; }
    #remoteside { width: 1fr; min-width: 32; height: 100%; border: round $foreground 40%; }
    #remotedetailpane { height: 2fr; }
    #remotesidebar { height: 1fr; min-height: 16; border-top: solid $foreground 40%; }
    #remotesidebar:focus { border-top: solid $border; }
    #remotebody, #remotedetail, #sidebarbody { padding: 1 2; }
    #hint { color: $text-muted; padding: 0 2; }
    #infobar { height: 1; }
    #status { padding: 0 2; height: 1; width: 1fr; }
    #status.error { color: $error; }
    #status.warning { color: $warning; }
    /* '$accent', not '$text-muted' like '#hint': clickable, like a link. */
    #indicators { color: $accent; padding: 0 2; height: 1; width: auto; }
    #vendor-indicator { color: $accent; padding: 0 2; height: 1; width: auto; }
    #target-indicator { color: $accent; padding: 0 2; height: 1; width: auto; }
    #download-indicator { color: $accent; padding: 0 2; height: 1; width: auto; }
    #remote-indicator { color: $accent; padding: 0 2; height: 1; width: auto; }
    #completions {
        height: auto;
        max-height: 5;
        border: none;
        margin: 0 2;
    }
    #completions > .option-list--option-highlighted {
        background: $accent;
        color: $text;
    }
    StartupModal { align: center middle; }
    #startuppane {
        width: 70%; height: 70%;
        border: round $border;
        background: $surface;
        padding: 1 2;
    }
    #startuptitle { color: blue; text-style: bold; height: 1; }
    #startuplog { height: 1fr; border: none; background: $surface; }
    #startupstatus { height: auto; color: $text-muted; }
    """

    def __init__(self, files=None, interaction_socket=None, remote=None, connect_remote=False,
                 remote_insecure=None, remote_ca_cert=None):
        super().__init__()
        self._remote = remote
        self._connect_remote = connect_remote
        self._remote_insecure = remote_insecure
        self._remote_ca_cert = remote_ca_cert
        self.context = _AppContext(self)
        self.history = History()
        self.build_state = BuildState()
        # "N build" chip's finish edge; the start edge is start_build()
        # calling refresh_indicators() directly.
        self.build_state.on_finished = self._build_finished
        # Follows a build onto the vendor screen for its 'vendor' task,
        # then back -- see _build_task_started/_build_task_finished below.
        self.build_state.on_task_started = self._build_task_started
        self.build_state.on_task_finished = self._build_task_finished
        self.vendor_state = VendorState()
        self.vendor_state.on_finished = self._vendor_finished
        self.fs_state = FilesystemState()
        self.ai_state = ai.AIState()
        # Back-reference so AIState can trigger socket notifications
        # without importing app here.
        self.ai_state.app = self
        self.target_state = TargetState()
        self.remote_session = RemoteSession(app=self)
        self.download_state = DownloadState()
        self.test_state = TestState()
        self.test_state.on_finished = self._test_finished
        self.test_state.on_started = self._test_started
        self.test_state.on_changed = self._test_changed
        self.diff_text = None
        self.cache_text = None
        # Set by commands.py's _issues() right before app.show("issues");
        # IssuesScreen.update_body() reads these back.
        self.issues_filter = None
        self.issues_min_urgency = None
        self.issues_min_severity = None
        self.issues_rescan = False
        self._startup_error = None
        # No spec at all, not a bad one -- a spec given but failed to
        # load still opens on Overview, where its error is expected.
        self._no_spec_given = not files
        self._pending_files = files
        self._loading_spec = False
        # Interaction socket, enabled via --interaction-socket: creates
        # the socket (overwriting any stale file) and starts a background
        # thread accepting newline-delimited JSON messages, dispatched
        # via _handle_socket_message.
        self._socket_path = interaction_socket
        self._socket_clients: list[socket.socket] = []
        self._socket_lock = threading.Lock()
        if self._socket_path:
            self._start_socket_server()

    @contextlib.asynccontextmanager
    async def run_test(self, *args, wait_for_spec=True, **kwargs):
        async with super().run_test(*args, **kwargs) as pilot:
            if wait_for_spec and getattr(self, "_loading_spec", False):
                while getattr(self, "_loading_spec", False):
                    await asyncio.sleep(0.01)
                    await pilot.pause()
            yield pilot

    def load_pending_spec(self):
        if not self._pending_files:
            return
        try:
            self.context.use(self._pending_files)
        except (OSError, ValueError) as e:
            self._startup_error = str(e)

    # Interaction-socket helpers, only reachable when --interaction-socket
    # is passed. Run in background threads and marshal UI actions via
    # self.call_from_thread().
    def _start_socket_server(self) -> None:
        path = self._socket_path
        if not path:
            return
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(path)
        server.listen()
        self._socket_server = server
        threading.Thread(target=self._socket_accept_loop, daemon=True).start()

    # Accepts connections, spinning a handler thread per client.
    def _socket_accept_loop(self) -> None:
        server: socket.socket = getattr(self, "_socket_server", None)
        if server is None:
            return
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                break
            with self._socket_lock:
                self._socket_clients.append(conn)
            threading.Thread(target=self._socket_client_handler, args=(conn,), daemon=True).start()

    # Reads newline-delimited JSON messages from 'conn', handing each
    # to _handle_socket_message. Closes on error or disconnect.
    def _socket_client_handler(self, conn: socket.socket) -> None:
        with conn:
            buffer = b""
            while True:
                try:
                    data = conn.recv(4096)
                except OSError:
                    break
                if not data:
                    break
                buffer += data
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if not line:
                        continue
                    try:
                        message = json.loads(line.decode())
                    except json.JSONDecodeError:
                        continue
                    self._handle_socket_message(message)
        # Clean up client list.
        with self._socket_lock:
            if conn in self._socket_clients:
                self._socket_clients.remove(conn)

    # Dispatches a JSON message from an external client: "input" types
    # msg["text"] into the prompt and submits it; "ai_input" forwards
    # msg["prompt"] straight to the AI chat.
    def _handle_socket_message(self, msg: dict) -> None:
        t = msg.get("type")
        if t == "input":
            text = msg.get("text", "")
            if not isinstance(text, str):
                return
            # Runs on the app's own thread (via call_from_thread below),
            # so action_submit() can just be awaited directly here.
            async def type_and_submit():
                try:
                    prompt = self.screen.query_one(Prompt)
                except NoMatches:
                    return
                prompt.value = ""
                prompt.cursor_position = 0
                for ch in text:
                    prompt.value += ch
                    prompt.cursor_position = len(prompt.value)
                    await asyncio.sleep(0.02)
                await prompt.action_submit()
            self.call_from_thread(type_and_submit)
        elif t == "ai_input":
            p = msg.get("prompt")
            if isinstance(p, str):
                self.call_from_thread(ai.ask, self, p)

    # Broadcasts a JSON message to all connected socket clients, dropping
    # any that raise on send.
    def _socket_send(self, data: dict) -> None:
        if not hasattr(self, "_socket_clients"):
            return
        raw = (json.dumps(data) + "\n").encode()
        with self._socket_lock:
            dead = []
            for client in self._socket_clients:
                try:
                    client.sendall(raw)
                except OSError:
                    dead.append(client)
            for client in dead:
                try:
                    client.close()
                finally:
                    self._socket_clients.remove(client)

    # Emits new assistant messages not yet sent, called from
    # AIState.changed after persisting. Streaming chunks are omitted.
    def _socket_send_ai_messages(self) -> None:
        msgs = self.ai_state._new_assistant_messages()
        for msg in msgs:
            self._socket_send({"type": "ai_message", "content": msg.get("content", "")})
        self.ai_state._mark_sent()

    # Whichever way the app is left, stop the downloads and forget the remote.
    def exit(self, *args, **kwargs):
        if getattr(self, "_loading_spec", False):
            from seine import tasks
            tasks.interrupt()
            if hasattr(self, "_load_worker") and self._load_worker:
                self._load_worker.cancel()
        self.download_state.cancel()
        self.remote_session.disconnect()
        super().exit(*args, **kwargs)

    # Releases any CredentialModal prompt still blocking a build worker,
    # so quitting the app can never leave it hung.
    async def action_quit(self):
        from seine.tui.credentials import release_pending
        release_pending()
        if getattr(self, "_loading_spec", False):
            from seine import tasks
            tasks.interrupt()
            if hasattr(self, "_load_worker") and self._load_worker:
                self._load_worker.cancel()
        await super().action_quit()

    # Nothing to build without a spec, so a bare 'seine tui' opens on
    # Doctor rather than an empty Overview.
    def on_mount(self):
        from seine import settings
        from seine.tui.startup import StartupModal
        current = settings.load()
        # An unset/hand-edited theme is silently skipped, not an error.
        if current["theme"] in commands.THEMES:
            self.theme = commands.THEMES[current["theme"]]

        # Auto-connect in a worker thread so UI startup is not blocked by latency.
        remote_target = self._remote or current.get("default_remote")
        should_connect = self._connect_remote or (current.get("auto_connect_remote") and bool(current.get("default_remote")))
        if should_connect:
            if remote_target:
                self.run_worker(lambda: self._auto_connect(remote_target), thread=True)
            else:
                self.say("remote: no URL specified and default_remote not configured", error=True)

        already_loaded = getattr(self.context, "_builds_val", None) is not None
        if self._no_spec_given or already_loaded:
            self._running_startup = len(current["startup_commands"]) > 0
            if self._no_spec_given:
                self.push_screen(DoctorScreen())
            else:
                self.push_screen(OverviewScreen())
            self.call_after_refresh(self._run_startup_commands, current["startup_commands"])
        else:
            self._running_startup = True
            self._pending_startup_commands = current["startup_commands"]
            self._loading_spec = True
            self.push_screen(OverviewScreen())
            modal = StartupModal()
            self._startup_modal = modal
            self.push_screen(modal)
            self.call_after_refresh(self._start_deferred_spec_load)

    def _start_deferred_spec_load(self):
        from seine.tui.startup import load_spec_deferred
        self._load_worker = self.run_worker(
            lambda: load_spec_deferred(self, self._startup_modal, self._pending_files),
            thread=True,
        )

    def _spec_load_finished(self, error):
        from seine.tui.startup import StartupModal
        self._loading_spec = False
        if isinstance(self.screen, StartupModal):
            self.pop_screen()
        elif hasattr(self, "_startup_modal") and self._startup_modal:
            try:
                self.uninstall_screen(self._startup_modal)
            except Exception:
                pass
        self._startup_modal = None

        if error:
            self._startup_error = error
            self.say(error, error=True)
        else:
            self._startup_error = None
            self.refresh_screens()
            self.remote_session.sync_matches()

        cmds = getattr(self, "_pending_startup_commands", [])
        self._pending_startup_commands = []
        if cmds:
            self._run_startup_commands(cmds)
        else:
            self._running_startup = False
            self._startup_progress(None, 0)

    def _auto_connect(self, target):
        session = self.remote_session
        if session.connect(target, insecure=self._remote_insecure,
                           ca_cert=self._remote_ca_cert) and session.warning:
            self.call_from_thread(self.say, f"remote: warning: {session.warning}", warning=True)

    def _run_startup_commands(self, lines):
        # Nothing seeded: leave the freshly mounted screen exactly as
        # its own on_mount() left it -- in particular, don't steal focus
        # back to the prompt a tick later while it is being used.
        if not lines:
            self._running_startup = False
            return
        total = len(lines)
        try:
            for index, line in enumerate(lines, 1):
                self._startup_progress(index, total)
                try:
                    commands.dispatch(self, line)
                except commands.CommandError as e:
                    self.say(str(e), error=True)
        finally:
            self._running_startup = False
            self._startup_progress(None, total)

    # Progress in the prompt's own placeholder ("Running startup
    # command (1/4)..."), cleared back to the default afterwards. Best
    # effort: a startup command may leave a modal (no prompt) on top.
    def _startup_progress(self, index, total):
        from seine.tui.base import DEFAULT_PLACEHOLDER
        try:
            prompt = self.screen.query_one(Prompt)
        except NoMatches:
            return
        if index is None:
            prompt.disabled = False
            prompt.placeholder = DEFAULT_PLACEHOLDER
            prompt.remove_class("startup")
            prompt.focus()
        else:
            prompt.disabled = True
            prompt.placeholder = "Running startup command (%d/%d)..." % (index, total)
            prompt.add_class("startup")

    # NoMatches below: worker callbacks (build/vendor finish, target
    # events) can land mid-transition, when the current screen's widgets
    # aren't composed yet -- a missed repaint is fine, a crash is not.
    def say(self, text, error=False, warning=False):
        if isinstance(self.screen, BaseScreen):
            try:
                self.screen.say(text, error=error, warning=warning)
            except NoMatches:
                pass

    def refresh_screens(self):
        if isinstance(self.screen, BaseScreen):
            self.screen.refresh_data()

    # Refreshes every chip regardless of which one a caller actually
    # changed -- ConsoleAdapter.on_event() needs TargetIndicator kept
    # current too, not just Indicators.
    def refresh_indicators(self):
        if isinstance(self.screen, BaseScreen):
            try:
                self.screen.query_one(Indicators).refresh_text()
                self.screen.query_one(VendorIndicator).refresh_text()
                self.screen.query_one(TargetIndicator).refresh_text()
                self.screen.query_one(DownloadIndicator).refresh_text()
                self.screen.query_one(RemoteIndicator).refresh_text()
            except NoMatches:
                pass

    def _build_finished(self):
        self._socket_send({"type": "build_finished",
                           "error": self.build_state.error,
                           "message": self.build_state.message})
        self.refresh_indicators()
        if self.build_state.remote:
            self.remote_session.sync_matches()
        # The build's analyze record and plan baseline now exist, so
        # whatever is on screen (overview, plan, build) is stale -- not
        # just the build screen's own task list.
        if isinstance(self.screen, BaseScreen):
            self.screen.refresh_data()
        # One-shot: only ai.py's _start_ai_build sets this, and it must
        # not fire again for whatever build runs next.
        if self.build_state.notify_ai:
            self.build_state.notify_ai = False
            # Only switch to chat if still on the Build screen; if they
            # navigated away themselves, leave that choice alone.
            if isinstance(self.screen, BuildScreen):
                self.show("chat")
            ai.notify_build_finished(self)

    # A build's 'vendor' task is the one part of a build the vendor
    # screen already knows how to show. Followed there while it runs,
    # then back to the build screen once done -- only while still on
    # the screen this following put the user on, same rule as
    # _build_finished().
    def _build_task_started(self, name):
        if name == "vendor" and isinstance(self.screen, BuildScreen):
            self.show("vendor")

    def _build_task_finished(self, name, failed=False):
        if name == "vendor" and isinstance(self.screen, VendorScreen):
            self.show("build")

    def _vendor_finished(self):
        self._socket_send({"type": "vendor_finished",
                           "error": self.vendor_state.error,
                           "message": self.vendor_state.message})
        self.refresh_indicators()
        if isinstance(self.screen, VendorScreen):
            self.screen.update_body()
        # One-shot, same as BuildState's own; only ai.py's start-vendor sets this.
        if self.vendor_state.notify_ai:
            self.vendor_state.notify_ai = False
            if isinstance(self.screen, VendorScreen):
                self.show("chat")
            ai.notify_vendor_finished(self)

    def _test_started(self):
        # A fresh run clears the previous per-test states, so
        # whatever is on screen (overview, test) is stale -- same
        # reason _test_finished() refreshes after a run.
        if isinstance(self.screen, BaseScreen):
            self.screen.refresh_data()

    def _test_changed(self):
        # Repaint right away so the overview's marks don't lag a
        # whole run behind. Pane only: the spec tree doesn't change
        # mid-run, so selection and expansion stay put.
        if isinstance(self.screen, OverviewScreen):
            self.screen.update_body()

    def _test_finished(self):
        self._socket_send({"type": "test_finished",
                           "error": self.test_state.error,
                           "message": self.test_state.message})
        # A finished test leaves new per-test states behind, so
        # whatever is on screen (overview, test) is stale -- same
        # reason _build_finished() refreshes after a build.
        if isinstance(self.screen, BaseScreen):
            self.screen.refresh_data()

    def show(self, name):
        target = SCREENS[name]
        if type(self.screen) is not target:
            self.switch_screen(target())
            self._socket_send({"type": "screen_changed", "screen": name})
        else:
            self.screen.refresh_data()

    # '!<command>' / bare '!': runs a real shell via App.suspend(), then
    # reports the exit status once the TUI resumes.
    def shell_escape(self, cmdline):
        shell = os.environ.get("SHELL", "/bin/sh")
        argv = [shell, "-c", cmdline] if cmdline.strip() else [shell]
        with self.suspend():
            result = subprocess.run(argv)
        self.say("$ %s  -> exit %d" % (cmdline or shell, result.returncode))

    # textual#5525: a fenced code block's MarkdownFence can call
    # _retheme() before its Static is mounted, raising NoMatches. Not
    # seine's bug (vendored dependency), and the default handler would
    # exit the whole session over it -- log and continue instead. Every
    # other exception still panics as normal.
    def _handle_exception(self, error):
        if _is_markdown_retheme_race(error):
            self.log.warning("ignored a known textual race in "
                             "MarkdownFence._retheme (textual#5525): %s" % error)
            return
        super()._handle_exception(error)

# Split out so the check is testable with a synthetic traceback,
# without needing a live MarkdownFence race to trigger it.
def _is_markdown_retheme_race(error):
    if not isinstance(error, NoMatches):
        return False
    tb = error.__traceback__
    while tb is not None:
        code = tb.tb_frame.f_code
        if code.co_name == "_retheme" and code.co_filename.endswith("_markdown.py"):
            return True
        tb = tb.tb_next
    return False

# Entry point for the TUI. 'argv' may contain --interaction-socket
# (or --interaction-socket=PATH), --remote[=URL] (with --insecure and
# --ca-cert[=PATH] for the connection) followed by zero or
# more spec files; flags are stripped before the rest are treated as specs.
def run(argv=None):
    # Manual parsing, consistent with the rest of the CLI's getopt use.
    spec_files: list[str] = []
    socket_path: str | None = None
    remote_target: str | None = None
    connect_remote: bool = False
    remote_insecure: bool | None = None
    remote_ca_cert: str | None = None
    if argv:
        args = list(argv)
        idx = 0
        while idx < len(args):
            arg = args[idx]
            idx += 1
            if arg.startswith("--interaction-socket"):
                if arg == "--interaction-socket":
                    if idx >= len(args):
                        raise ValueError("--interaction-socket requires a path")
                    socket_path = args[idx]
                    idx += 1
                else:
                    _, _, path = arg.partition("=")
                    if not path:
                        raise ValueError("--interaction-socket requires a path")
                    socket_path = path
                continue
            if arg == "--insecure":
                remote_insecure = True
                continue
            if arg.startswith("--ca-cert"):
                if arg == "--ca-cert":
                    if idx >= len(args):
                        raise ValueError("--ca-cert requires a path")
                    path = args[idx]
                    idx += 1
                else:
                    _, _, path = arg.partition("=")
                if not path:
                    raise ValueError("--ca-cert requires a path")
                from seine import settings
                remote_ca_cert = settings.check_ca_cert(path)
                continue
            if arg.startswith("--remote"):
                connect_remote = True
                if arg == "--remote":
                    if idx < len(args) and not args[idx].startswith("-") and not args[idx].endswith((".yaml", ".yml")):
                        remote_target = args[idx]
                        idx += 1
                else:
                    _, _, r_url = arg.partition("=")
                    if r_url:
                        remote_target = r_url
                continue
            spec_files.append(arg)
    SeineApp(files=spec_files or None, interaction_socket=socket_path,
             remote=remote_target, connect_remote=connect_remote,
             remote_insecure=remote_insecure, remote_ca_cert=remote_ca_cert).run()

