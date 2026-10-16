# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# Secondary inspection screens: plan, packages, analyze, doctor, diff.

from textual.widgets import Static

from seine.tui.base import BaseScreen
from seine.tui.render import (render_analyze, render_doctor,
                              render_packages, render_plan)


class PlanScreen(BaseScreen):
    def update_body(self):
        self.query_one("#body", Static).update(render_plan(self.app.context))


class PackagesScreen(BaseScreen):
    def update_body(self):
        self.query_one("#body", Static).update(render_packages(self.app.context))


class AnalyzeScreen(BaseScreen):
    def update_body(self):
        self.query_one("#body", Static).update(render_analyze(self.app.context))


class DoctorScreen(BaseScreen):
    def update_body(self):
        options = {}
        if self.app.context and self.app.context.active and self.app.context.builds:
            options = getattr(self.app.context.builds[0], "options", {}) or {}
            if "spec" not in options and hasattr(self.app.context.builds[0], "spec"):
                options = dict(options, spec=self.app.context.builds[0].spec)
        self.query_one("#body", Static).update(render_doctor(options=options))


# What /diff last computed -- not spec-scoped, so reads app.diff_text.
class DiffScreen(BaseScreen):
    def update_body(self):
        text = self.app.diff_text or (
            "no diff yet -- '/diff OLD.spdx.json NEW.spdx.json'\n")
        self.query_one("#body", Static).update(text)
