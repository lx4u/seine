# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0
"""Tests for seine-server settings loading and validation."""

import os
import shutil
import tempfile

from avocado import Test

from seine.distributed.server.settings import EnvRetention, Settings, SettingsError, Threshold, is_loopback


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


class RetentionSettingsTest(Test):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="seine-test-retention-settings-")

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _load(self, text):
        path = os.path.join(self.tmp_dir, "server.yaml")
        with open(path, "w") as f:
            f.write(text)
        return Settings.load(config_path=path, env={})

    def _dev(self, **keys):
        body = "".join(f"    {k}: {v}\n" for k, v in keys.items())
        return self._load(f"retention:\n  dev:\n{body}").retention.dev

    def test_absent_section_means_no_retention(self):
        self.assertIsNone(Settings.load(config_path=None, env={}).retention)
        self.assertIsNone(self._load("port: 9000\n").retention)

    def test_full_section(self):
        r = self._load(
            """
retention:
  interval: 600
  dev:
    worktrees: 2d
    artifacts: 7d
    cache: 12h
    high_water: 500G
    low_water: 400G
    min_age: 30m
  prod:
    worktrees: 20d
    artifacts: 365d
    cache: never
    high_water: 90%
"""
        ).retention
        self.assertEqual(r.interval, 600.0)
        self.assertEqual(
            r.dev,
            EnvRetention(
                worktrees=2 * 86400.0,
                artifacts=7 * 86400.0,
                cache=12 * 3600.0,
                high_water=Threshold(500 * 1024**3, False),
                low_water=Threshold(400 * 1024**3, False),
                min_age=1800.0,
            ),
        )
        self.assertEqual(r.prod.cache, None)
        self.assertEqual(r.prod.high_water, Threshold(90, True))

    def test_defaults_for_omitted_keys(self):
        for text in ("retention: {}\n", "retention:\n", "retention:\n  dev:\n  prod: {}\n"):
            r = self._load(text).retention
            self.assertEqual(r.interval, 3600.0)
            self.assertEqual(
                r.dev,
                EnvRetention(3 * 86400.0, 14 * 86400.0, 30 * 86400.0, Threshold(80, True), Threshold(60, True), 3600.0),
            )
            self.assertEqual(
                r.prod,
                EnvRetention(14 * 86400.0, None, 90 * 86400.0, Threshold(85, True), None, 3600.0),
            )

    def test_section_under_server_key(self):
        r = self._load("server:\n  retention:\n    interval: 60\n").retention
        self.assertEqual(r.interval, 60.0)

    def test_duration_forms(self):
        for text, seconds in (("45s", 45), ("10m", 600), ("2h", 7200), ("3d", 259200), ("never", None)):
            self.assertEqual(self._dev(artifacts=text).artifacts, seconds, text)
        self.assertEqual(self._dev(min_age="90s").min_age, 90.0)

    def test_bad_durations(self):
        for text in ("0d", "-1d", "3w", "3", "d", "1.5h", "soon", "''", "[1]"):
            with self.assertRaises(SettingsError, msg=text):
                self._dev(worktrees=text)
        with self.assertRaises(SettingsError):
            self._dev(min_age="never")

    def test_interval_forms(self):
        for text, seconds in (("30", 30.0), ("0.5", 0.5), ("'2m'", 120.0)):
            r = self._load(f"retention:\n  interval: {text}\n").retention
            self.assertEqual(r.interval, seconds, text)
        for text in ("0", "-5", "never", "x", "true", "[1]"):
            with self.assertRaises(SettingsError, msg=text):
                self._load(f"retention:\n  interval: {text}\n")

    def test_threshold_forms(self):
        self.assertEqual(self._dev(high_water="100%").high_water, Threshold(100, True))
        self.assertEqual(self._dev(high_water="72.5%").high_water, Threshold(72.5, True))
        self.assertEqual(self._dev(high_water="2T", low_water="1G").high_water.bytes, 2 * 1024**4)
        self.assertEqual(self._dev(high_water="512M", low_water="1M").high_water.bytes, 512 * 1024**2)
        self.assertEqual(self._dev(high_water="2G", low_water="1G").low_water.bytes, 1024**3)
        self.assertIsNone(self._dev(high_water="50%", low_water="40%").high_water.bytes)

    def test_bad_thresholds(self):
        for text in ("0%", "101%", "-5%", "10", "10K", "G", "ten%", "0G", "''"):
            with self.assertRaises(SettingsError, msg=text):
                self._dev(high_water=text)

    def test_low_water_must_be_below_high_water(self):
        for keys in (
            {"high_water": "50%", "low_water": "50%"},
            {"high_water": "50%", "low_water": "70%"},
            {"high_water": "1G", "low_water": "1024M"},
            {"high_water": "1G", "low_water": "2G"},
        ):
            with self.assertRaises(SettingsError, msg=str(keys)):
                self._dev(**keys)
        with self.assertRaises(SettingsError):
            self._dev(low_water="90%")

    def test_percent_and_size_cannot_be_mixed(self):
        with self.assertRaises(SettingsError) as ctx:
            self._dev(high_water="500G", low_water="60%")
        self.assertIn("both be percentages or both sizes", str(ctx.exception))

    def test_prod_needs_no_low_water(self):
        r = self._load("retention:\n  prod:\n    high_water: 1T\n").retention
        self.assertIsNone(r.prod.low_water)
        self.assertEqual(r.prod.high_water.bytes, 1024**4)

    def test_unknown_keys_are_rejected(self):
        for text in (
            "retention:\n  sweep: 1\n",
            "retention:\n  staging: {}\n",
            "retention:\n  dev:\n    ttl: 1d\n",
            "retention:\n  prod:\n    interval: 5\n",
            "retention: [1]\n",
            "retention: 5\n",
            "retention:\n  dev: [1]\n",
        ):
            with self.assertRaises(SettingsError, msg=text):
                self._load(text)

    def test_unknown_key_is_named(self):
        with self.assertRaises(SettingsError) as ctx:
            self._load("retention:\n  dev:\n    ttl: 1d\n")
        self.assertIn("ttl", str(ctx.exception))


ARTIFACTORY_YAML = """\
storage:
  type: artifactory
  artifactory_endpoint: https://arti.lan:8081
  artifactory_projects:
    alpha:
      dev: {token: alpha-dev-token}
      prod: {user: alpha-prod, password: alpha-prod-secret}
  artifactory_default:
    dev: {token: default-dev-token}
"""


class ArtifactorySettingsTest(Test):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="seine-test-arti-settings-")

    def tearDown(self):
        from seine import vault
        vault.clear_secrets()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _load(self, text, env=None, overrides=None):
        path = os.path.join(self.tmp_dir, "server.yaml")
        with open(path, "w") as f:
            f.write(text)
        return Settings.load(config_path=path, env=env or {}, overrides=overrides)

    def test_artifactory_section_is_parsed(self):
        s = self._load(ARTIFACTORY_YAML)
        self.assertEqual(s.storage_type, "artifactory")
        self.assertEqual(s.artifactory_endpoint, "https://arti.lan:8081")
        self.assertEqual(s.artifactory_keys("alpha", "dev"), {"token": "alpha-dev-token"})
        self.assertEqual(s.artifactory_keys("alpha", "prod"),
                         {"user": "alpha-prod", "password": "alpha-prod-secret"})

    def test_project_without_entry_uses_default(self):
        s = self._load(ARTIFACTORY_YAML)
        self.assertEqual(s.artifactory_keys("gamma", "dev"), {"token": "default-dev-token"})
        self.assertIsNone(s.artifactory_keys("gamma", "prod"))

    def test_storage_type_defaults_to_s3(self):
        s = self._load("storage:\n  endpoint: https://s3\n")
        self.assertEqual(s.storage_type, "s3")

    def test_unknown_storage_type_is_rejected(self):
        s = self._load("storage:\n  type: ftp\n")
        with self.assertRaises(SettingsError):
            Settings(**{**vars(s), "enrollment_token": "t"}).validate()

    def test_artifactory_keys_need_an_endpoint(self):
        s = self._load("storage:\n  artifactory_default:\n    dev: {token: t}\n")
        with self.assertRaises(SettingsError):
            Settings(**{**vars(s), "enrollment_token": "t"}).validate()

    def test_artifactory_key_shapes_are_validated(self):
        for text in (
            "storage:\n  artifactory_default:\n    dev: {user: u}\n",
            "storage:\n  artifactory_default:\n    dev: {password: p}\n",
            "storage:\n  artifactory_default:\n    dev: {token: t, user: u}\n",
            "storage:\n  artifactory_default:\n    staging: {token: t}\n",
        ):
            with self.assertRaises(SettingsError, msg=text):
                self._load(text)

    def test_artifactory_endpoint_from_env(self):
        s = self._load("storage:\n  type: artifactory\n",
                       env={"SEINE_ARTIFACTORY_ENDPOINT": "https://env:8081"})
        self.assertEqual(s.artifactory_endpoint, "https://env:8081")
