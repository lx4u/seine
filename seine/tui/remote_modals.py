# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import re
from typing import Any, Optional

from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static


class AdminModalBase(ModalScreen):
    DEFAULT_CSS = """
    AdminModalBase { align: center middle; }
    .modal-box {
        width: 60; height: auto;
        border: round $border;
        background: $surface;
        padding: 1 2;
    }
    .modal-title { text-style: bold; padding-bottom: 1; }
    .modal-label { color: $text-muted; padding-top: 1; }
    .modal-error { color: red; height: auto; padding-top: 1; }
    .modal-box Input { border: none; height: 1; padding: 0 1; }
    .btn-row { height: 1; margin-top: 1; align-horizontal: right; }
    .btn-row > Button { min-width: 8; margin-left: 1; height: 1; border-top: none; border-bottom: none; }
    .toggle-btn { width: 100%; height: 1; margin-top: 1; border-top: none; border-bottom: none; }
    """

    BINDINGS = [Binding("escape", "cancel", show=False)]

    def action_cancel(self):
        self.dismiss(None)


class UserCreateModal(AdminModalBase):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.is_admin = False

    def compose(self):
        with Vertical(classes="modal-box"):
            yield Static("Create User Account", classes="modal-title")
            yield Static("Username:", classes="modal-label")
            yield Input(placeholder="e.g. alice", id="username")
            yield Button("Role: Standard User", id="toggle-role", classes="toggle-btn")
            yield Static("", id="error-msg", classes="modal-error")
            with Horizontal(classes="btn-row"):
                yield Button("Cancel", id="cancel")
                yield Button("Create", variant="primary", id="submit")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
        elif event.button.id == "toggle-role":
            self.is_admin = not self.is_admin
            lbl = "Role: System Admin" if self.is_admin else "Role: Standard User"
            event.button.label = lbl
        elif event.button.id == "submit":
            val = self.query_one("#username", Input).value.strip()
            if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,63}$", val):
                self.query_one("#error-msg", Static).update("invalid username format")
                return
            self.dismiss({"username": val, "is_admin": self.is_admin})


class TokenIssueModal(AdminModalBase):
    def __init__(self, user_id: str, **kwargs):
        super().__init__(**kwargs)
        self.user_id = user_id

    def compose(self):
        with Vertical(classes="modal-box"):
            yield Static(f"Issue Personal Access Token for '{self.user_id}'", classes="modal-title")
            yield Static("Expiration in days (leave empty for never):", classes="modal-label")
            yield Input(placeholder="e.g. 30", id="days")
            yield Static("", id="error-msg", classes="modal-error")
            with Horizontal(classes="btn-row"):
                yield Button("Cancel", id="cancel")
                yield Button("Issue", variant="primary", id="submit")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
        elif event.button.id == "submit":
            val = self.query_one("#days", Input).value.strip()
            days: Optional[int] = None
            if val:
                try:
                    days = int(val)
                    if days <= 0:
                        raise ValueError()
                except ValueError:
                    self.query_one("#error-msg", Static).update("days must be a positive integer")
                    return
            self.dismiss({"days": days})


class TokenDisplayModal(AdminModalBase):
    def __init__(self, token: str, **kwargs):
        super().__init__(**kwargs)
        self.token = token

    def compose(self):
        with Vertical(classes="modal-box"):
            yield Static("Personal Access Token Issued", classes="modal-title")
            yield Static("Copy this token now. It will not be shown again:", classes="modal-label")
            yield Input(value=self.token, id="token-val", select_on_focus=True)
            with Horizontal(classes="btn-row"):
                yield Button("Done", variant="primary", id="submit")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(None)


class ProjectCreateModal(AdminModalBase):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.provision_buckets = False

    def compose(self):
        with Vertical(classes="modal-box"):
            yield Static("Create Project", classes="modal-title")
            yield Static("Project Name:", classes="modal-label")
            yield Input(placeholder="e.g. embedded-os", id="name")
            yield Static("Dev Bucket (optional):", classes="modal-label")
            yield Input(placeholder="defaults to seine-<name>-dev", id="dev-bucket")
            yield Static("Prod Bucket (optional):", classes="modal-label")
            yield Input(placeholder="defaults to seine-<name>-prod", id="prod-bucket")
            yield Button("Provision S3 Buckets: Off", id="toggle-provision", classes="toggle-btn")
            yield Static("", id="error-msg", classes="modal-error")
            with Horizontal(classes="btn-row"):
                yield Button("Cancel", id="cancel")
                yield Button("Create", variant="primary", id="submit")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
        elif event.button.id == "toggle-provision":
            self.provision_buckets = not self.provision_buckets
            lbl = "Provision S3 Buckets: On" if self.provision_buckets else "Provision S3 Buckets: Off"
            event.button.label = lbl
        elif event.button.id == "submit":
            name = self.query_one("#name", Input).value.strip()
            if not re.match(r"^[a-z0-9][a-z0-9._-]*$", name):
                self.query_one("#error-msg", Static).update("invalid project name")
                return
            dev = self.query_one("#dev-bucket", Input).value.strip() or None
            prod = self.query_one("#prod-bucket", Input).value.strip() or None
            self.dismiss({
                "name": name,
                "dev_bucket": dev,
                "prod_bucket": prod,
                "provision_buckets": self.provision_buckets,
            })


class MemberAssignModal(AdminModalBase):
    ROLES = ("developer", "releaser", "admin")

    def __init__(self, project_id: str, **kwargs):
        super().__init__(**kwargs)
        self.project_id = project_id
        self.role_idx = 0

    def compose(self):
        with Vertical(classes="modal-box"):
            yield Static(f"Assign Member to '{self.project_id}'", classes="modal-title")
            yield Static("Username:", classes="modal-label")
            yield Input(placeholder="e.g. bob", id="username")
            yield Button(f"Role: {self.ROLES[self.role_idx]}", id="toggle-role", classes="toggle-btn")
            yield Static("", id="error-msg", classes="modal-error")
            with Horizontal(classes="btn-row"):
                yield Button("Cancel", id="cancel")
                yield Button("Assign", variant="primary", id="submit")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
        elif event.button.id == "toggle-role":
            self.role_idx = (self.role_idx + 1) % len(self.ROLES)
            event.button.label = f"Role: {self.ROLES[self.role_idx]}"
        elif event.button.id == "submit":
            u = self.query_one("#username", Input).value.strip()
            if not u:
                self.query_one("#error-msg", Static).update("username cannot be empty")
                return
            self.dismiss({"user_id": u, "role": self.ROLES[self.role_idx]})
