#!/usr/bin/env python3

# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import asyncio
import io
import os
import sys
import tempfile
import threading
from unittest import mock

import avocado

path_to_self = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from tests.native_image import native_image
from tests.testutils import remove_at_exit

os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="seine-tui-tests-config-")
remove_at_exit(os.environ["XDG_CONFIG_HOME"])
for _var in ("SEINE_LLM_MODEL", "SEINE_LLM_API_BASE", "SEINE_LLM_API_KEY", "MTDA_REMOTE"):
    os.environ.pop(_var, None)

NATIVE_IMAGE = native_image()

class StartupModalTest(avocado.Test):
    """
    :avocado: tags=tui
    """

    def setUp(self):
        try:
            from seine.tui import commands
            from seine.tui.app import DoctorScreen, OverviewScreen, SeineApp
            from seine.tui.startup import PipeStream, StartupModal, ThreadOutputProxy
        except ImportError as e:
            self.cancel(f"the 'tui' extra (textual) is not installed: {e}")
        self.SeineApp = SeineApp
        self.StartupModal = StartupModal
        self.OverviewScreen = OverviewScreen
        self.DoctorScreen = DoctorScreen
        self.PipeStream = PipeStream
        self.ThreadOutputProxy = ThreadOutputProxy
        self.commands = commands
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

    def test_spec_loading_behind_modal_and_prompt_shut(self):
        from seine.tui.context import Context
        block_event = threading.Event()
        original_use = Context.use

        def blocked_use(context_self, files):
            block_event.wait(timeout=5.0)
            return original_use(context_self, files)

        async def scenario():
            app = self.SeineApp(files=[NATIVE_IMAGE])
            with mock.patch("seine.tui.context.Context.use", blocked_use):
                async with app.run_test(wait_for_spec=False) as pilot:
                    await pilot.pause()
                    self.assertTrue(app._loading_spec)
                    self.assertTrue(app._running_startup)
                    self.assertIsInstance(app.screen, self.StartupModal)

                    # Esc does not close the modal
                    await pilot.press("escape")
                    await pilot.pause()
                    self.assertIsInstance(app.screen, self.StartupModal)

                    # /use command while loading is rejected
                    with self.assertRaises(self.commands.CommandError) as cm:
                        self.commands.dispatch(app, f"/use {NATIVE_IMAGE}")
                    self.assertIn("still loading", str(cm.exception))

                    # Unblock worker and let spec finish loading
                    block_event.set()
                    for _ in range(100):
                        if not app._loading_spec:
                            break
                        await asyncio.sleep(0.05)
                        await pilot.pause()

                    self.assertFalse(app._loading_spec)
                    self.assertIsInstance(app.screen, self.OverviewScreen)
                    self.assertTrue(app.context.active)
                    self.assertFalse(app._running_startup)
        asyncio.run(scenario())

    def test_bad_spec_pops_modal_and_sets_startup_error(self):
        async def scenario():
            app = self.SeineApp(files=["/does/not/exist.yaml"])
            async with app.run_test():
                self.assertIsNotNone(app._startup_error)
                self.assertFalse(app.context.active)
                self.assertIsInstance(app.screen, self.OverviewScreen)
        asyncio.run(scenario())

    def test_no_spec_never_pushes_modal(self):
        async def scenario():
            app = self.SeineApp()
            async with app.run_test():
                self.assertIsInstance(app.screen, self.DoctorScreen)
                self.assertFalse(app._loading_spec)
                self.assertIsNone(app._startup_error)
        asyncio.run(scenario())

    def test_quit_during_load_does_not_hang(self):
        block_event = threading.Event()

        def blocked_use(context_self, files):
            block_event.wait(timeout=5.0)

        async def scenario():
            app = self.SeineApp(files=[NATIVE_IMAGE])
            with mock.patch("seine.tui.context.Context.use", blocked_use):
                async with app.run_test(wait_for_spec=False) as pilot:
                    await pilot.pause()
                    self.assertTrue(app._loading_spec)
                    # Quit should cancel worker and exit cleanly
                    await app.action_quit()
                    block_event.set()
        asyncio.run(scenario())

    def test_thread_output_proxy_routes_worker_and_leaves_main_untouched(self):
        target = io.StringIO()
        real = io.StringIO()
        worker_id = None
        proxy = None

        def worker():
            nonlocal worker_id, proxy
            worker_id = threading.get_ident()
            proxy = self.ThreadOutputProxy(target, real, worker_id)
            proxy.write("from worker\n")

        t = threading.Thread(target=worker)
        t.start()
        t.join()

        # From main thread:
        proxy.write("from main\n")

        self.assertEqual(target.getvalue(), "from worker\n")
        self.assertEqual(real.getvalue(), "from main\n")
