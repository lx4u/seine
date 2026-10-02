# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

from urllib.parse import urlsplit

from textual.binding import Binding
from textual.containers import Horizontal
from textual.css.query import NoMatches
from textual.widgets import Static

from seine.tui.base import BaseScreen, StaticPane

def server_host(url: str) -> str:
    """Just the host of a server URL, for display."""
    return urlsplit(url).hostname or url


class RemotePane(StaticPane):
    can_focus = True

class SidebarStatic(Static):
    def __init__(self, **kwargs):
        kwargs.setdefault("markup", False)
        super().__init__(**kwargs)

    def on_click(self, event):
        try:
            scr = self.screen
        except Exception:
            scr = getattr(self, "_screen", None)
        if not scr:
            return
        lines = str(self.renderable or "").split("\n")
        if 0 <= event.y < len(lines):
            line = lines[event.y]
            for num in range(1, 7):
                if f"[^{num}]" in line:
                    if hasattr(scr, "action_select_tab"):
                        scr.action_select_tab(num)
                    break

class RemoteScreen(BaseScreen):
    BINDINGS = BaseScreen.BINDINGS + [
        Binding("ctrl+1", "select_tab(1)", "Builds", show=False),
        Binding("ctrl+2", "select_tab(2)", "Workers", show=False),
        Binding("ctrl+3", "select_tab(3)", "Artifacts", show=False),
        Binding("ctrl+4", "select_tab(4)", "Users", show=False),
        Binding("ctrl+5", "select_tab(5)", "Projects", show=False),
        Binding("ctrl+6", "select_tab(6)", "Server Ops", show=False),
        Binding("s", "switch_project", "Switch Project", show=False),
    ]

    HINT_UPDATE = {"tab": "Tab switch pane · ^1-^6 tabs · s project"}

    TABS = {
        1: "Builds",
        2: "Workers",
        3: "Artifacts",
        4: "Users",
        5: "Projects",
        6: "Server / Ops",
    }

    _app_ref = None

    # A worker thread can outlive this screen, once another one replaces it:
    # it must still find the app.
    @property
    def app(self):
        try:
            app = super().app
        except NoActiveAppError:
            return self._app_ref
        self._app_ref = app
        return app

    def say(self, text, error=False, warning=False):
        try:
            super().say(text, error=error, warning=warning)
        except NoMatches:
            self.app.say(text, error=error, warning=warning)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.active_tab = 1

    def compose(self):
        yield Horizontal(
            RemotePane(Static(id="remotebody", markup=False), id="remotemain"),
            RemotePane(SidebarStatic(id="sidebarbody", markup=False), id="remotesidebar"),
            id="main",
        )
        yield from self.footer()

    def _render_sidebar(self):
        session = getattr(self.app, "remote_session", None)
        connected = bool(session and session.connected)
        is_admin = bool(connected and session.is_admin)

        lines = [" REMOTE COCKPIT", " ──────────────────────"]
        if connected:
            lines.append(f" Server:  {server_host(session.url)}")
            if session.ping_ms is not None:
                lines.append(f" Latency: {session.ping_ms}ms")
            user_label = session.user_id or "authenticated"
            if is_admin:
                user_label += " [admin]"
            lines.append(f" User:    {user_label}")
            lines.append(f" Project: {session.active_project or 'none'}")
        else:
            lines.append(" Status: Disconnected")
            lines.append(" (local engine mode)")

        lines.extend(["", " NAVIGATION", " ──────────────────────"])
        for tab_id in (1, 2, 3):
            prefix = " ▸ " if tab_id == self.active_tab else "   "
            lines.append(f"{prefix}[^{tab_id}] {self.TABS[tab_id]}")

        if is_admin:
            lines.extend(["", " SUPER-POWERS", " ──────────────────────"])
            for tab_id in (4, 5, 6):
                prefix = " ▸ " if tab_id == self.active_tab else "   "
                lines.append(f"{prefix}[^{tab_id}] {self.TABS[tab_id]}")

        lines.append("")
        return "\n".join(lines)

    def _render_main(self):
        session = getattr(self.app, "remote_session", None)
        connected = bool(session and session.connected)
        tab_name = self.TABS.get(self.active_tab, "Unknown")

        lines = [
            f" REMOTE {tab_name.upper()}",
            " ═════════════════════════════════════════════════════════════",
        ]
        if not connected:
            lines.append("")
            lines.append(" No active remote server connection.")
            lines.append(" Use '/remote <url>' or press Ctrl+R to connect to a remote cluster.")
            lines.append("")
            return "\n".join(lines)

        lines.append(f" Active view: {tab_name}")
        lines.append(f" Target cluster: {session.url}")
        if session.active_project:
            lines.append(f" Active project: {session.active_project}")
        lines.append("")
        lines.append(f" [Sub-screen {tab_name} ready. Press ctrl-1 to ctrl-6 or Tab to navigate.]")
        lines.append("")
        return "\n".join(lines)

    def update_body(self):
        try:
            main_static = self.query_one("#remotebody", Static)
            sidebar_static = self.query_one("#sidebarbody", Static)
        except Exception:
            return
        main_static.update(self._render_main())
        sidebar_static.update(self._render_sidebar())

    def action_select_tab(self, tab: int):
        session = getattr(self.app, "remote_session", None)
        is_admin = bool(session and session.connected and session.is_admin)
        if tab in (4, 5, 6) and not is_admin:
            self.say(f"admin privileges required for tab {tab}", warning=True)
            return
        if tab not in self.TABS:
            return
        self.active_tab = tab
        self.update_body()

    def action_switch_project(self):
        from seine.tui import commands
        try:
            commands.dispatch(self.app, "/project")
        except commands.CommandError as e:
            self.say(str(e), warning=True)
