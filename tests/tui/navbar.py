#!/usr/bin/env python3

import asyncio
import os
import sys
import tempfile

import avocado

path_to_self = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from tests.testutils import remove_at_exit

os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="seine-tui-tests-config-")
remove_at_exit(os.environ["XDG_CONFIG_HOME"])
for _var in ("SEINE_LLM_MODEL", "SEINE_LLM_API_BASE", "SEINE_LLM_API_KEY", "MTDA_REMOTE"):
    os.environ.pop(_var, None)

class NavBarTest(avocado.Test):
    """
    :avocado: tags=tui
    """

    def setUp(self):
        try:
            from seine.tui.app import SeineApp
            from seine.tui import navbar
        except ImportError as e:
            self.cancel("the 'tui' extra (textual) is not installed: %s" % e)
        self.SeineApp = SeineApp
        self.navbar = navbar
        os.environ["SEINE_HISTORY_FILE"] = os.path.join(self.workdir, "history.json")
        self.spec = os.path.join(self.workdir, "main.yaml")
        with open(self.spec, "w") as f:
            f.write("distribution:\n"
                    "    release: trixie\n"
                    "    architecture: amd64\n")

    def tearDown(self):
        os.environ.pop("SEINE_HISTORY_FILE", None)

    def _icon(self, app, name):
        return app.screen.query_one("#nav-" + name)

    def test_diff_is_not_in_the_bar(self):
        names = [i.name for i in self.navbar.NAV_ITEMS]
        self.assertEqual(len(names), 15)
        self.assertNotIn("diff", names)
        for item in self.navbar.NAV_ITEMS:
            self.assertEqual(len(item.icon), 1)

    def test_icons_follow_the_active_specification(self):
        async def scenario():
            app = self.SeineApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                self.assertTrue(self._icon(app, "doctor").has_class("nav-active"))
                self.assertTrue(self._icon(app, "artifacts").has_class("nav-disabled"))
                self.assertFalse(self._icon(app, "cache").has_class("nav-disabled"))
                app.context.use([self.spec])
                app.screen.query_one("#navbar").refresh_state()
                self.assertFalse(self._icon(app, "artifacts").has_class("nav-disabled"))
                # No 'image:' section in this specification.
                self.assertTrue(self._icon(app, "plan").has_class("nav-disabled"))
        asyncio.run(scenario())

    def test_click_switches_screen_and_highlights_it(self):
        async def scenario():
            app = self.SeineApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                await pilot.click("#nav-cache")
                await pilot.pause()
                self.assertIs(type(app.screen), app.SCREENS["cache"])
                self.assertTrue(self._icon(app, "cache").has_class("nav-active"))
                self.assertFalse(self._icon(app, "doctor").has_class("nav-active"))
        asyncio.run(scenario())

    def test_clicking_an_unavailable_icon_says_why(self):
        async def scenario():
            from textual.widgets import Static
            app = self.SeineApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                await pilot.click("#nav-artifacts")
                await pilot.pause()
                self.assertIs(type(app.screen), app.SCREENS["doctor"])
                status = app.screen.query_one("#status", Static)
                self.assertIn("no active specification", str(status.render()))
        asyncio.run(scenario())
