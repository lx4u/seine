# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# Interface a build watcher (e.g. progress.Display) implements.
# output/sampled/plan are optional (no-op default); check support with
# getattr(reporter, "sampled", None) rather than isinstance().
import json
import os
import socket
import threading
import time
from typing import Protocol

class Reporter(Protocol):
    def started(self, name: str) -> None:
        ...

    def finished(self, name: str, failed: bool = False) -> None:
        ...

    def say(self, text: str) -> None:
        ...

    def output(self, name: str, line: str) -> None:
        pass

    def sampled(self, sample: dict) -> None:
        pass

    def plan(self, steps, cached=()) -> None:
        pass

# Name of the variable holding the Unix socket path a worker agent listens on.
SOCKET_ENV = "SEINE_REPORTER_SOCKET"

# Sends each call as one JSON line over a Unix socket, then passes it
# on to 'inner' (e.g. progress.Display). The lock keeps lines whole
# when parallel tasks report at once. A dead socket never fails a build.
class SocketReporter:
    def __init__(self, path, inner=None):
        self.inner = inner
        self._lock = threading.Lock()
        self._sock = None
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.connect(path)
            self._sock = sock
        except OSError:
            pass

    @classmethod
    def from_env(cls, inner=None):
        path = os.environ.get(SOCKET_ENV)
        return cls(path, inner) if path else inner

    def _emit(self, event):
        event["timestamp"] = time.time()
        line = (json.dumps(event) + "\n").encode()
        with self._lock:
            if self._sock is None:
                return
            try:
                self._sock.sendall(line)
            except OSError:
                self._sock = None

    def plan(self, steps, cached=()):
        tasks = [{"name": s.name, "needs": list(s.needs)} for s in steps]
        tasks += [{"name": name, "needs": [], "cached": True} for name in cached]
        self._emit({"type": "task_plan", "tasks": tasks})

    def started(self, name):
        self._emit({"type": "task_started", "task": name})
        if self.inner is not None:
            self.inner.started(name)

    def finished(self, name, failed=False):
        self._emit({"type": "task_finished", "task": name, "failed": failed})
        if self.inner is not None:
            self.inner.finished(name, failed=failed)

    def say(self, text):
        self._emit({"type": "say", "text": text})
        if self.inner is not None:
            self.inner.say(text)

    def sampled(self, sample):
        self._emit({"type": "sampled", "sample": sample})

    def close(self):
        if self._sock is not None:
            self._sock.close()
            self._sock = None
