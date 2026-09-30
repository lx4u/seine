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
