#!/usr/bin/env python3
# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import avocado
import json
import os
import sys
import tempfile

from unittest import mock

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine.credentials import (
    CredentialError, CredentialNotFound, CredentialSource,
    _parse, _keyring_reachable, _save_to_keyring, _save_to_settings,
    _SETTINGS_FILE_ENV,
    resolve,
)


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

class _FailKeyring:
    """Stands in for keyring.backends.fail.Keyring."""


def _keyring_modules(get_password=None, lookup_error=None, set_error=None,
                     reachable=True):
    keyring_mod = mock.MagicMock(name="keyring")
    if lookup_error is not None:
        keyring_mod.get_password.side_effect = lookup_error
    else:
        keyring_mod.get_password.return_value = get_password
    if set_error is not None:
        keyring_mod.set_password.side_effect = set_error
    keyring_mod.get_keyring.return_value = (
        object() if reachable else _FailKeyring())

    fail_mod = mock.MagicMock(name="keyring.backends.fail")
    fail_mod.Keyring = _FailKeyring
    backends_mod = mock.MagicMock(name="keyring.backends")
    backends_mod.fail = fail_mod

    return {
        "keyring": keyring_mod,
        "keyring.backends": backends_mod,
        "keyring.backends.fail": fail_mod,
    }


def _settings_env(path):
    return {_SETTINGS_FILE_ENV: path}


def _write_settings(path, data):
    with open(path, "w") as f:
        json.dump(data, f)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

class Parsing(avocado.Test):

    def test_single_env_segment(self):
        self.assertEqual(_parse("env:MY_VAR"), [("env", "MY_VAR")])

    def test_full_four_backend_chain(self):
        result = _parse(
            "keyring:item | settings:item | vault:kv/data/x#f | env:VAR")
        self.assertEqual(result, [
            ("keyring",  "item"),
            ("settings", "item"),
            ("vault",    "kv/data/x#f"),
            ("env",      "VAR"),
        ])

    def test_whitespace_around_pipe_stripped(self):
        self.assertEqual(_parse("env:A|env:B"), [("env", "A"), ("env", "B")])
        self.assertEqual(_parse("env:A  |  env:B"), [("env", "A"), ("env", "B")])

    def test_empty_spec_raises(self):
        with self.assertRaises(CredentialError):
            _parse("")

    def test_empty_segment_between_pipes_raises(self):
        with self.assertRaises(CredentialError):
            _parse("env:A | | env:B")

    def test_literal_value_raises(self):
        # No colon at all -- a bare literal is refused, never resolved.
        with self.assertRaises(CredentialError):
            _parse("hunter2")

    def test_unknown_backend_raises(self):
        with self.assertRaises(CredentialError):
            _parse("netrc:~/.netrc")

    def test_empty_name_raises(self):
        with self.assertRaises(CredentialError):
            _parse("env:")

    def test_non_string_raises(self):
        with self.assertRaises(CredentialError):
            _parse(42)

    def test_vault_ref_with_slashes_and_hash_preserved(self):
        self.assertEqual(
            _parse("vault:kv/data/feeds/private#password"),
            [("vault", "kv/data/feeds/private#password")])


# ---------------------------------------------------------------------------
# Backend: env
# ---------------------------------------------------------------------------

class EnvBackend(avocado.Test):

    def test_set_variable_returned(self):
        with mock.patch.dict(os.environ, {"APT_HTTP_USER": "alice"}):
            self.assertEqual(resolve("env:APT_HTTP_USER"), "alice")

    def test_skip_empty_makes_an_empty_value_fall_through(self):
        with mock.patch.dict(os.environ, {"A": "", "B": "second"}):
            self.assertEqual(resolve("env:A | env:B", skip_empty=True), "second")
            with self.assertRaises(CredentialNotFound):
                resolve("env:A", skip_empty=True)

    def test_credential_source_skip_empty(self):
        with mock.patch.dict(os.environ, {"A": "", "B": "second"}):
            src = CredentialSource({"x": "env:A | env:B"}, skip_empty=True)
            self.assertEqual(src.get(), {"x": "second"})

    def test_empty_string_is_valid_not_a_miss(self):
        with mock.patch.dict(os.environ, {"APT_HTTP_USER": ""}):
            self.assertEqual(resolve("env:APT_HTTP_USER"), "")

    def test_unset_variable_raises(self):
        env = {k: v for k, v in os.environ.items() if k != "APT_HTTP_USER"}
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(CredentialNotFound):
                resolve("env:APT_HTTP_USER")


# ---------------------------------------------------------------------------
# Backend: keyring
# ---------------------------------------------------------------------------

class KeyringBackend(avocado.Test):

    def test_found_item_returned(self):
        mods = _keyring_modules(get_password="s3cr3t")
        with mock.patch.dict(sys.modules, mods):
            self.assertEqual(resolve("keyring:apt-http-user"), "s3cr3t")

    def test_none_result_raises_not_found(self):
        mods = _keyring_modules(get_password=None)
        with mock.patch.dict(sys.modules, mods):
            with self.assertRaises(CredentialNotFound):
                resolve("keyring:missing")

    def test_package_unavailable_raises_not_found(self):
        with mock.patch.dict(sys.modules, {"keyring": None}):
            with self.assertRaises(CredentialNotFound):
                resolve("keyring:anything")

    def test_backend_error_raises_not_found(self):
        mods = _keyring_modules(lookup_error=Exception("locked"))
        with mock.patch.dict(sys.modules, mods):
            with self.assertRaises(CredentialNotFound):
                resolve("keyring:anything")

    def test_not_found_is_subtype_of_credential_error(self):
        mods = _keyring_modules(get_password=None)
        with mock.patch.dict(sys.modules, mods):
            try:
                resolve("keyring:x")
                self.fail("expected CredentialNotFound")
            except CredentialNotFound:
                pass
            except CredentialError:
                self.fail("wanted CredentialNotFound, got plain CredentialError")


# ---------------------------------------------------------------------------
# Backend: settings
# ---------------------------------------------------------------------------

class SettingsBackend(avocado.Test):

    def test_found_key_returned(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json",
                                        delete=False) as f:
            json.dump({"apt-http-user": "alice"}, f)
            path = f.name
        try:
            with mock.patch.dict(os.environ, _settings_env(path)):
                self.assertEqual(resolve("settings:apt-http-user"), "alice")
        finally:
            os.unlink(path)

    def test_missing_file_raises_not_found(self):
        with mock.patch.dict(os.environ,
                             _settings_env("/nonexistent/credentials.json")):
            with self.assertRaises(CredentialNotFound):
                resolve("settings:anything")

    def test_missing_key_raises_not_found(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json",
                                        delete=False) as f:
            json.dump({"other-key": "v"}, f)
            path = f.name
        try:
            with mock.patch.dict(os.environ, _settings_env(path)):
                with self.assertRaises(CredentialNotFound):
                    resolve("settings:apt-http-user")
        finally:
            os.unlink(path)

    def test_non_string_value_raises_credential_error(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json",
                                        delete=False) as f:
            json.dump({"port": 8080}, f)
            path = f.name
        try:
            with mock.patch.dict(os.environ, _settings_env(path)):
                with self.assertRaises(CredentialError):
                    resolve("settings:port")
        finally:
            os.unlink(path)

    def test_invalid_json_raises_credential_error(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json",
                                        delete=False) as f:
            f.write("not json{")
            path = f.name
        try:
            with mock.patch.dict(os.environ, _settings_env(path)):
                with self.assertRaises(CredentialError):
                    resolve("settings:anything")
        finally:
            os.unlink(path)

    def test_wide_permissions_warn_but_still_resolve(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json",
                                        delete=False) as f:
            json.dump({"k": "v"}, f)
            path = f.name
        try:
            os.chmod(path, 0o644)
            with mock.patch.dict(os.environ, _settings_env(path)):
                with mock.patch("sys.stderr") as stderr:
                    value = resolve("settings:k")
            self.assertEqual(value, "v")
            self.assertTrue(stderr.write.called)
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------
# Backend: vault (via an injected reader -- no seine.vault import)
# ---------------------------------------------------------------------------

class VaultBackend(avocado.Test):

    def test_no_reader_configured_is_a_miss(self):
        with self.assertRaises(CredentialNotFound):
            resolve("vault:kv/data/x#f")

    def test_reader_receives_full_ref(self):
        reader = mock.Mock(return_value="hunter2")
        value = resolve("vault:kv/data/feeds/apt#password", vault_reader=reader)
        self.assertEqual(value, "hunter2")
        reader.assert_called_once_with("kv/data/feeds/apt#password")

    def test_reader_vault_not_found_becomes_credential_not_found(self):
        class VaultNotFound(Exception):
            pass
        reader = mock.Mock(side_effect=VaultNotFound("404"))
        with self.assertRaises(CredentialNotFound):
            resolve("vault:kv/data/x#f", vault_reader=reader)

    def test_reader_other_error_becomes_credential_error(self):
        reader = mock.Mock(side_effect=Exception("connection refused"))
        with self.assertRaises(CredentialError):
            resolve("vault:kv/data/x#f", vault_reader=reader)


# ---------------------------------------------------------------------------
# Chain resolution
# ---------------------------------------------------------------------------

class Chain(avocado.Test):

    def test_first_hit_short_circuits(self):
        with mock.patch.dict(os.environ, {"A": "first"}):
            with mock.patch("seine.credentials._resolve_keyring") as keyring:
                value = resolve("env:A | keyring:b")
        self.assertEqual(value, "first")
        keyring.assert_not_called()

    def test_first_miss_second_hit(self):
        env = dict(os.environ)
        env.pop("MISSING", None)
        env["PRESENT"] = "found"
        with mock.patch.dict(os.environ, env, clear=True):
            value = resolve("env:MISSING | env:PRESENT")
        self.assertEqual(value, "found")

    def test_all_miss_raises_collecting_all_reasons(self):
        class VaultNotFound(Exception):
            pass
        env = dict(os.environ)
        env.pop("MISSING_VAR", None)
        with mock.patch.dict(os.environ, env, clear=True):
            reader = mock.Mock(side_effect=VaultNotFound("vault miss"))
            try:
                resolve("vault:kv/data/x#f | env:MISSING_VAR", vault_reader=reader)
                self.fail("expected CredentialNotFound")
            except CredentialNotFound as e:
                self.assertIn("vault", str(e))
                self.assertIn("MISSING_VAR", str(e))

    def test_hard_vault_error_stops_chain(self):
        reader = mock.Mock(side_effect=Exception("TLS failure"))
        with mock.patch.dict(os.environ, {"FALLBACK": "v"}):
            with self.assertRaises(CredentialError):
                resolve("vault:kv/data/x#f | env:FALLBACK", vault_reader=reader)


# ---------------------------------------------------------------------------
# Write-back (module-level helpers, exercised through CredentialSource below
# too, but also directly for the settings/keyring specifics)
# ---------------------------------------------------------------------------

class WriteBack(avocado.Test):

    def test_settings_write_is_atomic_and_creates_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "sub", "credentials.json")
            with mock.patch.dict(os.environ, _settings_env(path)):
                _save_to_settings("apt-http-user", "alice")
            with open(path) as f:
                data = json.load(f)
        self.assertEqual(data, {"apt-http-user": "alice"})

    def test_settings_write_preserves_existing_keys(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json",
                                        delete=False) as f:
            json.dump({"other": "existing"}, f)
            path = f.name
        try:
            with mock.patch.dict(os.environ, _settings_env(path)):
                _save_to_settings("new-key", "new-val")
            with open(path) as f:
                data = json.load(f)
            self.assertEqual(data["other"], "existing")
            self.assertEqual(data["new-key"], "new-val")
        finally:
            os.unlink(path)

    def test_settings_write_is_0600(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "credentials.json")
            with mock.patch.dict(os.environ, _settings_env(path)):
                _save_to_settings("k", "v")
            import stat
            mode = stat.S_IMODE(os.stat(path).st_mode)
            self.assertEqual(mode, 0o600)

    def test_settings_save_errors_are_swallowed(self):
        with mock.patch.dict(os.environ,
                             _settings_env("/root/no-permission/creds.json")):
            _save_to_settings("x", "v")  # must not raise

    def test_keyring_save_errors_are_swallowed(self):
        mods = _keyring_modules(set_error=Exception("locked"))
        with mock.patch.dict(sys.modules, mods):
            _save_to_keyring("x", "v")  # must not raise


# ---------------------------------------------------------------------------
# CredentialSource
# ---------------------------------------------------------------------------

class CredentialSourceClass(avocado.Test):

    def _make_src(self, fields=None, context=None, prompt=None, vault_reader=None):
        fields = fields or {"login": "env:LOGIN", "password": "env:PASS"}
        return CredentialSource(fields, context=context, prompt=prompt,
                                vault_reader=vault_reader)

    def test_get_returns_provider_values_for_both_fields(self):
        with mock.patch.dict(os.environ, {"LOGIN": "alice", "PASS": "hunter2"}):
            src = self._make_src()
            self.assertEqual(src.get(), {"login": "alice", "password": "hunter2"})

    def test_get_caches_result(self):
        with mock.patch.dict(os.environ, {"LOGIN": "a", "PASS": "b"}):
            src = self._make_src()
            src.get()
        # Second call must not re-resolve (env is now gone).
        self.assertEqual(src.get(), {"login": "a", "password": "b"})

    def test_no_prompt_needed_when_both_fields_hit(self):
        prompt = mock.Mock()
        with mock.patch.dict(os.environ, {"LOGIN": "a", "PASS": "b"}):
            src = self._make_src(prompt=prompt)
            src.get()
        prompt.assert_not_called()

    def test_missing_field_triggers_one_prompt_for_the_whole_pair(self):
        env = dict(os.environ)
        env["LOGIN"] = "alice"
        env.pop("PASS", None)
        prompt = mock.Mock(return_value={"login": "alice", "password": "typed"})
        with mock.patch.dict(os.environ, env, clear=True):
            src = self._make_src(prompt=prompt)
            values = src.get()
        self.assertEqual(values, {"login": "alice", "password": "typed"})
        prompt.assert_called_once()

    def test_prompt_receives_chain_default_and_empty_for_missing(self):
        env = dict(os.environ)
        env["LOGIN"] = "alice"
        env.pop("PASS", None)
        prompt = mock.Mock(return_value={"login": "alice", "password": "typed"})
        with mock.patch.dict(os.environ, env, clear=True):
            self._make_src(prompt=prompt).get()
        _, fields = prompt.call_args[0]
        self.assertEqual(fields["login"], ("alice", False))
        self.assertEqual(fields["password"], ("", True))

    def test_prompt_context_is_passed_through_on_first_prompt(self):
        env = dict(os.environ)
        env.pop("LOGIN", None)
        env.pop("PASS", None)
        prompt = mock.Mock(return_value={"login": "a", "password": "b"})
        with mock.patch.dict(os.environ, env, clear=True):
            self._make_src(context="https://repo.example.com/debian",
                           prompt=prompt).get()
        context, _ = prompt.call_args[0]
        self.assertEqual(context, "https://repo.example.com/debian")

    def test_no_prompt_raises_when_a_field_misses(self):
        env = dict(os.environ)
        env.pop("PASS", None)
        env["LOGIN"] = "a"
        with mock.patch.dict(os.environ, env, clear=True):
            src = self._make_src()  # no prompt
            with self.assertRaises(CredentialNotFound):
                src.get()

    def test_failed_reprompts_both_fields_prefilled(self):
        env = dict(os.environ)
        env["LOGIN"] = "alice"
        env["PASS"] = "wrong"
        prompt = mock.Mock(return_value={"login": "alice", "password": "correct"})
        with mock.patch.dict(os.environ, env, clear=True):
            src = self._make_src(prompt=prompt)
            src.get()
            values = src.failed()
        self.assertEqual(values, {"login": "alice", "password": "correct"})
        _, fields = prompt.call_args[0]
        self.assertEqual(fields["login"], ("alice", False))
        self.assertEqual(fields["password"], ("wrong", True))

    def test_failed_context_mentions_attempt_number(self):
        env = dict(os.environ)
        env["LOGIN"] = "alice"
        env["PASS"] = "wrong"
        prompt = mock.Mock(return_value={"login": "alice", "password": "v"})
        with mock.patch.dict(os.environ, env, clear=True):
            src = self._make_src(context="Feed 'https://x'", prompt=prompt)
            src.get()
            src.failed()
        context, _ = prompt.call_args[0]
        self.assertIn("attempt 1", context)
        self.assertIn("3", context)  # of MAX_ATTEMPTS

    def test_failed_raises_after_max_attempts(self):
        env = dict(os.environ)
        env.pop("LOGIN", None)
        env.pop("PASS", None)
        prompt = mock.Mock(return_value={"login": "a", "password": "b"})
        with mock.patch.dict(os.environ, env, clear=True):
            src = self._make_src(prompt=prompt)
            src.get()          # attempt 1
            src.failed()       # attempt 2
            src.failed()       # attempt 3
            with self.assertRaises(CredentialNotFound):
                src.failed()   # over the limit

    def test_failed_raises_when_no_prompt_available(self):
        with mock.patch.dict(os.environ, {"LOGIN": "a", "PASS": "b"}):
            src = self._make_src()  # no prompt
            src.get()
        with self.assertRaises(CredentialNotFound):
            src.failed()

    def test_commit_is_noop_when_nothing_was_prompted(self):
        with mock.patch.dict(os.environ, {"LOGIN": "a", "PASS": "b"}):
            src = self._make_src(fields={
                "login": "keyring:l | env:LOGIN",
                "password": "keyring:p | env:PASS"})
            src.get()
        with mock.patch("seine.credentials._save_to_keyring") as ks:
            src.commit()
        ks.assert_not_called()

    def test_commit_writes_the_whole_prompted_pair_to_each_own_chain(self):
        # login already resolved from its own chain, but the modal shows
        # (and returns) both fields together -- commit() persists both,
        # each to its own chain's writable backends.
        env = dict(os.environ)
        env["LOGIN"] = "alice"   # resolves from chain
        env.pop("PASS", None)    # missing -- forces the pair prompt
        prompt = mock.Mock(return_value={"login": "alice", "password": "secret"})
        with mock.patch.dict(os.environ, env, clear=True):
            src = self._make_src(fields={
                "login": "keyring:l | env:LOGIN",
                "password": "keyring:p | env:PASS"}, prompt=prompt)
            src.get()
        with mock.patch("seine.credentials._keyring_reachable", return_value=True):
            with mock.patch("seine.credentials._save_to_keyring") as ks:
                src.commit()
        ks.assert_any_call("l", "alice")
        ks.assert_any_call("p", "secret")
        self.assertEqual(ks.call_count, 2)

    def test_commit_writes_to_first_working_backend_only(self):
        env = dict(os.environ)
        env.pop("LOGIN", None)
        env.pop("PASS", None)
        prompt = mock.Mock(return_value={"login": "alice", "password": "secret"})
        with mock.patch.dict(os.environ, env, clear=True):
            src = self._make_src(fields={
                "login": "keyring:l | settings:l | env:LOGIN",
                "password": "env:PASS"}, prompt=prompt)
            src.get()
        with mock.patch("seine.credentials._keyring_reachable", return_value=True):
            with mock.patch("seine.credentials._save_to_keyring") as ks:
                with mock.patch("seine.credentials._save_to_settings") as ss:
                    src.commit()
        ks.assert_called_once_with("l", "alice")
        ss.assert_not_called()

    def test_commit_falls_back_to_settings_when_keyring_save_fails(self):
        env = dict(os.environ)
        env.pop("LOGIN", None)
        env.pop("PASS", None)
        prompt = mock.Mock(return_value={"login": "alice", "password": "secret"})
        with mock.patch.dict(os.environ, env, clear=True):
            src = self._make_src(fields={
                "login": "keyring:l | settings:l | env:LOGIN",
                "password": "env:PASS"}, prompt=prompt)
            src.get()
        with mock.patch("seine.credentials._keyring_reachable", return_value=True):
            with mock.patch("seine.credentials._save_to_keyring", return_value=False):
                with mock.patch("seine.credentials._save_to_settings") as ss:
                    src.commit()
        ss.assert_called_once_with("l", "alice")

    def test_offer_save_passes_the_flag_to_the_prompt(self):
        prompt = mock.Mock(return_value={"token": "t", "_save": True})
        with mock.patch.dict(os.environ, {}, clear=True):
            src = CredentialSource({"token": "env:T"}, prompt=prompt,
                                   offer_save=True)
            self.assertEqual(src.get(), {"token": "t"})
        prompt.assert_called_once()
        self.assertEqual(prompt.call_args[1], {"offer_save": True})

    def test_declined_save_writes_nothing(self):
        prompt = mock.Mock(return_value={"token": "t", "_save": False})
        with mock.patch.dict(os.environ, {}, clear=True):
            src = CredentialSource({"token": "keyring:t | settings:t | env:T"},
                                   prompt=prompt, offer_save=True)
            src.get()
        with mock.patch("seine.credentials._keyring_reachable", return_value=True):
            with mock.patch("seine.credentials._save_to_keyring") as ks:
                with mock.patch("seine.credentials._save_to_settings") as ss:
                    src.commit()
        ks.assert_not_called()
        ss.assert_not_called()

    def test_prompt_without_offer_save_keeps_the_two_argument_call(self):
        prompt = mock.Mock(return_value={"login": "a", "password": "b"})
        with mock.patch.dict(os.environ, {}, clear=True):
            self._make_src(prompt=prompt).get()
        self.assertEqual(prompt.call_args[1], {})

    def test_commit_skips_unreachable_keyring(self):
        env = dict(os.environ)
        env.pop("LOGIN", None)
        env.pop("PASS", None)
        prompt = mock.Mock(return_value={"login": "alice", "password": "secret"})
        with mock.patch.dict(os.environ, env, clear=True):
            src = self._make_src(fields={
                "login": "keyring:l | settings:l | env:LOGIN",
                "password": "env:PASS"}, prompt=prompt)
            src.get()
        with mock.patch("seine.credentials._keyring_reachable", return_value=False):
            with mock.patch("seine.credentials._save_to_keyring") as ks:
                with mock.patch("seine.credentials._save_to_settings") as ss:
                    src.commit()
        ks.assert_not_called()
        ss.assert_called_once_with("l", "alice")

    def test_commit_is_idempotent(self):
        env = dict(os.environ)
        env.pop("LOGIN", None)
        env.pop("PASS", None)
        prompt = mock.Mock(return_value={"login": "alice", "password": "secret"})
        with mock.patch.dict(os.environ, env, clear=True):
            src = self._make_src(fields={
                "login": "keyring:l | env:LOGIN",
                "password": "env:PASS"}, prompt=prompt)
            src.get()
        with mock.patch("seine.credentials._keyring_reachable", return_value=True):
            with mock.patch("seine.credentials._save_to_keyring") as ks:
                src.commit()
                src.commit()
                src.commit()
        ks.assert_called_once_with("l", "alice")


if __name__ == "__main__":
    avocado.main()
