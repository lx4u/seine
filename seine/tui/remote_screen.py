# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

from typing import Any, Optional
from urllib.parse import urlsplit

import requests
from textual._context import NoActiveAppError
from textual.binding import Binding
from textual.containers import Horizontal
from textual.css.query import NoMatches
from textual.widgets import Static

from seine.tui.base import BaseScreen, StaticPane
from seine.tui.render_remote import render_remote_builds, render_remote_workers

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

class RemoteBodyStatic(Static):
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
        idx = event.y - 4
        if idx >= 0:
            if scr.active_tab == 1 and idx < len(getattr(scr, "remote_builds", [])):
                scr.selected_indices[1] = idx
                scr.update_body()
            elif scr.active_tab == 2 and idx < len(getattr(scr, "remote_workers", [])):
                scr.selected_indices[2] = idx
                scr.update_body()

class RemoteScreen(BaseScreen):
    BINDINGS = BaseScreen.BINDINGS + [
        Binding("ctrl+1", "select_tab(1)", "Builds", show=False),
        Binding("ctrl+2", "select_tab(2)", "Workers", show=False),
        Binding("ctrl+3", "select_tab(3)", "Artifacts", show=False),
        Binding("ctrl+4", "select_tab(4)", "Users", show=False),
        Binding("ctrl+5", "select_tab(5)", "Projects", show=False),
        Binding("ctrl+6", "select_tab(6)", "Server Ops", show=False),
        Binding("up", "cursor_up", "Up", show=False),
        Binding("down", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("j", "cursor_down", "Down", show=False),
        Binding("enter", "view_logs", "View Logs", show=False),
        Binding("c", "cancel_build", "Cancel Build", show=False),
        Binding("d", "download_artifact", "Download Artifact", show=False),
        Binding("p", "toggle_worker_pause", "Pause Worker", show=False),
        Binding("r", "deregister_worker", "Deregister Worker", show=False),
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
        self.selected_indices: dict[int, int] = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0, 6: 0}
        self.remote_builds: list[dict[str, Any]] = []
        self.remote_workers: list[dict[str, Any]] = []

    @property
    def builds(self) -> list[dict[str, Any]]:
        return self.remote_builds

    @builds.setter
    def builds(self, value: list[dict[str, Any]]):
        self.remote_builds = value

    def compose(self):
        yield Horizontal(
            RemotePane(RemoteBodyStatic(id="remotebody"), id="remotemain"),
            RemotePane(SidebarStatic(id="sidebarbody"), id="remotesidebar"),
            id="main",
        )
        yield from self.footer()

    def on_mount(self):
        super().on_mount()
        self.fetch_data()
        try:
            self.set_interval(5.0, self.fetch_data)
        except Exception:
            pass

    def _clamp_selections(self):
        # Prevent out-of-range cursor when roster size shrinks after API polling.
        for tab_id, items in [(1, self.remote_builds), (2, self.remote_workers)]:
            idx = self.selected_indices.get(tab_id, 0)
            self.selected_indices[tab_id] = max(0, min(idx, len(items) - 1)) if items else 0

    def _selected_item(self) -> Optional[dict[str, Any]]:
        items = (
            self.remote_builds
            if self.active_tab == 1
            else (self.remote_workers if self.active_tab == 2 else [])
        )
        idx = self.selected_indices.get(self.active_tab, 0)
        return items[idx] if (items and 0 <= idx < len(items)) else None

    def fetch_data(self):
        session = getattr(self.app, "remote_session", None)
        if not (session and session.connected):
            self.remote_builds = []
            self.remote_workers = []
            self.update_body()
            return

        def _worker():
            try:
                if self.active_tab == 1:
                    resp = session.request(
                        "get", "/api/v1/builds",
                        timeout=5.0,
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        self.remote_builds = data if isinstance(data, list) else []
                elif self.active_tab == 2:
                    resp = session.request(
                        "get", "/api/v1/workers",
                        timeout=5.0,
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        self.remote_workers = (
                            data.get("workers", [])
                            if isinstance(data, dict)
                            else (data if isinstance(data, list) else [])
                        )
            except Exception:
                pass

            def _finish():
                self._clamp_selections()
                self.update_body()

            # Marshal back to UI thread only if event loop is actively running.
            if getattr(self.app, "is_running", False) is True and hasattr(self.app, "call_from_thread"):
                try:
                    self.app.call_from_thread(_finish)
                    return
                except RuntimeError:
                    pass
            _finish()

        if getattr(self.app, "is_running", False) is True and hasattr(self.app, "run_worker"):
            self.app.run_worker(_worker, thread=True)
        else:
            _worker()

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

        if not connected:
            lines = [
                f" REMOTE {tab_name.upper()}",
                " ═════════════════════════════════════════════════════════════",
                "",
                " No active remote server connection.",
                " Use '/remote <url>' or press Ctrl+R to connect to a remote cluster.",
                "",
            ]
            return "\n".join(lines)

        if self.active_tab == 1:
            return render_remote_builds(self.remote_builds, self.selected_indices.get(1, 0))
        if self.active_tab == 2:
            return render_remote_workers(self.remote_workers, self.selected_indices.get(2, 0))

        lines = [
            f" REMOTE {tab_name.upper()}",
            " ═════════════════════════════════════════════════════════════",
            f" Active view: {tab_name}",
            f" Target cluster: {server_host(session.url)}",
        ]
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
        self.fetch_data()
        self.update_body()

    def action_switch_project(self):
        from seine.tui import commands
        try:
            commands.dispatch(self.app, "/project")
        except commands.CommandError as e:
            self.say(str(e), warning=True)

    def action_cursor_up(self):
        idx = self.selected_indices.get(self.active_tab, 0)
        if idx > 0:
            self.selected_indices[self.active_tab] = idx - 1
            self.update_body()

    def action_cursor_down(self):
        items = (
            self.remote_builds
            if self.active_tab == 1
            else (self.remote_workers if self.active_tab == 2 else [])
        )
        idx = self.selected_indices.get(self.active_tab, 0)
        if idx < len(items) - 1:
            self.selected_indices[self.active_tab] = idx + 1
            self.update_body()

    def action_view_logs(self):
        if self.active_tab != 1:
            return
        build = self._selected_item()
        if not build:
            self.say("no build selected", warning=True)
            return
        build_id = str(build.get("id") or build.get("build_id") or "")
        self.say(f"streaming logs for build {build_id[:12]}...")

    def action_cancel_build(self):
        if self.active_tab != 1:
            return
        session = getattr(self.app, "remote_session", None)
        if not (session and session.connected):
            self.say("not connected to a remote server", warning=True)
            return
        build = self._selected_item()
        if not build:
            self.say("no build selected", warning=True)
            return
        build_id = str(build.get("id") or build.get("build_id") or "")
        short_id = build_id[:12]

        def _worker():
            try:
                resp = session.request(
                    "post", f"/api/v1/builds/{build_id}/cancel",
                    timeout=5.0,
                )
                if resp.status_code == 200:
                    msg, err = f"build {short_id} cancel requested", False
                else:
                    msg, err = f"cancel failed ({resp.status_code}): {resp.text}", True
            except Exception as e:
                msg, err = f"cancel failed: {e}", True

            def _notify():
                self.say(msg, error=err)
                self.fetch_data()

            if getattr(self.app, "is_running", False) is True and hasattr(self.app, "call_from_thread"):
                try:
                    self.app.call_from_thread(_notify)
                    return
                except RuntimeError:
                    pass
            _notify()

        if getattr(self.app, "is_running", False) is True and hasattr(self.app, "run_worker"):
            self.app.run_worker(_worker, thread=True)
        else:
            _worker()

    def action_download_artifact(self):
        if self.active_tab != 1:
            return
        build = self._selected_item()
        if not build:
            self.say("no build selected", warning=True)
            return
        build_id = str(build.get("id") or build.get("build_id") or "")
        self.say(f"downloading artifacts for build {build_id[:12]}...")

    def action_toggle_worker_pause(self):
        if self.active_tab != 2:
            return
        session = getattr(self.app, "remote_session", None)
        if not (session and session.connected):
            self.say("not connected to a remote server", warning=True)
            return
        if not session.is_admin:
            self.say("admin privileges required to manage workers", warning=True)
            return
        worker = self._selected_item()
        if not worker:
            self.say("no worker selected", warning=True)
            return
        worker_id = str(worker.get("id") or worker.get("worker_id") or "")
        short_id = worker_id[:12]
        st = (worker.get("status") or "online").lower()
        target_paused = st != "paused"

        def _worker():
            try:
                resp = session.request(
                    "post", f"/api/v1/workers/{worker_id}/pause",
                    json={"paused": target_paused},
                    timeout=5.0,
                )
                if resp.status_code == 200:
                    status_lbl = "paused" if target_paused else "resumed"
                    msg, err = f"worker {short_id} {status_lbl}", False
                else:
                    msg, err = f"pause failed ({resp.status_code}): {resp.text}", True
            except Exception as e:
                msg, err = f"pause failed: {e}", True

            def _notify():
                self.say(msg, error=err)
                self.fetch_data()

            if getattr(self.app, "is_running", False) is True and hasattr(self.app, "call_from_thread"):
                try:
                    self.app.call_from_thread(_notify)
                    return
                except RuntimeError:
                    pass
            _notify()

        if getattr(self.app, "is_running", False) is True and hasattr(self.app, "run_worker"):
            self.app.run_worker(_worker, thread=True)
        else:
            _worker()

    def action_deregister_worker(self):
        if self.active_tab != 2:
            return
        session = getattr(self.app, "remote_session", None)
        if not (session and session.connected):
            self.say("not connected to a remote server", warning=True)
            return
        if not session.is_admin:
            self.say("admin privileges required to deregister workers", warning=True)
            return
        worker = self._selected_item()
        if not worker:
            self.say("no worker selected", warning=True)
            return
        worker_id = str(worker.get("id") or worker.get("worker_id") or "")
        short_id = worker_id[:12]

        def _worker():
            try:
                resp = session.request(
                    "delete", f"/api/v1/workers/{worker_id}",
                    timeout=5.0,
                )
                if resp.status_code == 200:
                    msg, err = f"worker {short_id} deregistered", False
                else:
                    msg, err = f"deregister failed ({resp.status_code}): {resp.text}", True
            except Exception as e:
                msg, err = f"deregister failed: {e}", True

            def _notify():
                self.say(msg, error=err)
                self.fetch_data()

            if getattr(self.app, "is_running", False) is True and hasattr(self.app, "call_from_thread"):
                try:
                    self.app.call_from_thread(_notify)
                    return
                except RuntimeError:
                    pass
            _notify()

        if getattr(self.app, "is_running", False) is True and hasattr(self.app, "run_worker"):
            self.app.run_worker(_worker, thread=True)
        else:
            _worker()

