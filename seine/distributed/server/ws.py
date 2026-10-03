# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections import OrderedDict, deque
from typing import Any, Optional

from fastapi import WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool

from seine.distributed.server.auth import is_system_admin
from seine.distributed.server.db import Database

AUTH_TIMEOUT = 5.0
MAX_TEXT_BYTES = 64 * 1024
MAX_HISTORY_CHUNKS = 10000
MAX_BUILDS = 200
MAX_PLAN_TASKS = 5000

CLOSE_BAD_DATA = 1007
CLOSE_TOO_BIG = 1009
CLOSE_UNAUTHENTICATED = 4401
CLOSE_FORBIDDEN = 4403
CLOSE_NOT_FOUND = 4404

_TERMINAL_STATES = ("completed", "failed", "cancelled")


class BroadcastHub:
    """Fan-out hub: relays log chunks to all WebSocket listeners for a build.

    Late joiners receive buffered history so they see the full log from
    the start regardless of when they connect. History is bounded per
    build and across builds (least recently used builds are dropped).
    """

    def __init__(self, max_chunks: int = MAX_HISTORY_CHUNKS, max_builds: int = MAX_BUILDS):
        self._max_chunks = max_chunks
        self._max_builds = max_builds
        self._lock = threading.Lock()
        self._listeners: dict[str, set[WebSocket]] = {}
        self._history: OrderedDict[str, deque[dict[str, Any]]] = OrderedDict()
        # Kept apart from the history so a chatty log never pushes it out.
        self._plans: dict[str, dict[str, Any]] = {}

    def _touch(self, build_id: str) -> deque[dict[str, Any]]:
        """Return the build's history, marking it most recently used (lock held)."""
        history = self._history.get(build_id)
        if history is None:
            history = self._history[build_id] = deque(maxlen=self._max_chunks)
            while len(self._history) > self._max_builds:
                evicted, _ = self._history.popitem(last=False)
                self._plans.pop(evicted, None)
        else:
            self._history.move_to_end(build_id)
        return history

    async def connect(self, build_id: str, websocket: WebSocket) -> None:
        """Register an accepted, authenticated viewer and replay the history."""
        with self._lock:
            self._listeners.setdefault(build_id, set()).add(websocket)
            replay = list(self._touch(build_id))
            plan = self._plans.get(build_id)
            if plan is not None:
                replay.insert(0, plan)

        for chunk in replay:
            try:
                await websocket.send_json(chunk)
            except Exception:
                pass

    def disconnect(self, build_id: str, websocket: WebSocket) -> None:
        with self._lock:
            listeners = self._listeners.get(build_id)
            if listeners is not None:
                listeners.discard(websocket)
                if not listeners:
                    del self._listeners[build_id]

    def has_listeners(self, build_id: str) -> bool:
        with self._lock:
            return build_id in self._listeners

    def forget(self, build_id: str) -> None:
        """Drop the history and listeners of a build."""
        with self._lock:
            self._history.pop(build_id, None)
            self._plans.pop(build_id, None)
            self._listeners.pop(build_id, None)

    async def broadcast(self, build_id: str, message: dict[str, Any]) -> None:
        with self._lock:
            history = self._touch(build_id)
            if message.get("type") == "task_plan":
                self._plans[build_id] = message
            else:
                history.append(message)
            listeners = list(self._listeners.get(build_id, ()))

        for ws in listeners:
            try:
                await ws.send_json(message)
            except Exception:
                self.disconnect(build_id, ws)


def forget_finished_build(db: Database, hub: BroadcastHub, build_id: str) -> None:
    """Free the log history of a finished build once nobody listens to it."""
    build = db.get_build(build_id)
    if build and build["status"] in _TERMINAL_STATES and not hub.has_listeners(build_id):
        hub.forget(build_id)


def stream_role(db: Database, token: Any, build_id: str) -> tuple[Optional[str], int]:
    """Return ("viewer"|"sender", 0) for an allowed token, else (None, close code)."""
    if not isinstance(token, str) or not token:
        return None, CLOSE_UNAUTHENTICATED

    build = db.get_build(build_id)
    record = db.tokens.validate(token)
    if record:
        user = db.users.get(record["user_id"])
        if user is None or not user["active"]:
            return None, CLOSE_UNAUTHENTICATED
        if build is None:
            return None, CLOSE_NOT_FOUND
        if is_system_admin(db, record) or db.projects.get_member(build["project"], record["user_id"]):
            return "viewer", 0
        return None, CLOSE_FORBIDDEN

    worker = db.get_worker_by_token(token)
    if worker:
        if build is None:
            return None, CLOSE_NOT_FOUND
        if db.scheduler.worker_holds_build(worker["id"], build_id):
            return "sender", 0
        return None, CLOSE_FORBIDDEN

    return None, CLOSE_UNAUTHENTICATED


def _is_name(value: Any) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 512


def parse_event(data: dict[str, Any], build_id: str) -> Optional[dict[str, Any]]:
    """Return a clean copy of a structured build event, or None when it is malformed."""
    kind = data.get("type")
    event: dict[str, Any] = {"build_id": build_id, "type": kind, "timestamp": time.time()}
    if kind == "task_plan":
        tasks = data.get("tasks")
        if not isinstance(tasks, list) or len(tasks) > MAX_PLAN_TASKS:
            return None
        clean = []
        for task in tasks:
            if not isinstance(task, dict) or not _is_name(task.get("name")):
                return None
            needs = task.get("needs", [])
            if not isinstance(needs, list) or not all(_is_name(n) for n in needs):
                return None
            entry: dict[str, Any] = {"name": task["name"], "needs": needs}
            if task.get("cached") is True:
                entry["cached"] = True
            clean.append(entry)
        event["tasks"] = clean
    elif kind in ("task_started", "task_finished"):
        if not _is_name(data.get("task")):
            return None
        event["task"] = data["task"]
        if kind == "task_finished":
            event["failed"] = data.get("failed") is True
    elif kind == "sampled":
        sample = data.get("sample")
        if not isinstance(sample, dict):
            return None
        event["sample"] = {k: v for k, v in sample.items()
                           if isinstance(k, str) and isinstance(v, (int, float))}
    elif kind == "say":
        if not isinstance(data.get("text"), str) or len(data["text"].encode()) > MAX_TEXT_BYTES:
            return None
        event["text"] = data["text"]
    else:
        return None
    return event


def parse_chunk(raw: str, build_id: str) -> tuple[Optional[dict[str, Any]], int]:
    """Validate a sender message and return (chunk or event, 0) or (None, close code)."""
    try:
        data = json.loads(raw)
    except ValueError:
        return None, CLOSE_BAD_DATA
    if isinstance(data, dict) and "type" in data:
        event = parse_event(data, build_id)
        return (event, 0) if event else (None, CLOSE_BAD_DATA)
    if not isinstance(data, dict) or not isinstance(data.get("text"), str):
        return None, CLOSE_BAD_DATA
    source = data.get("source", "stdout")
    if not isinstance(source, str):
        return None, CLOSE_BAD_DATA
    if len(data["text"].encode()) > MAX_TEXT_BYTES:
        return None, CLOSE_TOO_BIG
    chunk = {
        "build_id": build_id,
        "source": source,
        "text": data["text"],
        "timestamp": time.time(),
    }
    return chunk, 0


async def serve_stream(websocket: WebSocket, hub: BroadcastHub, db: Database, build_id: str) -> None:
    """Authenticate a stream client with its first message, then relay or view."""
    await websocket.accept()
    try:
        first = await asyncio.wait_for(websocket.receive_json(), AUTH_TIMEOUT)
    except (asyncio.TimeoutError, ValueError):
        await websocket.close(code=CLOSE_UNAUTHENTICATED)
        return
    except WebSocketDisconnect:
        return

    token = first.get("auth") if isinstance(first, dict) else None
    role, code = await run_in_threadpool(stream_role, db, token, build_id)
    if role is None:
        await websocket.close(code=code)
        return

    try:
        if role == "viewer":
            await hub.connect(build_id, websocket)
            await websocket.receive_text()
            # Viewers only receive; any message from one is a violation.
            await websocket.close(code=CLOSE_FORBIDDEN)
        else:
            while True:
                chunk, code = parse_chunk(await websocket.receive_text(), build_id)
                if chunk is None:
                    await websocket.close(code=code)
                    return
                await hub.broadcast(build_id, chunk)
    except WebSocketDisconnect:
        pass
    finally:
        hub.disconnect(build_id, websocket)
        await run_in_threadpool(forget_finished_build, db, hub, build_id)
