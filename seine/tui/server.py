# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# The UNIX domain socket interaction server: accepts connections,
# dispatches "input"/"ai_input" JSON commands to the running app,
# and broadcasts UI events to connected clients.
#
# Also hosts the development HTTP server (PayloadServer) used by
# '/serve' to serve update payloads to attached hardware and VMs.

import asyncio
import functools
import http.server
import json
import os
import socket
import threading

from textual.css.query import NoMatches

from seine.tui import ai
from seine.tui.base import Prompt


class InteractionServer:
    def __init__(self, app, path: str | None = None):
        self.app = app
        self.path = path
        self.server: socket.socket | None = None
        self.clients: list[socket.socket] = []
        self._lock = threading.Lock()
        if self.path:
            self.start()

    @property
    def _socket_server(self):
        return self.server

    @property
    def _socket_clients(self):
        return self.clients

    def start(self) -> None:
        if not self.path:
            return
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(self.path)
        server.listen()
        self.server = server
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def _accept_loop(self) -> None:
        server = self.server
        if server is None:
            return
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                break
            with self._lock:
                self.clients.append(conn)
            threading.Thread(target=self._client_handler, args=(conn,), daemon=True).start()

    def _client_handler(self, conn: socket.socket) -> None:
        with conn:
            buffer = b""
            while True:
                try:
                    data = conn.recv(4096)
                except OSError:
                    break
                if not data:
                    break
                buffer += data
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if not line:
                        continue
                    try:
                        message = json.loads(line.decode())
                    except json.JSONDecodeError:
                        continue
                    self._handle_message(message)
        with self._lock:
            if conn in self.clients:
                self.clients.remove(conn)

    def _handle_message(self, msg: dict) -> None:
        t = msg.get("type")
        if t == "input":
            text = msg.get("text", "")
            if not isinstance(text, str):
                return

            async def type_and_submit():
                try:
                    prompt = self.app.screen.query_one(Prompt)
                except NoMatches:
                    return
                prompt.value = ""
                prompt.cursor_position = 0
                for ch in text:
                    prompt.value += ch
                    prompt.cursor_position = len(prompt.value)
                    await asyncio.sleep(0.02)
                await prompt.action_submit()

            self.app.call_from_thread(type_and_submit)
        elif t == "ai_input":
            p = msg.get("prompt")
            if isinstance(p, str):
                self.app.call_from_thread(ai.ask, self.app, p)

    def send(self, data: dict) -> None:
        raw = (json.dumps(data) + "\n").encode()
        with self._lock:
            dead = []
            for client in self.clients:
                try:
                    client.sendall(raw)
                except OSError:
                    dead.append(client)
            for client in dead:
                try:
                    client.close()
                finally:
                    self.clients.remove(client)

    def send_ai_messages(self) -> None:
        ai_state = getattr(self.app, "ai_state", None)
        if not ai_state:
            return
        msgs = ai_state._new_assistant_messages()
        for msg in msgs:
            self.send({"type": "ai_message", "content": msg.get("content", "")})
        ai_state._mark_sent()

    def close(self) -> None:
        if self.server:
            try:
                self.server.close()
            except OSError:
                pass
            self.server = None
        with self._lock:
            for client in self.clients:
                try:
                    client.close()
                except OSError:
                    pass
            self.clients.clear()
        if self.path:
            try:
                os.unlink(self.path)
            except OSError:
                pass


class _PayloadHTTPHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        # Suppress stderr noise in TUI
        pass

    def translate_path(self, path):
        translated = super().translate_path(path)
        if os.path.exists(translated):
            return translated
        if os.path.isdir(self.directory):
            rel = os.path.relpath(translated, self.directory)
            for entry in sorted(os.listdir(self.directory)):
                if entry.endswith("-payload"):
                    candidate = os.path.join(self.directory, entry, rel)
                    if os.path.exists(candidate):
                        return candidate
        return translated


class _ReusableThreadingHTTPServer(http.server.ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


class PayloadServer:
    def __init__(self, directory: str, port: int = 8000, host: str = "0.0.0.0"):
        self.directory = os.path.abspath(directory)
        self.host = host
        self.requested_port = port
        self.port = port
        self.httpd: _ReusableThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self.httpd is not None

    def start(self) -> None:
        if self.running:
            return
        os.makedirs(self.directory, exist_ok=True)
        handler = functools.partial(_PayloadHTTPHandler, directory=self.directory)
        self.httpd = _ReusableThreadingHTTPServer((self.host, self.requested_port), handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None
        if self.thread:
            self.thread.join(timeout=1.0)
            self.thread = None
