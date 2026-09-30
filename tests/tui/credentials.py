#!/usr/bin/env python3
# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import asyncio
import avocado
import contextlib
import os
import sys

from unittest import mock

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

# Same guard tests/tui/tui.py uses for anything needing the 'tui' extra.
@contextlib.contextmanager
def _tui_required(test):
    try:
        yield
    except ImportError as e:
        test.cancel("the 'tui' extra (textual) is not installed: %s" % e)

SPEC = """
distribution:
    release: bookworm
    architecture: amd64
    uri: http://example.com/debian
    feeds:
        - suite: bookworm
          auth:
              login: "env:CRED_TEST_LOGIN"
              password: "env:CRED_TEST_PASS"
image:
    filename: cred-test.img
    partitions:
        - label: rootfs
          where: /
"""

def _write_spec(workdir):
    path = os.path.join(workdir, "spec.yaml")
    with open(path, "w") as f:
        f.write(SPEC)
    return path

class CredentialModalDrivesABuildPrompt(avocado.Test):
    """
    :avocado: tags=tui
    """
    def setUp(self):
        with _tui_required(self):
            from seine.image import Image
            from seine.tui.app import SeineApp
            from seine.tui.credentials import CredentialModal
        self.Image = Image
        self.SeineApp = SeineApp
        self.CredentialModal = CredentialModal
        self.real_build = Image.build
        os.environ["SEINE_CACHE_DIR"] = self.workdir
        os.environ["XDG_CONFIG_HOME"] = self.workdir
        os.environ["SEINE_HISTORY_FILE"] = os.path.join(self.workdir, "history.json")
        os.environ.pop("CRED_TEST_LOGIN", None)
        os.environ.pop("CRED_TEST_PASS", None)

    def tearDown(self):
        self.Image.build = self.real_build
        os.environ.pop("SEINE_HISTORY_FILE", None)

    def test_typing_and_submitting_unblocks_the_build(self):
        from seine import tasks

        def instant_build(image, reporter=None):
            for step in tasks.ordered(image.tasks()):
                reporter.started(step.name)
                reporter.finished(step.name, failed=False)
        self.Image.build = instant_build

        spec = _write_spec(self.workdir)

        async def scenario():
            app = self.SeineApp(files=[spec])
            with mock.patch("seine.credentials.probe", return_value=True):
                async with app.run_test() as pilot:
                    try:
                        prompt = app.screen.query_one("#prompt")
                        prompt.value = "/build"
                        await pilot.press("enter")
                        await pilot.pause()

                        for _ in range(200):
                            if isinstance(app.screen, self.CredentialModal):
                                break
                            await asyncio.sleep(0.01)
                            await pilot.pause()
                        self.assertIsInstance(app.screen, self.CredentialModal)

                        from textual.widgets import Input
                        app.screen.query_one("#login", Input).value = "alice"
                        app.screen.query_one("#password", Input).value = "s3cr3t"
                        await pilot.click("#password")
                        await pilot.press("enter")
                        await pilot.pause()

                        for _ in range(200):
                            if app.build_state.done:
                                break
                            await asyncio.sleep(0.01)
                            await pilot.pause()
                        self.assertTrue(app.build_state.done)
                        self.assertFalse(app.build_state.error, app.build_state.message)
                    finally:
                        if app.build_state.running:
                            app.build_state.worker.cancel()

        asyncio.run(scenario())

    def test_escape_cancels_and_fails_the_build(self):
        from seine import tasks

        def instant_build(image, reporter=None):
            for step in tasks.ordered(image.tasks()):
                reporter.started(step.name)
                reporter.finished(step.name, failed=False)
        self.Image.build = instant_build

        spec = _write_spec(self.workdir)

        async def scenario():
            app = self.SeineApp(files=[spec])
            async with app.run_test() as pilot:
                try:
                    prompt = app.screen.query_one("#prompt")
                    prompt.value = "/build"
                    await pilot.press("enter")
                    await pilot.pause()

                    for _ in range(200):
                        if isinstance(app.screen, self.CredentialModal):
                            break
                        await asyncio.sleep(0.01)
                        await pilot.pause()
                    self.assertIsInstance(app.screen, self.CredentialModal)

                    await pilot.press("escape")
                    await pilot.pause()

                    for _ in range(200):
                        if app.build_state.done:
                            break
                        await asyncio.sleep(0.01)
                        await pilot.pause()
                    self.assertTrue(app.build_state.done)
                    self.assertTrue(app.build_state.error)
                finally:
                    if app.build_state.running:
                        app.build_state.worker.cancel()

        asyncio.run(scenario())


if __name__ == "__main__":
    avocado.main()


class CredentialModalSaveChoice(avocado.Test):
    """
    :avocado: tags=tui
    """
    def setUp(self):
        with _tui_required(self):
            from textual.app import App
            from textual.widgets import Checkbox, Input
            from seine.tui.credentials import CredentialModal, tui_prompt
        self.App = App
        self.Checkbox = Checkbox
        self.Input = Input
        self.CredentialModal = CredentialModal
        self.tui_prompt = tui_prompt

    def _submit(self, offer_save, untick=False):
        import threading
        event, result = threading.Event(), {}
        modal = self.CredentialModal(
            "seine-server @ https://srv", {"token": ("", True)}, event, result,
            offer_save=offer_save)
        class Host(self.App):
            def on_mount(self):
                self.push_screen(modal)
        host = Host()

        async def scenario():
            async with host.run_test() as pilot:
                await pilot.pause()
                self.assertEqual(len(modal.query(self.Checkbox)), 1 if offer_save else 0)
                self.assertTrue(modal.query_one("#token", self.Input).password)
                modal.query_one("#token", self.Input).value = "snt_abc"
                if untick:
                    modal.query_one("#save", self.Checkbox).value = False
                await pilot.press("enter")
                await pilot.pause()

        asyncio.run(scenario())
        self.assertTrue(event.is_set())
        return result["values"]

    def test_no_save_choice_unless_offered(self):
        self.assertEqual(self._submit(offer_save=False), {"token": "snt_abc"})

    def test_save_is_ticked_by_default(self):
        self.assertEqual(self._submit(offer_save=True),
                         {"token": "snt_abc", "_save": True})

    def test_unticking_declines_the_save(self):
        self.assertEqual(self._submit(offer_save=True, untick=True),
                         {"token": "snt_abc", "_save": False})

    def test_tui_prompt_forwards_offer_save_to_the_modal(self):
        app = mock.Mock()
        shown = []

        def call_from_thread(fn, modal):
            shown.append(modal)
            modal._result["values"] = {"token": "t", "_save": False}
            modal._event.set()
        app.call_from_thread = call_from_thread
        prompt = self.tui_prompt(app)
        values = prompt("ctx", {"token": ("", True)}, offer_save=True)
        self.assertEqual(values, {"token": "t", "_save": False})
        self.assertTrue(shown[0]._offer_save)
        prompt("ctx", {"token": ("", True)})
        self.assertFalse(shown[1]._offer_save)
