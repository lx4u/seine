# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0
"""Project event stream: tells project members when a build changes state."""

from __future__ import annotations

import asyncio
import threading
from typing import Any, Optional

from fastapi import WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool

from seine.distributed.server.auth import is_system_admin
from seine.distributed.server.db import Database
from seine.distributed.server.ws import (
    AUTH_TIMEOUT,
    CLOSE_FORBIDDEN,
    CLOSE_NOT_FOUND,
    CLOSE_UNAUTHENTICATED,
)

# A subscriber that falls this far behind loses events; it catches up on reconnect.
QUEUE_SIZE = 100


def build_event(build: dict[str, Any]) -> dict[str, Any]:
    """The lightweight message sent for a build that changed."""
    started = build.get("started_at")
    finished = build.get("finished_at")
    return {
        "type": "build_status",
        "build_id": build["id"],
        "project": build["project"],
        "spec_digest": build.get("spec_digest") or "",
        "target_arch": build["target_arch"],
        "status": build["status"],
        "created_at": build["created_at"],
        "started_at": started,
        "finished_at": finished,
        "duration": finished - started if started and finished else None,
        "artifacts_expired": bool(build.get("artifacts_expired_at")),
        "artifacts": [
            {"name": m["name"], "size": m["size"], "sha256": m["sha256"]}
            for m in build.get("artifact_meta") or []
        ],
    }


class ProjectEvents:
    """Fan-out of build events to the members following a project.

    publish() may be called from any thread; each subscriber is fed
    through its own bounded queue on the loop that serves it.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: dict[str, set[tuple[asyncio.AbstractEventLoop, asyncio.Queue]]] = {}

    def subscribe(self, project: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(QUEUE_SIZE)
        with self._lock:
            self._subscribers.setdefault(project, set()).add((asyncio.get_running_loop(), queue))
        return queue

    def unsubscribe(self, project: str, queue: asyncio.Queue) -> None:
        with self._lock:
            found = self._subscribers.get(project, set())
            found -= {sub for sub in found if sub[1] is queue}
            if not found:
                self._subscribers.pop(project, None)

    @staticmethod
    def _put(queue: asyncio.Queue, event: dict[str, Any]) -> None:
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            pass

    def publish(self, project: str, event: dict[str, Any]) -> None:
        with self._lock:
            targets = list(self._subscribers.get(project, ()))
        for loop, queue in targets:
            try:
                loop.call_soon_threadsafe(self._put, queue, event)
            except RuntimeError:
                pass


def events_role(db: Database, token: Any, project: str) -> int:
    """Return 0 for a member's token, else the close code."""
    if not isinstance(token, str) or not token:
        return CLOSE_UNAUTHENTICATED
    record = db.tokens.validate(token)
    user = db.users.get(record["user_id"]) if record else None
    if user is None or not user["active"]:
        return CLOSE_UNAUTHENTICATED
    if db.get_project(project) is None:
        return CLOSE_NOT_FOUND
    if is_system_admin(db, record) or db.projects.get_member(project, record["user_id"]):
        return 0
    return CLOSE_FORBIDDEN


async def serve_events(
    websocket: WebSocket, events: ProjectEvents, db: Database, project: str,
) -> None:
    """Authenticate with the first message, then push the project's build events.

    The "subscribed" message tells the client nothing is missed from now on.
    """
    await websocket.accept()
    try:
        first = await asyncio.wait_for(websocket.receive_json(), AUTH_TIMEOUT)
    except (asyncio.TimeoutError, ValueError):
        await websocket.close(code=CLOSE_UNAUTHENTICATED)
        return
    except WebSocketDisconnect:
        return

    token = first.get("auth") if isinstance(first, dict) else None
    code = await run_in_threadpool(events_role, db, token, project)
    if code:
        await websocket.close(code=code)
        return

    queue = events.subscribe(project)
    await websocket.send_json({"type": "subscribed", "project": project})
    # Subscribers only listen; a read that returns means they left or misbehaved.
    reader = asyncio.ensure_future(websocket.receive_text())
    try:
        while True:
            getter = asyncio.ensure_future(queue.get())
            await asyncio.wait({getter, reader}, return_when=asyncio.FIRST_COMPLETED)
            if reader.done():
                getter.cancel()
                reader.result()
                await websocket.close(code=CLOSE_FORBIDDEN)
                return
            await websocket.send_json(getter.result())
    except WebSocketDisconnect:
        pass
    finally:
        reader.cancel()
        events.unsubscribe(project, queue)
