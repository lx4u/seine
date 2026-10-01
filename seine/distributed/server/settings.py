# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0
"""Configuration of seine-server: defaults, config file, environment, CLI flags."""

from __future__ import annotations

import dataclasses
import ipaddress
import os
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

import yaml

from seine import vault
from seine.distributed.server.db import validate_project_name

DEFAULT_CONFIG_PATH = "/etc/seine/server.yaml"
CONFIG_ENV = "SEINE_SERVER_CONFIG"
DEFAULT_MAX_UPLOAD_BYTES = 512 * 1024 * 1024

ENV_VARS = {
    "host": "SEINE_HOST",
    "port": "SEINE_PORT",
    "db_path": "SEINE_DB_PATH",
    "enrollment_token": "SEINE_ENROLLMENT_TOKEN",
    "max_upload_bytes": "SEINE_MAX_UPLOAD_BYTES",
    "tls_cert": "SEINE_TLS_CERT",
    "tls_key": "SEINE_TLS_KEY",
    "stale_after": "SEINE_STALE_AFTER",
    "reap_interval": "SEINE_REAP_INTERVAL",
    "native_grace": "SEINE_NATIVE_GRACE",
    "job_lost_grace": "SEINE_JOB_LOST_GRACE",
    "secret_ttl": "SEINE_SECRET_TTL",
    "new_user_project": "SEINE_NEW_USER_PROJECT",
    "s3_endpoint": "SEINE_S3_ENDPOINT",
    "s3_region": "SEINE_S3_REGION",
}

S3_ENVIRONMENTS = ("dev", "prod")
_S3_KEY_FIELDS = ("access_key", "secret_key")


class SettingsError(ValueError):
    """Raised when the server configuration is unusable."""


@dataclass
class Settings:
    """Runtime settings of seine-server."""

    host: str = "127.0.0.1"
    port: int = 8000
    db_path: str = "seine.db"
    enrollment_token: Optional[str] = None
    max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES
    tls_cert: Optional[str] = None
    tls_key: Optional[str] = None
    stale_after: float = 120.0
    reap_interval: float = 30.0
    native_grace: float = 30.0
    job_lost_grace: float = 90.0
    secret_ttl: float = 6 * 3600.0
    # "none", "auto" (a home project per user) or the name of a shared project.
    new_user_project: str = "none"
    s3_endpoint: Optional[str] = None
    s3_region: str = "garage"
    # {project: {env: {access_key, secret_key}}}; keys come from the file only.
    s3_projects: dict = field(default_factory=dict, repr=False)
    s3_default: dict = field(default_factory=dict, repr=False)
    config_path: Optional[str] = None

    def __post_init__(self) -> None:
        self.s3_projects = {p: _s3_envs(e, f"storage.projects.{p}") for p, e in self.s3_projects.items()}
        self.s3_default = _s3_envs(self.s3_default, "storage.default")
        for envs in (*self.s3_projects.values(), self.s3_default):
            for pair in envs.values():
                for value in pair.values():
                    vault.record_secret(value)

    def s3_keys(self, project: str, env: str) -> Optional[dict[str, str]]:
        """Return the project's key pair for env ('dev' or 'prod'), else the default's, else None."""
        envs = self.s3_projects.get(project, self.s3_default)
        return envs.get(env)

    def validate(self) -> None:
        """Raise SettingsError unless the server may start with these settings."""
        if not self.enrollment_token:
            raise SettingsError(
                "no enrollment token: set SEINE_ENROLLMENT_TOKEN, "
                "'enrollment_token' in the config file or --enrollment-token"
            )
        if bool(self.tls_cert) != bool(self.tls_key):
            raise SettingsError("--tls-cert and --tls-key must be given together")
        if self.native_grace < 0:
            raise SettingsError("native_grace must not be negative")
        if self.job_lost_grace <= 0:
            raise SettingsError("job_lost_grace must be greater than zero")
        if self.secret_ttl <= 0:
            raise SettingsError("secret_ttl must be greater than zero")
        if self.new_user_project not in ("none", "auto"):
            try:
                validate_project_name(self.new_user_project)
            except ValueError as e:
                raise SettingsError(f"new_user_project: {e}") from e
        keyed = self.s3_default or any(self.s3_projects.values())
        if keyed and not self.s3_endpoint:
            raise SettingsError("storage keys are configured but 'storage.endpoint' is not")

    @property
    def tls_enabled(self) -> bool:
        return bool(self.tls_cert and self.tls_key)

    @property
    def exposed_without_tls(self) -> bool:
        return not self.tls_enabled and not is_loopback(self.host)

    @classmethod
    def load(
        cls,
        config_path: Optional[str] = None,
        env: Optional[Mapping[str, str]] = None,
        overrides: Optional[Mapping[str, Any]] = None,
    ) -> "Settings":
        """Merge defaults < config file < environment < overrides (None values are skipped)."""
        env = os.environ if env is None else env
        explicit = config_path or env.get(CONFIG_ENV)
        path = explicit or DEFAULT_CONFIG_PATH
        values: dict[str, Any] = {}
        if explicit or os.path.exists(path):
            values.update(_read_config(path))
        for name, var in ENV_VARS.items():
            if env.get(var) not in (None, ""):
                values[name] = env[var]
        values.update({k: v for k, v in (overrides or {}).items() if v is not None})
        return cls._from_values(values, config_path=path if (explicit or os.path.exists(path)) else None)

    @classmethod
    def _from_values(cls, values: Mapping[str, Any], config_path: Optional[str]) -> "Settings":
        known = {f.name for f in dataclasses.fields(cls)} - {"config_path"}
        unknown = set(values) - known
        if unknown:
            raise SettingsError(f"unknown setting(s): {', '.join(sorted(unknown))}")
        casts = {
            "port": int,
            "max_upload_bytes": int,
            "stale_after": float,
            "reap_interval": float,
            "native_grace": float,
            "job_lost_grace": float,
            "secret_ttl": float,
        }
        out: dict[str, Any] = {}
        for name, value in values.items():
            if name in ("s3_projects", "s3_default"):
                out[name] = value
                continue
            try:
                out[name] = casts.get(name, str)(value)
            except (TypeError, ValueError):
                raise SettingsError(f"invalid value for {name}: {value!r}")
        return cls(config_path=config_path, **out)


def is_loopback(host: str) -> bool:
    """Return True if host is localhost or a loopback address."""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _s3_envs(value: Any, where: str) -> dict[str, dict[str, str]]:
    """Validate {env: {access_key, secret_key}}; an env with no keys is left out."""
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise SettingsError(f"{where}: expected a mapping of dev/prod")
    unknown = set(value) - set(S3_ENVIRONMENTS)
    if unknown:
        raise SettingsError(f"{where}: unknown environment(s): {', '.join(sorted(map(str, unknown)))}")
    out = {}
    for env, pair in value.items():
        pair = pair or {}
        if not isinstance(pair, Mapping):
            raise SettingsError(f"{where}.{env}: expected access_key and secret_key")
        unknown = set(pair) - set(_S3_KEY_FIELDS)
        if unknown:
            raise SettingsError(f"{where}.{env}: unknown key(s): {', '.join(sorted(map(str, unknown)))}")
        present = [k for k in _S3_KEY_FIELDS if pair.get(k)]
        if not present:
            continue
        if len(present) != len(_S3_KEY_FIELDS):
            raise SettingsError(f"{where}.{env}: access_key and secret_key must be given together")
        if not all(isinstance(pair[k], str) for k in _S3_KEY_FIELDS):
            raise SettingsError(f"{where}.{env}: keys must be strings")
        out[env] = {k: pair[k] for k in _S3_KEY_FIELDS}
    return out


def _read_storage(section: Any) -> dict[str, Any]:
    """Flatten the 'storage:' section of the config file into settings values."""
    if not isinstance(section, Mapping):
        raise SettingsError("invalid 'storage' section: expected a mapping")
    unknown = set(section) - {"endpoint", "region", "projects", "default"}
    if unknown:
        raise SettingsError(f"unknown storage setting(s): {', '.join(sorted(map(str, unknown)))}")
    projects = section.get("projects") or {}
    if not isinstance(projects, Mapping):
        raise SettingsError("storage.projects: expected a mapping of project names")
    values: dict[str, Any] = {"s3_projects": dict(projects), "s3_default": section.get("default")}
    for name in ("endpoint", "region"):
        if section.get(name):
            values[f"s3_{name}"] = section[name]
    return values


def _read_config(path: str) -> dict[str, Any]:
    """Read a server config file; keys are flat or under 'server:'."""
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except OSError as e:
        raise SettingsError(f"cannot read config {path}: {e}")
    except yaml.YAMLError as e:
        raise SettingsError(f"invalid config {path}: {e}")
    if not isinstance(data, dict):
        raise SettingsError(f"invalid config {path}: expected a mapping")
    section = data.pop("server", None) or {}
    database = data.pop("database", None) or section.pop("database", None)
    storage = data.pop("storage", None) or section.pop("storage", None)
    values = {**data, **section}
    if storage is not None:
        values.update(_read_storage(storage))
    if database:
        if database.get("type", "sqlite") != "sqlite":
            raise SettingsError(f"unsupported database type: {database['type']}")
        if "path" in database:
            values["db_path"] = database["path"]
    return values
