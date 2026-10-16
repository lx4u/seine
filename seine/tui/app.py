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
import time

from textual.app import App
from textual.css.query import NoMatches

from seine.tui import ai, commands
from seine.tui.commands import RegistryProvider
from seine.tui.base import (BaseScreen, DownloadIndicator, Indicators, VendorIndicator,
                            Prompt, RemoteIndicator, TargetIndicator)
from seine.tui.download import DownloadState
from seine.tui.remote_session import RemoteSession
from seine.tui.build import BuildScreen, BuildState
from seine.tui.chat import ChatScreen
from seine.tui.context import AppContext
from seine.tui.filesystem import FilesystemScreen, FilesystemState
from seine.tui.history import History
from seine.tui.issues import IssuesScreen
from seine.tui.overview import OverviewScreen
from seine.tui.vendor import VendorScreen, VendorState
from seine.tui.artifacts import ArtifactsScreen
from seine.tui.cache import CacheScreen
from seine.tui.screens import (AnalyzeScreen, DiffScreen, DoctorScreen,
                               PackagesScreen, PlanScreen)
from seine.tui.server import InteractionServer
from seine.tui.remote_screen import RemoteScreen
from seine.tui.target import TargetState
from seine.tui.target_screen import TargetScreen
from seine.tui.testing import TestScreen, TestState

SCREENS = {"overview": OverviewScreen, "plan": PlanScreen, "build": BuildScreen,
          "artifacts": ArtifactsScreen, "filesystem": FilesystemScreen,
          "packages": PackagesScreen, "analyze": AnalyzeScreen,
          "cache": CacheScreen, "doctor": DoctorScreen, "diff": DiffScreen,
          "issues": IssuesScreen, "chat": ChatScreen, "target": TargetScreen,
          "test": TestScreen, "vendor": VendorScreen, "remote": RemoteScreen}


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
    #prompt, #spectree, #tail, #fslist, #previewpane, #logviewer, #castpane {
        border: round $foreground 40%;
    }
    #prompt:focus, #spectree:focus, #tail:focus, #fslist:focus,
    #previewpane:focus, #logviewer:focus, #castpane:focus {
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
    /* Timeline up top, playing row's arguments below, 2:1 -- the
       arguments pane only shows while a replay with a timeline
       runs, leaving the full height otherwise. */
    #bodypane { height: 2fr; }
    #argspane { height: 1fr; border-top: solid $foreground 40%; }
    #args { padding: 1 2; }
    #tasklist { padding: 1 2; }
    #tail, #logviewer { padding: 0 1; }
    #fslist { height: 1fr; }
    #previewpane { height: 1fr; padding: 1 2; }
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
    """

    def __init__(self, files=None, interaction_socket=None, remote=None, connect_remote=False,
                 remote_insecure=None, remote_ca_cert=None):
        super().__init__()
        self._remote = remote
        self._connect_remote = connect_remote
        self._remote_insecure = remote_insecure
        self._remote_ca_cert = remote_ca_cert
        self.context = AppContext(self)
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
        # Interaction socket server: delegates socket lifecycle and message
        # broadcasting to InteractionServer.
        self._server = InteractionServer(self, interaction_socket)

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

    @property
    def _socket_path(self):
        return self._server.path if hasattr(self, "_server") else None

    @property
    def _socket_server(self):
        return self._server.server if hasattr(self, "_server") else None

    @property
    def _socket_clients(self):
        return self._server.clients if hasattr(self, "_server") else []

    # Broadcasts a JSON message to all connected socket clients.
    def _socket_send(self, data: dict) -> None:
        if hasattr(self, "_server"):
            self._server.send(data)

    # Emits new assistant messages not yet sent, called from
    # AIState.changed after persisting. Streaming chunks are omitted.
    def _socket_send_ai_messages(self) -> None:
        if hasattr(self, "_server"):
            self._server.send_ai_messages()

    # Whichever way the app is left, stop the downloads and forget the remote.
    def exit(self, *args, **kwargs):
        if getattr(self, "_loading_spec", False):
            from seine import tasks
            tasks.interrupt()
            if hasattr(self, "_load_worker") and self._load_worker:
                self._load_worker.cancel()
        self.download_state.cancel()
        self.remote_session.disconnect()
        if hasattr(self, "_server"):
            self._server.close()
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

