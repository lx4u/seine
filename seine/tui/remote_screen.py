# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import hashlib
import os
from typing import Any, Optional
from urllib.parse import urlsplit

import requests
from textual._context import NoActiveAppError
from textual.binding import Binding
from textual.containers import Horizontal
from textual.css.query import NoMatches
from textual.widgets import Static

from seine.distributed.common.transport import check_server_url
from seine.tui.download import DownloadState, redraw
from seine.tui.base import BaseScreen, StaticPane
from seine.tui.render_remote import (
    render_remote_artifacts,
    render_remote_builds,
    render_remote_ops,
    render_remote_projects,
    render_remote_users,
    render_remote_workers,
)
from seine.tui.remote_modals import (
    MemberAssignModal,
    ProjectCreateModal,
    TokenDisplayModal,
    TokenIssueModal,
    UserCreateModal,
)

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
            for tab_id, attr in [
                (1, "remote_builds"),
                (2, "remote_workers"),
                (3, "remote_artifacts"),
                (4, "remote_users"),
                (5, "remote_projects"),
            ]:
                if scr.active_tab == tab_id and idx < len(getattr(scr, attr, [])):
                    scr.selected_indices[tab_id] = idx
                    scr.update_body()
                    break

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
        Binding("n", "admin_new", "New", show=False),
        Binding("t", "admin_issue_token", "Issue PAT", show=False),
        Binding("a", "admin_toggle_admin", "Toggle Admin", show=False),
        Binding("x", "admin_toggle_active", "Toggle Active", show=False),
        Binding("m", "admin_manage_members", "Members", show=False),
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
        self.remote_artifacts: list[dict[str, Any]] = []
        self.remote_users: list[dict[str, Any]] = []
        self.remote_tokens: list[dict[str, Any]] = []
        self.remote_projects: list[dict[str, Any]] = []
        self.remote_ops_settings: dict[str, Any] = {}
        self.remote_ops_stats: dict[str, Any] = {}

    @property
    def builds(self) -> list[dict[str, Any]]:
        return self.remote_builds

    @builds.setter
    def builds(self, value: list[dict[str, Any]]):
        self.remote_builds = value

    @property
    def artifacts(self) -> list[dict[str, Any]]:
        return self.remote_artifacts

    @artifacts.setter
    def artifacts(self, value: list[dict[str, Any]]):
        self.remote_artifacts = value

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
        for tab_id, items in [
            (1, self.remote_builds),
            (2, self.remote_workers),
            (3, self.remote_artifacts),
            (4, self.remote_users),
            (5, self.remote_projects),
        ]:
            idx = self.selected_indices.get(tab_id, 0)
            self.selected_indices[tab_id] = max(0, min(idx, len(items) - 1)) if items else 0

    def _selected_item(self) -> Optional[dict[str, Any]]:
        tab_to_list = {
            1: self.remote_builds,
            2: self.remote_workers,
            3: self.remote_artifacts,
            4: self.remote_users,
            5: self.remote_projects,
        }
        items = tab_to_list.get(self.active_tab, [])
        idx = self.selected_indices.get(self.active_tab, 0)
        return items[idx] if (items and 0 <= idx < len(items)) else None

    def fetch_data(self):
        session = getattr(self.app, "remote_session", None)
        if not (session and session.connected):
            self.remote_builds = []
            self.remote_workers = []
            self.remote_artifacts = []
            self.remote_users = []
            self.remote_tokens = []
            self.remote_projects = []
            self.remote_ops_settings = {}
            self.remote_ops_stats = {}
            self.update_body()
            return

        def _worker():
            try:
                if self.active_tab in (1, 3):
                    resp = session.request(
                        "get", "/api/v1/builds",
                        timeout=5.0,
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        self.remote_builds = data if isinstance(data, list) else []
                        arts: list[dict[str, Any]] = []
                        for b in self.remote_builds:
                            b_id = str(b.get("id") or b.get("build_id") or "")
                            proj = str(b.get("project") or "")
                            arch = str(b.get("target_arch") or b.get("architecture") or "")
                            for m in (b.get("artifact_meta") or []):
                                if isinstance(m, dict):
                                    arts.append({
                                        "name": m.get("name", "artifact"),
                                        "size": m.get("size", 0),
                                        "sha256": m.get("sha256", ""),
                                        "key": m.get("key", ""),
                                        "build_id": b_id,
                                        "project": proj,
                                        "target_arch": arch,
                                    })
                        self.remote_artifacts = arts
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
                elif self.active_tab == 4 and session.is_admin:
                    # fetch users and all PATs in parallel within the same worker
                    r_users = session.request(
                        "get", "/api/v1/users",
                        timeout=5.0,
                    )
                    if r_users.status_code == 200:
                        self.remote_users = r_users.json() or []
                    r_tokens = session.request(
                        "get", "/api/v1/tokens",
                        timeout=5.0,
                    )
                    if r_tokens.status_code == 200:
                        self.remote_tokens = r_tokens.json() or []
                elif self.active_tab == 5 and session.is_admin:
                    r_proj = session.request(
                        "get", "/api/v1/projects",
                        timeout=5.0,
                    )
                    if r_proj.status_code == 200:
                        self.remote_projects = r_proj.json() or []
                elif self.active_tab == 6 and session.is_admin:
                    # aggregate queue stats from builds list; no dedicated endpoint yet
                    r_builds = session.request(
                        "get", "/api/v1/builds",
                        timeout=5.0,
                    )
                    if r_builds.status_code == 200:
                        all_builds = r_builds.json() or []
                    else:
                        all_builds = []
                    r_workers = session.request(
                        "get", "/api/v1/workers",
                        timeout=5.0,
                    )
                    workers_data = []
                    if r_workers.status_code == 200:
                        wd = r_workers.json()
                        workers_data = wd.get("workers", []) if isinstance(wd, dict) else wd or []
                    stats: dict[str, Any] = {"total_builds": len(all_builds)}
                    for st in ("queued", "running", "completed", "failed", "cancelled"):
                        stats[st] = sum(
                            1 for b in all_builds
                            if (b.get("status") or "").lower() in (st, st.rstrip("led") + "ling")
                        )
                    online_w = [w for w in workers_data if (w.get("status") or "online") == "online"]
                    paused_w = [w for w in workers_data if (w.get("status") or "") == "paused"]
                    stats["workers_online"] = len(online_w)
                    stats["workers_paused"] = len(paused_w)
                    stats["total_slots"] = sum(w.get("concurrency_slots", 1) for w in workers_data)
                    stats["free_disk_gb"] = sum(float(w.get("free_disk_gb", 0.0) or 0.0) for w in online_w)
                    self.remote_ops_stats = stats
                    self.remote_ops_settings = {
                        "url": server_host(session.url),
                        "user_id": session.user_id,
                        "is_admin": session.is_admin,
                        "active_project": session.active_project,
                        "ping_ms": session.ping_ms,
                    }
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
        if self.active_tab == 3:
            state = self._download_state()
            return render_remote_artifacts(
                self.remote_artifacts, self.selected_indices.get(3, 0),
                state.snapshot() if state else None)
        if self.active_tab == 4:
            return render_remote_users(self.remote_users, self.remote_tokens, self.selected_indices.get(4, 0))
        if self.active_tab == 5:
            return render_remote_projects(self.remote_projects, self.selected_indices.get(5, 0))
        if self.active_tab == 6:
            return render_remote_ops(self.remote_ops_settings, self.remote_ops_stats)

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
        items = {
            1: self.remote_builds,
            2: self.remote_workers,
            3: self.remote_artifacts,
            4: self.remote_users,
            5: self.remote_projects,
        }.get(self.active_tab, [])
        idx = self.selected_indices.get(self.active_tab, 0)
        if idx < len(items) - 1:
            self.selected_indices[self.active_tab] = idx + 1
            self.update_body()

    def _download_state(self) -> Optional[DownloadState]:
        state = getattr(self.app, "download_state", None)
        return state if isinstance(state, DownloadState) else None

    def _download_changed(self):
        """Redraw the status bar and the artifacts tab; callable from a worker thread."""
        if getattr(self.app, "is_running", False) is True and hasattr(self.app, "call_from_thread"):
            try:
                self.app.call_from_thread(redraw, self.app)
            except RuntimeError:
                pass  # the app is closing
            return
        redraw(self.app)

    def _notify_say(self, msg: str, error: bool = False, warning: bool = False):
        if getattr(self.app, "is_running", False) is True and hasattr(self.app, "call_from_thread"):
            try:
                self.app.call_from_thread(self.say, msg, error=error, warning=warning)
                return
            except RuntimeError:
                pass
        if warning:
            self.say(msg, warning=True)
        elif error:
            self.say(msg, error=True)
        else:
            self.say(msg, error=False)

    def action_view_logs(self):
        if self.active_tab == 1:
            build = self._selected_item()
            if not build:
                self.say("no build selected", warning=True)
                return
            build_id = str(build.get("id") or build.get("build_id") or "")
            self.say(f"streaming logs for build {build_id[:12]}...")
        elif self.active_tab == 3:
            item = self._selected_item()
            if not item:
                self.say("no artifact selected", warning=True)
                return
            build_id = str(item.get("build_id") or "")
            self._trigger_download(build_id, artifact_name=item.get("name"))

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
        if self.active_tab not in (1, 3):
            return
        item = self._selected_item()
        if not item:
            lbl = "build" if self.active_tab == 1 else "artifact"
            self.say(f"no {lbl} selected", warning=True)
            return
        build_id = str(item.get("id") or item.get("build_id") or "")
        self._trigger_download(build_id, artifact_name=None)

    def _trigger_download(self, build_id: str, artifact_name: Optional[str] = None):
        session = getattr(self.app, "remote_session", None)
        if not (session and session.connected):
            self.say("not connected to a remote server", warning=True)
            return
        if not build_id:
            self.say("no build associated with selection", warning=True)
            return

        short_id = build_id[:12]
        target_dir = getattr(self.app, "download_dir", None) or "./deploy"

        def _worker():
            try:
                resp = session.request(
                    "get", f"/api/v1/builds/{build_id}",
                    timeout=5.0,
                )
                if resp.status_code != 200:
                    self._notify_say(f"failed to fetch build details: HTTP {resp.status_code}", error=True)
                    return
                info = resp.json()
            except Exception as e:
                self._notify_say(f"failed to fetch build {short_id}: {e}", error=True)
                return

            if info.get("status") != "completed":
                st = info.get("status", "unknown")
                self._notify_say(f"build {short_id} has no artifacts (status: {st})", warning=True)
                return

            download_urls = info.get("download_urls") or {}
            if artifact_name:
                download_urls = {k: v for k, v in download_urls.items() if k == artifact_name}

            if not download_urls:
                target_desc = f"artifact '{artifact_name}'" if artifact_name else "artifacts"
                self._notify_say(f"no download URLs available for {target_desc} in build {short_id}", warning=True)
                return

            manifest = {
                a["name"]: a
                for a in (info.get("artifacts") or [])
                if isinstance(a, dict) and "name" in a
            }

            os.makedirs(target_dir, exist_ok=True)
            count = len(download_urls)
            done = 0
            errors = []
            progress = self._download_state()
            if progress:
                for name in download_urls:
                    progress.queue(build_id, name, (manifest.get(name) or {}).get("size"))
                self._download_changed()

            for name, url in download_urls.items():
                expected = manifest.get(name) or {}
                dest = os.path.join(target_dir, name)
                part = f"{dest}.part"
                if progress:
                    progress.start(build_id, name)
                    self._download_changed()
                try:
                    if progress and progress.cancelled:
                        raise RuntimeError("cancelled")
                    try:
                        check_server_url(url, insecure=session.insecure)
                    except ValueError as e:
                        raise RuntimeError(f"{e} (or '/set remote_insecure true')") from e
                    with requests.get(
                        url, stream=True, timeout=(5.0, 30.0), verify=session.verify,
                    ) as r:
                        if r.status_code != 200:
                            raise RuntimeError(f"HTTP {r.status_code}")
                        sha256 = hashlib.sha256()
                        size = 0
                        with open(part, "wb") as f:
                            for chunk in r.iter_content(chunk_size=65536):
                                f.write(chunk)
                                sha256.update(chunk)
                                size += len(chunk)
                                if progress and progress.cancelled:
                                    raise RuntimeError("cancelled")
                                if progress and progress.advance(build_id, name, len(chunk)):
                                    self._download_changed()

                    expected_sha = (expected.get("sha256") or "").lower()
                    expected_size = expected.get("size")
                    if expected_sha and sha256.hexdigest() != expected_sha:
                        os.replace(part, f"{dest}.corrupt")
                        raise RuntimeError("checksum verification failed")
                    if expected_size is not None and size != expected_size:
                        os.replace(part, f"{dest}.corrupt")
                        raise RuntimeError("size mismatch")

                    os.replace(part, dest)
                    done += 1
                    if progress:
                        progress.finish(build_id, name)
                except Exception as e:
                    if progress:
                        progress.finish(build_id, name, failed=True)
                    if os.path.exists(part):
                        try:
                            os.remove(part)
                        except OSError:
                            pass
                    errors.append(f"{name}: {e}")

            if progress:
                self._download_changed()
            if errors:
                msg = f"downloaded {done}/{count} artifact(s); failed: {'; '.join(errors)}"
                self._notify_say(msg, error=True)
            else:
                art_label = f"artifact '{artifact_name}'" if artifact_name else f"{done} artifact(s)"
                self._notify_say(f"downloaded {art_label} to {target_dir}", error=False)

        if getattr(self.app, "is_running", False) is True and hasattr(self.app, "run_worker"):
            self.app.run_worker(_worker, thread=True)
        else:
            _worker()

    def _require_admin(self) -> bool:
        session = getattr(self.app, "remote_session", None)
        if not (session and session.connected):
            self.say("not connected to a remote server", warning=True)
            return False
        if not session.is_admin:
            self.say("admin privileges required", warning=True)
            return False
        return True

    def _run_admin_request(self, fn, on_success: str):
        """Run an admin API call in a background worker; fetch_data on success."""
        def _worker():
            try:
                msg, err = fn()
            except Exception as e:
                msg, err = str(e), True

            def _notify():
                self.say(msg, error=err)
                if not err:
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

    def action_admin_new(self):
        """Open creation modal for the active admin tab (users: n, projects: n)."""
        if not self._require_admin():
            return
        session = getattr(self.app, "remote_session", None)

        if self.active_tab == 4:
            # create a new user account
            def _on_result(result):
                if not result:
                    return
                uid = result["username"]
                is_admin = result.get("is_admin", False)

                def _call():
                    try:
                        resp = session.request(
                            "post", "/api/v1/users",
                            json={"id": uid, "is_admin": is_admin},
                            timeout=5.0,
                        )
                        if resp.status_code in (200, 201):
                            return f"user '{uid}' created", False
                        return f"create failed ({resp.status_code}): {resp.text}", True
                    except Exception as e:
                        return f"create failed: {e}", True

                self._run_admin_request(_call, f"user '{uid}' created")

            if getattr(self.app, "is_running", False) is True:
                self.app.push_screen(UserCreateModal(), _on_result)

        elif self.active_tab == 5:
            # create a new project
            def _on_result(result):
                if not result:
                    return
                name = result["name"]
                payload = {k: v for k, v in result.items() if v is not None}

                def _call():
                    try:
                        resp = session.request(
                            "post", "/api/v1/projects",
                            json=payload,
                            timeout=5.0,
                        )
                        if resp.status_code in (200, 201):
                            return f"project '{name}' created", False
                        return f"create failed ({resp.status_code}): {resp.text}", True
                    except Exception as e:
                        return f"create failed: {e}", True

                self._run_admin_request(_call, f"project '{name}' created")

            if getattr(self.app, "is_running", False) is True:
                self.app.push_screen(ProjectCreateModal(), _on_result)

    def action_admin_issue_token(self):
        """Issue a PAT for the selected user (tab 4 only)."""
        if self.active_tab != 4 or not self._require_admin():
            return
        session = getattr(self.app, "remote_session", None)
        user = self._selected_item()
        if not user:
            self.say("no user selected", warning=True)
            return
        user_id = str(user.get("id") or "")

        def _on_issue(result):
            if not result:
                return
            days = result.get("days")

            def _call():
                try:
                    payload: dict = {"user_id": user_id, "kind": "pat"}
                    if days is not None:
                        payload["days"] = days
                    resp = session.request(
                        "post", "/api/v1/tokens",
                        json=payload,
                        timeout=5.0,
                    )
                    if resp.status_code in (200, 201):
                        return resp.json().get("token", ""), False
                    return f"issue failed ({resp.status_code}): {resp.text}", True
                except Exception as e:
                    return f"issue failed: {e}", True

            def _on_token(token_or_err):
                if not isinstance(token_or_err, str):
                    return
                if " failed" in token_or_err:
                    self.say(token_or_err, error=True)
                    return
                # display token once — never stored again after this modal
                if getattr(self.app, "is_running", False) is True:
                    self.app.push_screen(TokenDisplayModal(token_or_err))
                self.fetch_data()

            def _worker():
                token_or_err, err = _call()
                def _notify():
                    _on_token(token_or_err if not err else f"issue failed: {token_or_err}")
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

        if getattr(self.app, "is_running", False) is True:
            self.app.push_screen(TokenIssueModal(user_id=user_id), _on_issue)

    def action_admin_toggle_admin(self):
        """Toggle global admin flag on the selected user (tab 4 only)."""
        if self.active_tab != 4 or not self._require_admin():
            return
        session = getattr(self.app, "remote_session", None)
        user = self._selected_item()
        if not user:
            self.say("no user selected", warning=True)
            return
        user_id = str(user.get("id") or "")
        new_admin = not bool(user.get("is_admin"))

        def _call():
            try:
                resp = session.request(
                    "patch", f"/api/v1/users/{user_id}",
                    json={"is_admin": new_admin},
                    timeout=5.0,
                )
                if resp.status_code == 200:
                    label = "granted" if new_admin else "revoked"
                    return f"admin {label} for '{user_id}'", False
                return f"update failed ({resp.status_code}): {resp.text}", True
            except Exception as e:
                return f"update failed: {e}", True

        self._run_admin_request(_call, "")

    def action_admin_toggle_active(self):
        """Toggle active/disabled state on the selected user (tab 4 only)."""
        if self.active_tab != 4 or not self._require_admin():
            return
        session = getattr(self.app, "remote_session", None)
        user = self._selected_item()
        if not user:
            self.say("no user selected", warning=True)
            return
        user_id = str(user.get("id") or "")
        new_active = not bool(user.get("active", True))

        def _call():
            try:
                resp = session.request(
                    "patch", f"/api/v1/users/{user_id}",
                    json={"active": new_active},
                    timeout=5.0,
                )
                if resp.status_code == 200:
                    label = "activated" if new_active else "deactivated"
                    return f"user '{user_id}' {label}", False
                return f"update failed ({resp.status_code}): {resp.text}", True
            except Exception as e:
                return f"update failed: {e}", True

        self._run_admin_request(_call, "")

    def action_admin_manage_members(self):
        """Open member assignment modal for the selected project (tab 5 only)."""
        if self.active_tab != 5 or not self._require_admin():
            return
        session = getattr(self.app, "remote_session", None)
        project = self._selected_item()
        if not project:
            self.say("no project selected", warning=True)
            return
        project_id = str(project.get("name") or project.get("id") or "")

        def _on_result(result):
            if not result:
                return
            uid = result["user_id"]
            role = result["role"]

            def _call():
                try:
                    resp = session.request(
                        "post", f"/api/v1/projects/{project_id}/members",
                        json={"user_id": uid, "role": role},
                        timeout=5.0,
                    )
                    if resp.status_code in (200, 201):
                        return f"assigned '{uid}' as {role} in '{project_id}'", False
                    return f"assign failed ({resp.status_code}): {resp.text}", True
                except Exception as e:
                    return f"assign failed: {e}", True

            self._run_admin_request(_call, "")

        if getattr(self.app, "is_running", False) is True:
            self.app.push_screen(MemberAssignModal(project_id=project_id), _on_result)

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

