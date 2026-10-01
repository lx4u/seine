# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0
"""Tests for seine-server settings loading and validation."""

import os
import shutil
import tempfile

from avocado import Test

from seine.distributed.server.settings import Settings, SettingsError, is_loopback


class SettingsTest(Test):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="seine-test-settings-")

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _write_config(self, text):
        path = os.path.join(self.tmp_dir, "server.yaml")
        with open(path, "w") as f:
            f.write(text)
        return path

    def test_defaults(self):
        s = Settings.load(config_path=None, env={})
        self.assertEqual(s.host, "127.0.0.1")
        self.assertEqual(s.port, 8000)
        self.assertEqual(s.max_upload_bytes, 512 * 1024 * 1024)
        self.assertEqual(s.stale_after, 120.0)
        self.assertEqual(s.reap_interval, 30.0)
        self.assertEqual(s.native_grace, 30.0)
        self.assertEqual(s.job_lost_grace, 90.0)
        self.assertIsNone(s.enrollment_token)

    def test_new_user_project_setting(self):
        self.assertEqual(Settings.load(config_path=None, env={}).new_user_project, "none")
        s = Settings.load(config_path=None, env={"SEINE_NEW_USER_PROJECT": "auto"})
        self.assertEqual(s.new_user_project, "auto")
        Settings(enrollment_token="t", new_user_project="auto").validate()
        Settings(enrollment_token="t", new_user_project="team-a").validate()
        with self.assertRaises(SettingsError):
            Settings(enrollment_token="t", new_user_project="Not A Name").validate()

    def test_native_grace_from_environment_and_validation(self):
        s = Settings.load(config_path=None, env={"SEINE_NATIVE_GRACE": "5"})
        self.assertEqual(s.native_grace, 5.0)
        with self.assertRaises(SettingsError):
            Settings(enrollment_token="t", native_grace=-1).validate()

    def test_job_lost_grace_from_file_environment_and_validation(self):
        path = self._write_config("job_lost_grace: 45\n")
        self.assertEqual(Settings.load(config_path=path, env={}).job_lost_grace, 45.0)
        s = Settings.load(config_path=path, env={"SEINE_JOB_LOST_GRACE": "20"})
        self.assertEqual(s.job_lost_grace, 20.0)
        for bad in (0, -1):
            with self.assertRaises(SettingsError):
                Settings(enrollment_token="t", job_lost_grace=bad).validate()
        with self.assertRaises(SettingsError):
            Settings.load(config_path=None, env={"SEINE_JOB_LOST_GRACE": "soon"})

    def test_file_overrides_defaults_flat(self):
        path = self._write_config("port: 9000\nenrollment_token: from-file\n")
        s = Settings.load(config_path=path, env={})
        self.assertEqual((s.port, s.enrollment_token, s.config_path), (9000, "from-file", path))

    def test_file_server_section_and_database_block(self):
        path = self._write_config(
            "server:\n  host: 10.0.0.1\n  stale_after: 60\n"
            "database:\n  type: sqlite\n  path: /var/lib/seine/x.db\n"
        )
        s = Settings.load(config_path=path, env={})
        self.assertEqual((s.host, s.stale_after, s.db_path), ("10.0.0.1", 60.0, "/var/lib/seine/x.db"))

    def test_precedence_file_env_flags(self):
        path = self._write_config("port: 9000\nhost: 10.0.0.1\nenrollment_token: file\n")
        env = {"SEINE_PORT": "9100", "SEINE_ENROLLMENT_TOKEN": "env"}
        s = Settings.load(config_path=path, env=env, overrides={"enrollment_token": "flag", "host": None})
        self.assertEqual(s.enrollment_token, "flag")
        self.assertEqual(s.port, 9100)
        self.assertEqual(s.host, "10.0.0.1")

    def test_config_path_from_environment(self):
        path = self._write_config("port: 9200\n")
        self.assertEqual(Settings.load(env={"SEINE_SERVER_CONFIG": path}).port, 9200)

    def test_missing_explicit_config_is_an_error(self):
        with self.assertRaises(SettingsError):
            Settings.load(config_path=os.path.join(self.tmp_dir, "nope.yaml"), env={})

    def test_unknown_key_and_bad_value_are_errors(self):
        with self.assertRaises(SettingsError):
            Settings.load(config_path=self._write_config("prot: 1\n"), env={})
        with self.assertRaises(SettingsError):
            Settings.load(config_path=None, env={"SEINE_PORT": "eighty"})

    def test_validate_requires_token(self):
        with self.assertRaises(SettingsError):
            Settings().validate()
        with self.assertRaises(SettingsError):
            Settings(enrollment_token="").validate()
        Settings(enrollment_token="x").validate()

    def test_validate_tls_pair(self):
        with self.assertRaises(SettingsError):
            Settings(enrollment_token="x", tls_cert="c").validate()
        with self.assertRaises(SettingsError):
            Settings(enrollment_token="x", tls_key="k").validate()
        s = Settings(enrollment_token="x", tls_cert="c", tls_key="k")
        s.validate()
        self.assertTrue(s.tls_enabled)

    def test_loopback_detection(self):
        for host in ("127.0.0.1", "::1", "localhost"):
            self.assertTrue(is_loopback(host), host)
        for host in ("0.0.0.0", "192.168.1.5", "::", "example.org"):
            self.assertFalse(is_loopback(host), host)


STORAGE_YAML = """\
storage:
  endpoint: https://s3.lan:3900
  region: lan
  projects:
    alpha:
      dev: {access_key: GKalpha-dev, secret_key: alpha-dev-secret}
      prod: {access_key: GKalpha-prod, secret_key: alpha-prod-secret}
    beta:
      dev: {access_key: GKbeta-dev, secret_key: beta-dev-secret}
  default:
    dev: {access_key: GKdef-dev, secret_key: default-dev-secret}
"""


class StorageSettingsTest(Test):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="seine-test-storage-settings-")

    def tearDown(self):
        from seine import vault
        vault.clear_secrets()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _load(self, text, env=None, overrides=None):
        path = os.path.join(self.tmp_dir, "server.yaml")
        with open(path, "w") as f:
            f.write(text)
        return Settings.load(config_path=path, env=env or {}, overrides=overrides)

    def test_storage_section_is_parsed(self):
        s = self._load(STORAGE_YAML)
        self.assertEqual((s.s3_endpoint, s.s3_region), ("https://s3.lan:3900", "lan"))
        self.assertEqual(s.s3_keys("alpha", "dev"), {"access_key": "GKalpha-dev", "secret_key": "alpha-dev-secret"})
        self.assertEqual(s.s3_keys("alpha", "prod")["access_key"], "GKalpha-prod")

    def test_project_without_entry_uses_default_and_never_mixes(self):
        s = self._load(STORAGE_YAML)
        self.assertEqual(s.s3_keys("gamma", "dev")["access_key"], "GKdef-dev")
        self.assertIsNone(s.s3_keys("gamma", "prod"))
        self.assertIsNone(s.s3_keys("beta", "prod"))

    def test_no_default_means_no_credentials(self):
        s = self._load("storage:\n  endpoint: https://s3\n")
        self.assertIsNone(s.s3_keys("alpha", "dev"))

    def test_endpoint_and_region_overridable_keys_are_not(self):
        s = self._load(
            STORAGE_YAML,
            env={"SEINE_S3_ENDPOINT": "https://env", "SEINE_S3_REGION": "env-r"},
            overrides={"s3_region": "flag-r"},
        )
        self.assertEqual((s.s3_endpoint, s.s3_region), ("https://env", "flag-r"))
        s = Settings.load(config_path=None, env={"SEINE_S3_ACCESS_KEY": "x", "SEINE_S3_SECRET_KEY": "y"})
        self.assertIsNone(s.s3_keys("alpha", "dev"))

    def test_keys_must_come_in_pairs(self):
        for pair in ("{access_key: a}", "{secret_key: s}"):
            with self.assertRaises(SettingsError):
                self._load(f"storage:\n  default:\n    dev: {pair}\n")

    def test_unknown_names_are_rejected(self):
        for text in (
            "storage:\n  bucket: x\n",
            "storage:\n  default:\n    staging: {access_key: a, secret_key: s}\n",
            "storage:\n  default:\n    dev: {access_key: a, secret_key: s, token: t}\n",
            "storage:\n  projects:\n    p:\n      qa: {access_key: a, secret_key: s}\n",
            "storage: [1]\n",
        ):
            with self.assertRaises(SettingsError, msg=text):
                self._load(text)

    def test_keys_need_an_endpoint(self):
        s = self._load("storage:\n  default:\n    dev: {access_key: a, secret_key: s}\n")
        with self.assertRaises(SettingsError):
            Settings(**{**vars(s), "enrollment_token": "t"}).validate()
        s = self._load(STORAGE_YAML)
        Settings(**{**vars(s), "enrollment_token": "t"}).validate()

    def test_key_values_are_registered_as_secrets_and_kept_out_of_repr(self):
        from seine import vault
        vault.clear_secrets()
        s = self._load(STORAGE_YAML)
        for value in ("GKalpha-dev", "alpha-prod-secret", "default-dev-secret"):
            self.assertIn(value, vault.secrets())
            self.assertNotIn(value, repr(s))
