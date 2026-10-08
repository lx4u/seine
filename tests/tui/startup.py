#!/usr/bin/env python3

# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

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

class StartupModalTest(avocado.Test):
    """
    :avocado: tags=tui
    """

    def setUp(self):
        try:
            from seine.tui.app import SeineApp
            from seine.tui.startup import StartupModal
        except ImportError as e:
            self.cancel(f"the 'tui' extra (textual) is not installed: {e}")
        self.SeineApp = SeineApp
        self.StartupModal = StartupModal
        os.environ["SEINE_HISTORY_FILE"] = os.path.join(self.workdir, "history.json")

    def tearDown(self):
        os.environ.pop("SEINE_HISTORY_FILE", None)

    def test_startup_modal_renders_centered_and_esc_does_not_pop(self):
        async def scenario():
            app = self.SeineApp()
            modal = self.StartupModal()
            async with app.run_test() as pilot:
                app.push_screen(modal)
                await pilot.pause()
                self.assertIs(app.screen, modal)
                pane = modal.query_one("#startuppane")
                self.assertIsNotNone(pane)
                await pilot.press("escape")
                await pilot.pause()
                self.assertIs(app.screen, modal)
        asyncio.run(scenario())

    def test_startup_modal_append_lines_from_thread(self):
        async def scenario():
            import threading
            app = self.SeineApp()
            modal = self.StartupModal()
            async with app.run_test() as pilot:
                app.push_screen(modal)
                await pilot.pause()
                def worker():
                    app.call_from_thread(modal.append_lines, ["Getting image source signatures", "Copying blob ... done"])
                threading.Thread(target=worker, daemon=True).start()
                for _ in range(50):
                    if len(modal.query_one("#startuplog").lines) == 2:
                        break
                    await asyncio.sleep(0.05)
                    await pilot.pause()
                log = modal.query_one("#startuplog")
                self.assertEqual(len(log.lines), 2)
                self.assertIn("Getting image source signatures", log.lines[0].text)
                self.assertIn("Copying blob ... done", log.lines[1].text)
        asyncio.run(scenario())
