# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0
"""A blocking WebSocket client backed by the websockets asyncio implementation."""

from __future__ import annotations

import asyncio
import ssl
import threading
from typing import Any, Optional

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

OPEN_TIMEOUT = 10.0
SEND_TIMEOUT = 30.0
CLOSE_WAIT = 15.0


class WsClosed(Exception):
    """The peer (or the connection) closed the WebSocket."""

    def __init__(self, code: int, reason: str = ""):
        super().__init__(f"connection closed ({code}) {reason}".rstrip())
        self.code = code
        self.reason = reason


class WsClient:
    """Run one WebSocket connection on a private event loop thread.

    The sync websockets client stalls TLS 1.3 handshakes, so the asyncio one is used.
    """

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._serve, name="seine-ws", daemon=True)
        self._thread.start()
        self._ws: Any = None
        self._done = False

    def _serve(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_forever()
        finally:
            tasks = asyncio.all_tasks(self._loop)
            for task in tasks:
                task.cancel()
            self._loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
            self._loop.close()

    def _call(self, coro, timeout: Optional[float] = None):
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout)
        except TimeoutError:
            future.cancel()
            raise

    async def _open(self, url: str, ssl_context: Optional[ssl.SSLContext], open_timeout: float) -> None:
        kwargs = {"ssl": ssl_context} if ssl_context else {}
        self._ws = await connect(url, open_timeout=open_timeout, **kwargs)

    def connect(self, url: str, ssl_context: Optional[ssl.SSLContext] = None, open_timeout: float = OPEN_TIMEOUT) -> None:
        """Open the connection; the client is closed again if that fails."""
        try:
            self._call(self._open(url, ssl_context, open_timeout))
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _as_closed(exc: ConnectionClosed) -> WsClosed:
        frame = exc.rcvd or exc.sent
        return WsClosed(frame.code if frame else 1006, frame.reason if frame else "")

    def send(self, text: str, timeout: float = SEND_TIMEOUT) -> None:
        try:
            self._call(self._ws.send(text), timeout)
        except ConnectionClosed as exc:
            raise self._as_closed(exc) from None

    def recv(self, timeout: Optional[float] = None) -> str:
        """Return the next message; raise TimeoutError when none arrives in time."""
        try:
            return self._call(asyncio.wait_for(self._ws.recv(), timeout))
        except ConnectionClosed as exc:
            raise self._as_closed(exc) from None

    async def _shutdown(self) -> None:
        # Unread messages would hold back the close frame, so keep reading while closing.
        async def drain() -> None:
            async for _ in self._ws:
                pass

        task = asyncio.ensure_future(drain())
        try:
            await self._ws.close()
        finally:
            task.cancel()

    def close(self) -> None:
        """Close the connection and stop the loop thread; safe to call twice."""
        if self._done:
            return
        self._done = True
        if self._ws is not None:
            try:
                self._call(self._shutdown(), CLOSE_WAIT)
            except Exception:
                pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(CLOSE_WAIT)
