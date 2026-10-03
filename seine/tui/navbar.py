# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# Icon-only screen switcher on the left edge of every screen. '/diff'
# is left out: it needs two file arguments a click cannot give.

import os
from collections import namedtuple

from textual.containers import Vertical
from textual.widgets import Static

# 'available' is (app) -> reason why not, or None when the screen is usable.
NavItem = namedtuple("NavItem", "name icon available")

NO_SPEC = "no active specification -- '/use SPEC' first"
NO_IMAGE = "no active specification with an 'image:' section"

def _spec(app):
    return None if app.context.active else NO_SPEC

def _image(app):
    if not app.context.active:
        return NO_SPEC
    if any("image" not in build.spec for build in app.context.builds):
        return NO_IMAGE
    return None

def _section(key, state):
    def check(app):
        if getattr(app, state).running:
            return None
        if not app.context.active:
            return NO_SPEC
        if not any(key in build.spec for build in app.context.builds):
            return "no '%s:' section in the active specification" % key
        return None
    return check

def _always(app):
    return None

def _chat(app):
    from seine.tui.ai import configured
    return None if configured() else "AI assistant not configured -- set 'llm_model' in /settings"

def _target(app):
    if getattr(app, "_target_client", None) is not None or os.environ.get("MTDA_REMOTE"):
        return None
    return "target not connected -- run '/target connect'"

def _remote(app):
    return None if app.remote_session.connected else "remote not connected -- run '/remote'"

# Single-width glyphs only: the bar is one cell wide.
NAV_ITEMS = [
    NavItem("overview", "⌂", _always),
    NavItem("plan", "≡", _image),
    NavItem("build", "⚙", _image),
    NavItem("artifacts", "▣", _spec),
    NavItem("filesystem", "▤", _spec),
    NavItem("packages", "▦", _spec),
    NavItem("analyze", "◔", _spec),
    NavItem("cache", "▥", _always),
    NavItem("doctor", "✚", _always),
    NavItem("issues", "⚑", _spec),
    NavItem("chat", "✉", _chat),
    NavItem("target", "⌖", _target),
    NavItem("test", "✓", _section("test", "test_state")),
    NavItem("vendor", "⬡", _section("vendor", "vendor_state")),
    NavItem("remote", "◎", _remote),
]

class NavIcon(Static):
    can_focus = False

    def __init__(self, item, **kwargs):
        super().__init__(item.icon, markup=False, **kwargs)
        self.item = item
        self.tooltip = "%s (/%s)" % (item.name, item.name)

    def on_click(self, event):
        reason = self.item.available(self.app)
        if reason is None:
            self.app.show(self.item.name)
        else:
            self.app.screen.say("%s: %s" % (self.item.name, reason), warning=True)

class NavBar(Vertical):
    can_focus = False

    def compose(self):
        for item in NAV_ITEMS:
            yield NavIcon(item, id="nav-" + item.name, classes="nav-icon")

    def on_mount(self):
        self.refresh_state()
        self.set_interval(1.0, self.refresh_state)

    def refresh_state(self):
        current = type(self.app.screen)
        for icon in self.query(NavIcon):
            icon.set_class(self.app.SCREENS.get(icon.item.name) is current, "nav-active")
            icon.set_class(icon.item.available(self.app) is not None, "nav-disabled")
