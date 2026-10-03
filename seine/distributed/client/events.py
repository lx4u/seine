# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0
"""Follow a project's build events, reconnecting when the connection drops."""

from __future__ import annotations

import json
import threading
from typing import Any, Callable, Optional

from websockets.exceptions import WebSocketException

from seine.distributed.common.transport import ws_ssl_context
from seine.distributed.common.wsclient import WsClient, WsClosed

# The server closes with these when retrying cannot help, see server/ws.py.
GIVE_UP = (4401, 4403, 4404)
FIRST_DELAY = 1.0
MAX_DELAY = 30.0


class EventFollower(threading.Thread):
    """Call on_event(message) for each message the server pushes for a project.

    Every (re)connection starts with a "subscribed" message: anything
    that happened while disconnected has to be asked for again.
    """

    def __init__(
        self,
        server_url: str,
        project: str,
        token: str,
        on_event: Callable[[dict[str, Any]], None],
        ca_cert: Optional[str] = None,
    ):
        super().__init__(daemon=True, name="seine-events")
        self.url = f"{server_url.replace('http', 'ws', 1)}/api/v1/projects/{project}/events"
        self.token = token
        self.on_event = on_event
        self.ssl_context = ws_ssl_context(self.url, ca_cert)
        self._stop_event = threading.Event()
        self._subscribed = False

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        delay = FIRST_DELAY
        while not self._stop_event.is_set():
            self._subscribed = False
            try:
                self._follow()
            except WsClosed as e:
                if e.code in GIVE_UP:
                    return
            except (OSError, WebSocketException):
                pass
            if self._subscribed:
                delay = FIRST_DELAY
            self._stop_event.wait(delay)
            delay = min(delay * 2, MAX_DELAY)

    def _follow(self) -> None:
        """Read one connection until it ends."""
        ws = WsClient()
        try:
            ws.connect(self.url, self.ssl_context)
            ws.send(json.dumps({"auth": self.token}))
            while not self._stop_event.is_set():
                try:
                    raw = ws.recv(timeout=0.5)
                except TimeoutError:
                    continue
                try:
                    message = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(message, dict):
                    self._subscribed = self._subscribed or message.get("type") == "subscribed"
                    self.on_event(message)
        finally:
            ws.close()
