#!/usr/bin/env python3
# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import asyncio
import os
import sys
import threading
from unittest import mock
import avocado

path_to_self = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.insert(0, path_to_sources)

from textual.app import App
from textual.widgets import Input, Button
from seine import doctor
from seine.credentials import CredentialNotFound
from seine.tui.credentials import CredentialModal, tui_prompt
from seine.tui.render import render_s3_status, render_cache, render_cache_explain
from seine.tui.commands import _cache, CommandError


class CredentialModalTest(avocado.Test):
    def test_modal_compose_and_submit_with_s3_fields(self):
        async def scenario():
            fields = {
                "access_key": ("key1", False),
                "secret_key": ("secret1", True),
            }
            event = threading.Event()
            result = {}
            modal = CredentialModal("s3 storage", fields, event, result)

            class TestApp(App):
                def on_mount(self):
                    self.push_screen(modal)

            app = TestApp()
            async with app.run_test() as pilot:
                self.assertIsNotNone(modal.query_one("#access_key", Input))
                self.assertIsNotNone(modal.query_one("#secret_key", Input))
                self.assertIsNotNone(modal.query_one("#reveal_secret_key", Button))

                modal.query_one("#access_key", Input).value = "my-key"
                modal.query_one("#secret_key", Input).value = "my-secret"
                modal._submit()
                self.assertTrue(event.is_set())
                self.assertEqual(result["values"]["access_key"], "my-key")
                self.assertEqual(result["values"]["secret_key"], "my-secret")

        asyncio.run(scenario())

    def test_modal_cancel(self):
        async def scenario():
            fields = {"access_key": ("key1", False)}
            event = threading.Event()
            result = {}
            modal = CredentialModal("s3 storage", fields, event, result)

            class TestApp(App):
                def on_mount(self):
                    self.push_screen(modal)

            app = TestApp()
            async with app.run_test() as pilot:
                modal.action_cancel()
                self.assertTrue(event.is_set())
                self.assertTrue(result.get("cancelled"))

        asyncio.run(scenario())

    def test_modal_reveal_toggle(self):
        async def scenario():
            fields = {"secret_key": ("secret1", True)}
            event = threading.Event()
            result = {}
            modal = CredentialModal("s3 storage", fields, event, result)

            class TestApp(App):
                def on_mount(self):
                    self.push_screen(modal)

            app = TestApp()
            async with app.run_test() as pilot:
                secret_input = modal.query_one("#secret_key", Input)
                reveal_btn = modal.query_one("#reveal_secret_key", Button)
                self.assertTrue(secret_input.password)

                mock_btn = mock.MagicMock(id="reveal_secret_key", label=modal.HIDDEN_ICON)
                btn_event = mock.MagicMock(button=mock_btn)

                modal.on_button_pressed(btn_event)
                self.assertFalse(secret_input.password)
                self.assertEqual(mock_btn.label, modal.REVEALED_ICON)

                modal.on_button_pressed(btn_event)
                self.assertTrue(secret_input.password)
                self.assertEqual(mock_btn.label, modal.HIDDEN_ICON)

        asyncio.run(scenario())

    def test_tui_prompt_integration(self):
        app = mock.MagicMock()

        def fake_push(modal):
            modal._result["values"] = {"access_key": "k", "secret_key": "s"}
            modal._event.set()

        app.call_from_thread.side_effect = lambda fn, modal: fake_push(modal)
        prompt = tui_prompt(app)
        values = prompt("s3 storage", {"access_key": ("", False), "secret_key": ("", True)})
        self.assertEqual(values, {"access_key": "k", "secret_key": "s"})

    def test_tui_prompt_cancelled_raises_credential_not_found(self):
        app = mock.MagicMock()

        def fake_cancel(modal):
            modal._result["cancelled"] = True
            modal._event.set()

        app.call_from_thread.side_effect = lambda fn, modal: fake_cancel(modal)
        prompt = tui_prompt(app)
        with self.assertRaises(CredentialNotFound):
            prompt("s3 storage", {"access_key": ("", False), "secret_key": ("", True)})


class DoctorS3CheckTest(avocado.Test):
    def test_check_s3_not_configured_returns_none(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(doctor.check_s3({}))

    def test_check_s3_credentials_missing_returns_warn(self):
        options = {"shared_cache": True, "s3_endpoint": "http://127.0.0.1:9000"}
        with mock.patch("seine.credentials.s3_credential_source") as mock_src:
            instance = mock.MagicMock()
            instance.get.side_effect = CredentialNotFound("missing")
            mock_src.return_value = instance

            check = doctor.check_s3(options)
            self.assertIsNotNone(check)
            self.assertEqual(check.status, "warn")
            self.assertIn("credentials missing", check.detail)

    def test_check_s3_reachable_returns_ok(self):
        options = {"shared_cache": True, "s3_endpoint": "http://127.0.0.1:9000", "s3_bucket": "test-b"}
        with mock.patch("seine.credentials.s3_credential_source") as mock_src, \
             mock.patch("seine.credentials.probe_s3", return_value=True):
            instance = mock.MagicMock()
            instance.get.return_value = {"access_key": "a", "secret_key": "s"}
            mock_src.return_value = instance

            check = doctor.check_s3(options)
            self.assertIsNotNone(check)
            self.assertEqual(check.status, "ok")
            self.assertEqual(check.detail, "reachable")
            self.assertEqual(check.name, "test-b @ http://127.0.0.1:9000")

    def test_check_s3_unreachable_returns_warn(self):
        options = {"shared_cache": True, "s3_endpoint": "http://127.0.0.1:9000", "s3_bucket": "test-b"}
        with mock.patch("seine.credentials.s3_credential_source") as mock_src, \
             mock.patch("seine.credentials.probe_s3", return_value=False):
            instance = mock.MagicMock()
            instance.get.return_value = {"access_key": "a", "secret_key": "s"}
            mock_src.return_value = instance

            check = doctor.check_s3(options)
            self.assertIsNotNone(check)
            self.assertEqual(check.status, "warn")
            self.assertIn("not reachable", check.detail)

    def test_doctor_run_includes_s3_when_configured(self):
        options = {"shared_cache": True, "s3_endpoint": "http://127.0.0.1:9000", "s3_bucket": "test-b"}
        with mock.patch.object(doctor, "check_s3", return_value=doctor.Check(doctor.GROUP_S3, "s3", "ok", "reachable")):
            checks = doctor.run(options=options)
            names = [c.name for c in checks]
            self.assertIn("s3", names)


class TUICacheScreenTest(avocado.Test):
    def test_render_s3_status_online_and_offline(self):
        mock_check_ok = doctor.Check(doctor.GROUP_S3, "seine-cache @ http://127.0.0.1:9000", "ok", "reachable")
        with mock.patch.object(doctor, "check_s3", return_value=mock_check_ok):
            text = render_s3_status()
            self.assertIn("REMOTE CACHE (S3)", text)
            self.assertIn("online (reachable)", text)

        mock_check_warn = doctor.Check(doctor.GROUP_S3, "seine-cache @ http://127.0.0.1:9000", "warn", "credentials missing")
        with mock.patch.object(doctor, "check_s3", return_value=mock_check_warn):
            text = render_s3_status()
            self.assertIn("REMOTE CACHE (S3)", text)
            self.assertIn("offline (credentials missing)", text)

    def test_render_cache_includes_s3_status(self):
        mock_check = doctor.Check(doctor.GROUP_S3, "seine-cache @ http://127.0.0.1:9000", "ok", "reachable")
        with mock.patch.object(doctor, "check_s3", return_value=mock_check), \
             mock.patch("seine.cache.CacheCmd.info"):
            text = render_cache()
            self.assertIn("REMOTE CACHE (S3)", text)
            self.assertIn("seine-cache @ http://127.0.0.1:9000", text)

    def test_render_cache_explain_delegation(self):
        with mock.patch("seine.cache.CacheCmd.explain") as mock_explain:
            render_cache_explain(["target1.recipe", "target2.recipe"])
            mock_explain.assert_called_once_with(["target1.recipe", "target2.recipe"], options={})

    def test_tui_cache_command_routing(self):
        app = mock.MagicMock()
        app.context.active = False
        app.context.builds = []

        # Plain /cache: clears cache_text and shows cache
        _cache(app, [])
        self.assertIsNone(app.cache_text)
        app.show.assert_called_with("cache")

        # /cache explain ...: sets cache_text and shows cache
        app.show.reset_mock()
        with mock.patch("seine.tui.render.render_cache_explain", return_value="recipe diff output"):
            _cache(app, ["explain", "recipe1.recipe", "recipe2.recipe"])
            self.assertEqual(app.cache_text, "recipe diff output")
            app.show.assert_called_with("cache")

        # /cache explain with no targets raises CommandError
        with self.assertRaises(CommandError):
            _cache(app, ["explain"])

        # /cache why with no package raises CommandError
        with self.assertRaises(CommandError):
            _cache(app, ["why"])

        # /cache with invalid subcommand raises CommandError
        with self.assertRaises(CommandError):
            _cache(app, ["invalid"])
