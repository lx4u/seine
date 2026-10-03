# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0
"""Configuration of seine-server: defaults, config file, environment, CLI flags."""

from __future__ import annotations

import dataclasses
import ipaddress
import os
import re
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


_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_SIZE_UNITS = {"M": 1024**2, "G": 1024**3, "T": 1024**4}
_DURATION_RE = re.compile(r"(\d+)([smhd])")
_THRESHOLD_RE = re.compile(r"(\d+(?:\.\d+)?)(%|[MGT])")


@dataclass(frozen=True)
class Threshold:
    """A storage level: a percentage of the project quota, or a size in bytes."""

    value: float
    percent: bool

    @property
    def bytes(self) -> Optional[int]:
        """The size in bytes, or None for a percentage."""
        return None if self.percent else int(self.value)


@dataclass(frozen=True)
class EnvRetention:
    """Retention of one environment; durations are seconds, None means never."""

    worktrees: Optional[float]
    artifacts: Optional[float]
    cache: Optional[float]
    high_water: Optional[Threshold]
    low_water: Optional[Threshold]
    min_age: float


@dataclass(frozen=True)
class Retention:
    """The 'retention:' section: sweep interval (seconds) and per-environment rules."""

    interval: float
    dev: EnvRetention
    prod: EnvRetention


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
    # None keeps today's behaviour: nothing is ever deleted.
    retention: Optional[Retention] = None
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
            if name in ("s3_projects", "s3_default", "retention"):
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


def _duration(value: Any, where: str, allow_never: bool = True) -> Optional[float]:
    """Parse '<n>s|m|h|d' into seconds; 'never' gives None."""
    if allow_never and value == "never":
        return None
    match = _DURATION_RE.fullmatch(value.strip()) if isinstance(value, str) else None
    if not match or int(match[1]) == 0:
        never = " or 'never'" if allow_never else ""
        raise SettingsError(f"{where}: invalid duration {value!r}, expected <n>s, <n>m, <n>h or <n>d{never}")
    return float(int(match[1]) * _DURATION_UNITS[match[2]])


def _threshold(value: Any, where: str) -> Threshold:
    """Parse '<n>%' (above 0, up to 100) or '<n>M|G|T' (binary units)."""
    match = _THRESHOLD_RE.fullmatch(value.strip()) if isinstance(value, str) else None
    number = float(match[1]) if match else 0.0
    if not match or number <= 0 or (match[2] == "%" and number > 100):
        raise SettingsError(f"{where}: invalid threshold {value!r}, expected <n>% (1 to 100) or <n>M, <n>G, <n>T")
    if match[2] == "%":
        return Threshold(number, True)
    return Threshold(number * _SIZE_UNITS[match[2]], False)


_ENV_DEFAULTS = {
    "dev": {
        "worktrees": "3d",
        "artifacts": "14d",
        "cache": "30d",
        "high_water": "80%",
        "low_water": "60%",
        "min_age": "1h",
    },
    "prod": {
        "worktrees": "14d",
        "artifacts": "never",
        "cache": "90d",
        "high_water": "85%",
        "low_water": None,
        "min_age": "1h",
    },
}


def _env_retention(env: str, section: Any) -> EnvRetention:
    where = f"retention.{env}"
    if section is None:
        section = {}
    if not isinstance(section, Mapping):
        raise SettingsError(f"{where}: expected a mapping")
    unknown = set(section) - set(_ENV_DEFAULTS[env])
    if unknown:
        raise SettingsError(f"{where}: unknown key(s): {', '.join(sorted(map(str, unknown)))}")
    raw = {**_ENV_DEFAULTS[env], **section}
    out: dict[str, Any] = {}
    for key in ("worktrees", "artifacts", "cache"):
        out[key] = _duration(raw[key], f"{where}.{key}")
    out["min_age"] = _duration(raw["min_age"], f"{where}.min_age", allow_never=False)
    for key in ("high_water", "low_water"):
        out[key] = None if raw[key] is None else _threshold(raw[key], f"{where}.{key}")
    high, low = out["high_water"], out["low_water"]
    if high and low:
        if high.percent != low.percent:
            raise SettingsError(f"{where}: high_water and low_water must both be percentages or both sizes")
        if low.value >= high.value:
            raise SettingsError(f"{where}: low_water must be below high_water")
    return EnvRetention(**out)


def _read_retention(section: Any) -> Retention:
    """Validate the 'retention:' section of the config file."""
    if not isinstance(section, Mapping):
        raise SettingsError("invalid 'retention' section: expected a mapping")
    unknown = set(section) - {"interval", *S3_ENVIRONMENTS}
    if unknown:
        raise SettingsError(f"unknown retention setting(s): {', '.join(sorted(map(str, unknown)))}")
    interval = section.get("interval", 3600)
    if isinstance(interval, str):
        interval = _duration(interval, "retention.interval", allow_never=False)
    elif isinstance(interval, bool) or not isinstance(interval, (int, float)) or interval <= 0:
        raise SettingsError(f"retention.interval: invalid value {interval!r}, expected seconds above zero")
    envs = {env: _env_retention(env, section.get(env)) for env in S3_ENVIRONMENTS}
    return Retention(interval=float(interval), **envs)


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
    has_retention = "retention" in data or "retention" in section
    retention = data.pop("retention", None) or section.pop("retention", None) or {}
    values = {**data, **section}
    if has_retention:
        values["retention"] = _read_retention(retention)
    if storage is not None:
        values.update(_read_storage(storage))
    if database:
        if database.get("type", "sqlite") != "sqlite":
            raise SettingsError(f"unsupported database type: {database['type']}")
        if "path" in database:
            values["db_path"] = database["path"]
    return values
