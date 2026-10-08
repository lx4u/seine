# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

from textual.binding import Binding
from textual.containers import Vertical
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import RichLog, Static

from seine.progress import SPINNER

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
