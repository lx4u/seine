# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import os
import time
from typing import Any, Optional, Union
from urllib.parse import urlsplit

import requests

from seine import settings
from seine.credentials import CredentialNotFound, token_source
from seine.distributed.client.events import EventFollower
from seine.distributed.common.transport import (
    MAX_TOKEN_REJECTIONS,
    check_server_url,
    is_loopback,
    requests_verify,
)


def normalize_url(url: str) -> str:
    """Add the scheme a bare host lacks: http for loopback, https otherwise."""
    if not url.startswith(("http://", "https://")):
        scheme = "http" if is_loopback(urlsplit(f"//{url}").hostname or "") else "https"
        url = f"{scheme}://{url}"
    return url.rstrip("/")


class RemoteSession:
    """Encapsulates active remote server connection state, auth, and latency."""

    def __init__(self, app=None):
        self.app = app
        self.url: Optional[str] = None
        self.token: Optional[str] = None
        self.connected: bool = False
        self.user_id: Optional[str] = None
        self.is_admin: bool = False
        self.projects: dict[str, str] = {}
        self.default_project: Optional[str] = None
        self.active_project: Optional[str] = None
        self.ping_ms: Optional[float] = None
        self.last_error: Optional[str] = None
        self.warning: Optional[str] = None
        # Set by /remote --insecure and --ca-cert; unset, the settings apply as they are now.
        self._insecure: Optional[bool] = None
        self._ca_cert: Optional[str] = None
        self._follower: Optional[EventFollower] = None

    @property
    def insecure(self) -> bool:
        if self._insecure is not None:
            return self._insecure
        return bool(settings.load()["remote_insecure"])

    @insecure.setter
    def insecure(self, value: Optional[bool]) -> None:
        self._insecure = value

    @property
    def ca_cert(self) -> Optional[str]:
        return (self._ca_cert or settings.load()["remote_ca_cert"]
                or os.environ.get("SEINE_CA_CERT") or None)

    @ca_cert.setter
    def ca_cert(self, value: Optional[str]) -> None:
        self._ca_cert = value

    @property
    def verify(self) -> Union[str, bool]:
        return requests_verify(self.ca_cert)

    @property
    def auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        """Call the connected server with its token, TLS policy and a timeout."""
        kwargs.setdefault("timeout", 5.0)
        kwargs.setdefault("verify", self.verify)
        kwargs["headers"] = {**self.auth_headers, **kwargs.get("headers", {})}
        return getattr(requests, method.lower())(f"{self.url}{path}", **kwargs)

    def _fail(self, reason: str) -> bool:
        self.connected = False
        self.last_error = reason
        self.notify_indicators()
        return False

    def connect(
        self,
        url: str,
        token: Optional[str] = None,
        insecure: Optional[bool] = None,
        ca_cert: Optional[str] = None,
    ) -> bool:
        """Validate token and populate user_id, is_admin, and projects via GET /api/v1/me.

        Gives up after MAX_TOKEN_REJECTIONS rejected tokens; the next call
        starts counting again.
        """
        current = settings.load()
        asked_insecure, asked_ca_cert = insecure, ca_cert
        if insecure is None:
            insecure = bool(current["remote_insecure"])
        ca_cert = ca_cert or current["remote_ca_cert"] or os.environ.get("SEINE_CA_CERT") or None
        normalized = normalize_url(url)
        try:
            check_server_url(normalized, insecure=insecure)
        except ValueError as e:
            return self._fail(f"{e} (or '/set remote_insecure true')")
        verify = requests_verify(ca_cert)

        prompt = None
        if self.app:
            try:
                from seine.tui.credentials import tui_prompt
                prompt = tui_prompt(self.app)
            except Exception:
                prompt = None

        src = None
        token_val = token
        if not token_val:
            src = token_source(normalized, prompt=prompt)
            try:
                token_val = src.get()["token"]
            except CredentialNotFound:
                token_val = None

        rejected = 0
        while True:
            headers = {"Authorization": f"Bearer {token_val}"} if token_val else {}
            try:
                resp = requests.get(
                    f"{normalized}/api/v1/me", headers=headers, timeout=5.0, verify=verify,
                )
            except requests.exceptions.SSLError as e:
                return self._fail(
                    f"TLS verification failed: {e} ('/set remote_ca_cert PATH' trusts a private CA)"
                )
            except Exception as e:
                return self._fail(str(e))
            if resp.status_code != 401:
                break
            rejected += 1
            if not (src and prompt):
                break
            if rejected >= MAX_TOKEN_REJECTIONS:
                return self._fail(
                    f"authentication failed: {rejected} tokens rejected; run /remote to try again"
                )
            try:
                token_val = src.failed()["token"]
            except CredentialNotFound:
                return self._fail("authentication failed: token prompt cancelled")
        try:
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            if resp.status_code == 401:
                return self._fail("authentication failed: the server rejected the token")
            return self._fail(str(e))

        if src:
            try:
                src.commit()
            except Exception:
                pass

        self.url = normalized
        self.token = token_val
        self._insecure = asked_insecure
        self._ca_cert = asked_ca_cert
        self.warning = (
            f"{normalized} is plain http: the token travels unencrypted"
            if normalized.startswith("http://") and not is_loopback(urlsplit(normalized).hostname)
            else None
        )
        self.user_id = data.get("id")
        self.is_admin = bool(data.get("is_admin"))
        self.projects = data.get("projects") or {}
        self.default_project = data.get("default_project")
        self.active_project = self._decided_project()
        self.connected = True
        self.last_error = None
        self.ping()
        self.notify_indicators()
        self.sync_matches()
        self.follow()
        return True

    def _decided_project(self) -> Optional[str]:
        """The project to build in without asking: the default, else a lone membership."""
        if self.default_project:
            return self.default_project
        if len(self.projects) == 1 and not self.is_admin:
            return next(iter(self.projects))
        return None

    def all_projects(self) -> dict[str, str]:
        """Projects this user may pick, with their role; an administrator sees them all."""
        found = dict(self.projects)
        if self.is_admin:
            try:
                resp = self.request("get", "/api/v1/projects")
                for project in resp.json() if resp.status_code == 200 else []:
                    found.setdefault(project["name"], "admin")
            except Exception:
                pass
        return found

    def use_project(self, name: str) -> None:
        self.active_project = name
        self.notify_indicators()
        self.sync_matches()
        self.follow()

    def set_default_project(self, name: Optional[str]) -> Optional[str]:
        """Save the default project on the server (None clears it); return an error or None."""
        try:
            resp = self.request("patch", "/api/v1/me", json={"default_project": name})
        except Exception as e:
            return str(e)
        if resp.status_code in (404, 405):
            return "this server cannot store a default project"
        if resp.status_code >= 400:
            return f"HTTP {resp.status_code}"
        self.default_project = name
        return None

    def disconnect(self) -> None:
        """Reset connection state and return to local engine mode."""
        self.url = None
        self.token = None
        self.connected = False
        self.user_id = None
        self.is_admin = False
        self.projects = {}
        self.default_project = None
        self.active_project = None
        self.ping_ms = None
        self.last_error = None
        self.warning = None
        self._insecure = None
        self._ca_cert = None
        self.notify_indicators()
        self.sync_matches()
        self.follow()

    def ping(self) -> Optional[float]:
        """Perform light latency probe; updates self.ping_ms."""
        if not self.url or not self.connected:
            self.ping_ms = None
            return None
        t0 = time.time()
        try:
            resp = self.request("get", "/api/v1/me", timeout=3.0)
            if resp.status_code == 200:
                self.ping_ms = round((time.time() - t0) * 1000.0, 1)
                return self.ping_ms
            if resp.status_code == 401:
                # Never prompt from a background probe: /remote reconnects.
                self._fail("authentication failed: token rejected; run /remote to reconnect")
        except Exception:
            pass
        self.ping_ms = None
        return None

    def _on_ui(self, fn) -> None:
        # Only route through call_from_thread if the app event loop is running.
        if getattr(self.app, "is_running", False) is True and hasattr(self.app, "call_from_thread"):
            try:
                self.app.call_from_thread(fn)
                return
            except RuntimeError:
                pass
        fn()

    def notify_indicators(self) -> None:
        if not self.app:
            return
        def _refresh():
            try:
                if hasattr(self.app, "refresh_indicators"):
                    self.app.refresh_indicators()
                elif hasattr(self.app, "screen"):
                    self.app.screen.query_one("#remote-indicator").refresh_text()
            except Exception:
                pass
        self._on_ui(_refresh)

    def follow(self) -> None:
        """Follow the active project's build events, or stop following."""
        if self._follower:
            self._follower.stop()
            self._follower = None
        if not (self.app and self.connected and self.active_project and self.token):
            return
        from seine.tui import remote_match
        self._follower = EventFollower(
            self.url, self.active_project, self.token,
            lambda event: self._on_ui(lambda: remote_match.on_event(self.app, event)),
            ca_cert=self.ca_cert)
        self._follower.start()

    def sync_matches(self) -> None:
        """Ask the server what it holds for the active specification."""
        if not self.app:
            return
        from seine.tui import remote_match
        self._on_ui(lambda: remote_match.refresh(self.app))
