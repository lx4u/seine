# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import os
import sys
import threading

from textual.binding import Binding
from textual.containers import Vertical
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import RichLog, Static

from seine import tasks
from seine.progress import SPINNER
from seine.tui.sanitize import sanitize

FRAMES = SPINNER[True]

class StartupModal(ModalScreen):
    """Modal log overlay displayed while specifications load in the background."""

    DEFAULT_CSS = """
    StartupModal { align: center middle; }
    #startuppane {
        width: 70%; height: 70%;
        border: round $border;
        background: $surface;
        padding: 1 2;
    }
    #startuptitle { color: blue; text-style: bold; height: 1; }
    #startuplog { height: 1fr; border: none; background: $surface; }
    #startupstatus { height: auto; color: $text-muted; }
    """

    BINDINGS = [
        Binding("escape", "dismiss", show=False),
    ]

    def compose(self):
        with Vertical(id="startuppane"):
            yield Static(f"{FRAMES[0]} Loading specification...", id="startuptitle")
            yield RichLog(id="startuplog", markup=False, wrap=True, max_lines=4000)
            yield Static("", id="startupstatus")

    def on_mount(self):
        self._spinner_frame = 0
        self.set_interval(0.1, self._tick_spinner)

    def _tick_spinner(self):
        self._spinner_frame += 1
        frame = FRAMES[self._spinner_frame % len(FRAMES)]
        try:
            self.query_one("#startuptitle", Static).update(f"{frame} Loading specification...")
        except NoMatches:
            pass

    def action_dismiss(self):
        pass

    def append_log(self, text: str):
        self.query_one("#startuplog", RichLog).write(text)

    def append_lines(self, lines: list[str]):
        log = self.query_one("#startuplog", RichLog)
        for line in lines:
            log.write(line)

    def set_status(self, text: str):
        self.query_one("#startupstatus", Static).update(text)


class PipeStream:
    """Pipe-backed stream capturing subprocess and thread output for modal logging."""

    def __init__(self, on_lines):
        self._on_lines = on_lines
        self._r, self._w = os.pipe()
        self._pending = ""
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def fileno(self):
        return self._w

    @property
    def name(self):
        return "<startup-log>"

    def write(self, text):
        if isinstance(text, str):
            data = text.encode("utf-8", "replace")
        else:
            data = bytes(text)
        os.write(self._w, data)
        return len(text)

    def flush(self):
        pass

    def close(self):
        try:
            os.close(self._w)
        except OSError:
            pass
        self._thread.join()

    def _pump(self):
        while True:
            try:
                chunk = os.read(self._r, 65536)
            except OSError:
                break
            if not chunk:
                break
            text = self._pending + chunk.decode("utf-8", "replace")
            lines = text.split("\n")
            self._pending = lines.pop()
            if lines:
                sanitized = [sanitize(line) for line in lines]
                self._on_lines(sanitized)
        if self._pending:
            sanitized = [sanitize(self._pending)]
            self._on_lines(sanitized)
        try:
            os.close(self._r)
        except OSError:
            pass


class ThreadOutputProxy:
    """Thread-aware output proxy routing only worker writes to the pipe stream."""

    def __init__(self, target_stream, real_stream, worker_ident):
        self._target = target_stream
        self._real = real_stream
        self._worker_ident = worker_ident

    def write(self, s):
        if threading.get_ident() == self._worker_ident:
            return self._target.write(s)
        return self._real.write(s)

    def flush(self):
        if threading.get_ident() == self._worker_ident:
            return self._target.flush()
        return self._real.flush()

    def fileno(self):
        if threading.get_ident() == self._worker_ident:
            return self._target.fileno()
        return self._real.fileno()

    def isatty(self):
        return getattr(self._real, "isatty", lambda: False)()

    def __getattr__(self, name):
        return getattr(self._real, name)


def load_spec_deferred(app, modal, files):
    err = None
    pipe = PipeStream(on_lines=lambda lines: _safe_call(app, modal.append_lines, lines))
    worker_ident = threading.get_ident()
    old_stdout = sys.stdout
    old_stderr = sys.stderr
    sys.stdout = ThreadOutputProxy(pipe, old_stdout, worker_ident)
    sys.stderr = ThreadOutputProxy(pipe, old_stderr, worker_ident)
    try:
        with tasks.capture(pipe):
            app.context.use(files)
    except (OSError, ValueError) as e:
        err = str(e)
    finally:
        sys.stdout = old_stdout
        sys.stderr = old_stderr
        pipe.close()

    _safe_call(app, app._spec_load_finished, err)


def _safe_call(app, fn, *args):
    try:
        app.call_from_thread(fn, *args)
    except Exception:
        pass
