# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# Bridges credentials.CredentialSource's 'prompt' callable (called from
# the build worker thread) to a modal on the UI thread: the worker
# blocks on a threading.Event until the modal submits or cancels.

import threading

from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static

# Every event a prompt() call is currently waiting on, so app shutdown
# can release them instead of leaving the build worker blocked forever.
_PENDING = set()

def release_pending():
    for event in list(_PENDING):
        event.set()

class CredentialModal(ModalScreen):
    DEFAULT_CSS = """
    CredentialModal { align: center middle; }
    #credpane {
        width: 60; height: auto;
        border: round $border;
        background: $surface;
        padding: 1 2;
    }
    #credtitle { text-style: bold; padding-bottom: 1; }
    /* Flattened: this modal's own border already frames it -- an inner
       border on every field/button just adds height for no reason. */
    #credpane Input, #credpane Input:focus,
    #credpane Input.-invalid, #credpane Input.-invalid:focus {
        border: none; height: 1; padding: 0 1;
    }
    #credpane Button, #credpane Button:hover,
    #credpane Button:focus, #credpane Button.-active {
        border-top: none; border-bottom: none; height: 1;
    }
    #passwordrow, .passwordrow { height: 1; }
    #passwordrow > Input, .passwordrow > Input { width: 1fr; }
    #passwordrow > #reveal, .passwordrow > .revealbtn { width: 5; min-width: 5; }
    #actionrow { height: 1; padding-top: 1; align-horizontal: right; }
    #actionrow > Button { min-width: 8; margin-left: 1; }
    #credhint { color: $text-muted; padding-top: 1; }
    """

    # Current-state icons for #reveal: closed while masked, open once
    # revealed -- rather than 'show'/'hide' text, which crowded the row
    # this button shares with the password Input.
    HIDDEN_ICON   = "\U0001F512"  # closed padlock
    REVEALED_ICON = "\U0001F513"  # open padlock

    BINDINGS = [Binding("escape", "cancel", show=False)]

    def __init__(self, context, fields, event, result):
        super().__init__()
        # Not 'self._context': MessagePump already owns that name for its
        # own contextvars-restoring method, and shadowing it breaks every
        # 'with self._context():' in Textual's own message loop.
        self._feed_context = context
        self._fields = fields
        self._event = event
        self._result = result
        self._revealed = {}

    def compose(self):
        with Vertical(id="credpane"):
            yield Static(self._feed_context or "Credentials needed",
                         id="credtitle", markup=False)
            for field, (default_val, is_secret) in self._fields.items():
                label = "Access Key" if field == "access_key" else (
                    "Secret Key" if field == "secret_key" else field.replace("_", " ").title()
                )
                yield Static(label)
                if is_secret:
                    row_id = "passwordrow" if field == "password" else f"{field}_row"
                    btn_id = "reveal" if field == "password" else f"reveal_{field}"
                    with Horizontal(id=row_id, classes="passwordrow"):
                        yield Input(value=default_val, password=True, id=field)
                        yield Button(self.HIDDEN_ICON, id=btn_id, classes="revealbtn")
                else:
                    yield Input(value=default_val, id=field)
            with Horizontal(id="actionrow"):
                yield Button("Cancel", id="cancel")
                yield Button("OK", id="ok", variant="primary")
            yield Static("Enter submit · Esc cancel", id="credhint")

    def on_mount(self):
        first_input = self.query(Input).first()
        if first_input:
            first_input.focus()

    def on_button_pressed(self, event):
        btn_id = event.button.id or ""
        if btn_id == "reveal" or btn_id.startswith("reveal_"):
            field = "password" if btn_id == "reveal" else btn_id[len("reveal_"):]
            self._revealed[field] = not self._revealed.get(field, False)
            inp = self.query_one(f"#{field}", Input)
            inp.password = not self._revealed[field]
            event.button.label = (self.REVEALED_ICON if self._revealed[field]
                                  else self.HIDDEN_ICON)
        elif btn_id == "ok":
            self._submit()
        elif btn_id == "cancel":
            self.action_cancel()

    def on_input_submitted(self, event):
        self._submit()

    def action_cancel(self):
        self._result["cancelled"] = True
        self._release()

    def _submit(self):
        self._result["values"] = {
            field: self.query_one(f"#{field}", Input).value
            for field in self._fields
        }
        self._release()

    def _release(self):
        self._event.set()
        _PENDING.discard(self._event)
        try:
            self.app.pop_screen()
        except NoMatches:
            pass

# One prompt at a time per app: a second CredentialSource asking while
# the first modal is still open waits its turn rather than stacking modals.
_lock = threading.Lock()

def tui_prompt(app):
    def prompt(context, fields):
        with _lock:
            event = threading.Event()
            result = {}
            _PENDING.add(event)
            modal = CredentialModal(context, fields, event, result)
            app.call_from_thread(app.push_screen, modal)
            event.wait()
            if "values" not in result:
                from seine.credentials import CredentialNotFound
                raise CredentialNotFound(
                    "the credential prompt was cancelled or the app closed")
            return result["values"]
    return prompt
