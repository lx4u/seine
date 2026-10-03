# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import os
import shutil
import socket
import tempfile
import threading
from typing import Callable


class EventListener:
    """Receive the JSON lines a build's SocketReporter sends over a Unix socket.

    The socket lives in a short temporary directory: Unix socket paths
    are limited to about 100 bytes, which a job directory may exceed.
    """

    def __init__(self, on_event: Callable[[dict], None]):
        self._on_event = on_event
        self._dir = tempfile.mkdtemp(prefix="seine-ev-")
        self.path = os.path.join(self._dir, "reporter.sock")
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(self.path)
        self._server.listen(1)
        self._server.settimeout(0.2)
        self._closing = threading.Event()
        self._readers: list[threading.Thread] = []
        self._acceptor = threading.Thread(target=self._accept, daemon=True)
        self._acceptor.start()

    def _accept(self) -> None:
        # Keeps accepting until a timeout after close(), so no connection is lost.
        while True:
            try:
                conn, _ = self._server.accept()
            except socket.timeout:
                if self._closing.is_set():
                    return
                continue
            except OSError:
                return
            reader = threading.Thread(target=self._read, args=(conn,), daemon=True)
            self._readers.append(reader)
            reader.start()

    def _read(self, conn: socket.socket) -> None:
        with conn, conn.makefile("r", encoding="utf-8", errors="replace") as lines:
            for line in lines:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict) and isinstance(event.get("type"), str):
                    self._on_event(event)

    def close(self) -> None:
        """Stop listening, wait briefly for the readers and remove the socket."""
        self._closing.set()
        self._acceptor.join(timeout=5)
        self._server.close()
        for reader in self._readers:
            reader.join(timeout=5)
        shutil.rmtree(self._dir, ignore_errors=True)
