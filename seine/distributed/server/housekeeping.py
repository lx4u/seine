# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0
"""Delete expired worktrees and expired or over-quota dev artifacts per the 'retention:' settings."""

from __future__ import annotations

import logging
import math
import threading
import time
from datetime import datetime
from dataclasses import dataclass, field
from typing import Any, Optional

from seine.distributed.server.db import Database
from seine.distributed.server.settings import EnvRetention, Settings, Threshold
from seine.distributed.server.storage import StorageCredentialsError, provider_for
from seine.storage.artifactory.client import ArtifactoryError
from seine.storage.s3.client import S3ClientError

# Either network backend may fail a sweep the same way.
_STORAGE_ERRORS = (S3ClientError, ArtifactoryError)

logger = logging.getLogger("seine.server.housekeeping")

_lock = threading.Lock()


class HousekeepingBusy(RuntimeError):
    """Another housekeeping run is in progress."""


class UnknownProject(ValueError):
    """The project to sweep does not exist."""


@dataclass
class ProjectReport:
    """What one run did (or, in a dry run, would do) for one project."""

    project: str
    dry_run: bool = False
    # (build id, reason, bytes) per evicted build
    evicted: list[tuple[str, str, int]] = field(default_factory=list)
    # (build id, error) per build whose objects could not be deleted
    failures: list[tuple[str, str]] = field(default_factory=list)
    # (digest, bytes) per expired worktree, dev and prod buckets together
    worktrees: list[tuple[str, int]] = field(default_factory=list)
    # one message per bucket whose lifecycle rules could not be installed
    lifecycle: list[str] = field(default_factory=list)
    usage_before: Optional[int] = None
    usage_after: Optional[int] = None
    high_water_bytes: Optional[int] = None
    low_water_bytes: Optional[int] = None
    skipped_reason: Optional[str] = None


def _to_bytes(threshold: Optional[Threshold], quota_gb: Optional[float]) -> Optional[int]:
    """Resolve a threshold to bytes; None when it is unset or needs a missing quota."""
    if threshold is None:
        return None
    if not threshold.percent:
        return threshold.bytes
    if not quota_gb:
        return None
    return int(threshold.value / 100 * quota_gb * 1024**3)


def _meta_bytes(build: dict[str, Any]) -> int:
    return sum(int(a.get("size") or 0) for a in build["artifact_meta"])


def run_housekeeping(
    db: Database,
    settings: Settings,
    project: Optional[str] = None,
    dry_run: bool = False,
    now: Optional[float] = None,
) -> list[ProjectReport]:
    """Evict dev artifacts of every project (or just `project`); raise HousekeepingBusy if running."""
    if settings.retention is None:
        return []
    if not _lock.acquire(blocking=False):
        raise HousekeepingBusy("housekeeping is already running")
    try:
        now = time.time() if now is None else now
        projects = db.projects.list()
        if project is not None:
            projects = [p for p in projects if p["name"] == project]
            if not projects:
                raise UnknownProject(f"unknown project: {project}")
        reports = []
        for row in projects:
            report = ProjectReport(project=row["name"], dry_run=dry_run)
            reports.append(report)
            try:
                report.usage_before = _dev_usage(settings, row)
                _housekeep_worktrees(db, settings, row, report, now)
                _prod_check(settings, row, report)
                _housekeep_dev(db, settings, row, report, now)
            except _STORAGE_ERRORS as e:
                report.skipped_reason = f"storage error: {e}"
                logger.error("Housekeeping of %s failed: %s", row["name"], e)
            _log_summary(report)
        return reports
    finally:
        _lock.release()


_ABORT_RULE = "seine-abort-multipart"
_WORKTREES_RULE = "seine-worktrees-expiry"
_CACHE_RULE = "seine-cache-expiry"


def _lifecycle_rules(
    worktrees_ttl: Optional[float], cache_ttl: Optional[float], cache_prefix: str
) -> list[dict[str, Any]]:
    """The rules seine owns on a bucket; whole days, at least one."""
    rules: list[dict[str, Any]] = [{
        "ID": _ABORT_RULE, "Status": "Enabled", "Filter": {"Prefix": ""},
        "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1},
    }]
    if worktrees_ttl is not None:
        rules.append({
            "ID": _WORKTREES_RULE, "Status": "Enabled", "Filter": {"Prefix": "worktrees/"},
            "Expiration": {"Days": max(math.ceil(worktrees_ttl / 86400), 1)},
        })
    if cache_ttl is not None:
        rules.append({
            "ID": _CACHE_RULE, "Status": "Enabled", "Filter": {"Prefix": f"{cache_prefix}/"},
            "Expiration": {"Days": max(math.ceil(cache_ttl / 86400), 1)},
        })
    return rules


def _rule_key(rule: dict[str, Any]) -> tuple:
    """The parts of a rule seine sets, so a reply from the server compares equal."""
    prefix = rule.get("Filter", {}).get("Prefix", rule.get("Prefix", ""))
    return (
        rule.get("ID"), rule.get("Status", "Enabled"), prefix,
        rule.get("Expiration", {}).get("Days"),
        rule.get("AbortIncompleteMultipartUpload", {}).get("DaysAfterInitiation"),
    )


def merge_lifecycle(
    existing: list[dict[str, Any]],
    worktrees_ttl: Optional[float],
    cache_ttl: Optional[float] = None,
    cache_prefix: str = "cache",
) -> Optional[list[dict[str, Any]]]:
    """Return the rules to put, or None when the bucket already has seine's rules."""
    own = (_ABORT_RULE, _WORKTREES_RULE, _CACHE_RULE)
    mine = _lifecycle_rules(worktrees_ttl, cache_ttl, cache_prefix)
    current = [r for r in existing if r.get("ID") in own]
    if sorted(map(_rule_key, current)) == sorted(map(_rule_key, mine)):
        return None
    return [r for r in existing if r.get("ID") not in own] + mine


def _age_of(obj: dict[str, Any]) -> Optional[float]:
    """Epoch seconds of an object's last_modified; None when it has none."""
    stamp = obj.get("last_modified")
    return datetime.fromisoformat(stamp).timestamp() if stamp else None


def _housekeep_worktrees(
    db: Database, settings: Settings, row: dict[str, Any], report: ProjectReport, now: float
) -> None:
    """Install the lifecycle rules and sweep aged worktrees in the dev and prod buckets."""
    name = row["name"]
    for env_name in ("dev", "prod"):
        bucket = row.get(f"{env_name}_bucket")
        if not bucket:
            continue
        retention = getattr(settings.retention, env_name)
        ttl = retention.worktrees
        try:
            provider = provider_for(settings, name, bucket, env_name)
        except StorageCredentialsError as e:
            logger.debug("No %s storage for %s: %s", env_name, name, e)
            continue
        # Backends without server-side expiry (Artifactory) rely on the
        # explicit sweep below; there are no rules to install for them.
        if not report.dry_run and getattr(provider, "supports_lifecycle", True):
            try:
                rules = merge_lifecycle(provider.lifecycle_rules(), ttl, retention.cache, provider.prefix)
                if rules is not None:
                    provider.set_lifecycle_rules(rules)
                    logger.info("Installed lifecycle rules on the %s bucket of %s", env_name, name)
            except _STORAGE_ERRORS as e:
                logger.warning("Lifecycle rules of the %s bucket of %s not installed: %s", env_name, name, e)
                report.lifecycle.append(f"{env_name}: {e}")
        if ttl is None:
            continue
        try:
            _sweep_worktrees(db, provider, name, ttl, report, now)
        except _STORAGE_ERRORS as e:
            logger.error("Listing worktrees of the %s bucket of %s failed: %s", env_name, name, e)
            report.failures.append((f"worktrees ({env_name})", str(e)))


def _sweep_worktrees(
    db: Database, provider: Any, project: str, ttl: float, report: ProjectReport, now: float
) -> None:
    """Delete worktrees older than ttl that no unfinished build of the project uses."""
    active = db.builds.active_worktree_digests(project)
    prefix = f"worktrees/{project}/"
    for obj in provider.list_objects(prefix):
        age = _age_of(obj)
        digest = obj["key"][len(prefix):].removesuffix(".tar.zst")
        if age is None or age >= now - ttl or digest in active:
            continue
        if not report.dry_run:
            try:
                provider.delete_prefix(obj["key"])
            except _STORAGE_ERRORS as e:
                logger.error("Deleting worktree %s of %s failed: %s", digest, project, e)
                report.failures.append((f"worktree {digest}", str(e)))
                continue
        report.worktrees.append((digest, obj["size"]))
        logger.info(
            "Expired worktree %s of %s: %d bytes%s",
            digest, project, obj["size"], " (dry run)" if report.dry_run else "",
        )


def _prod_check(settings: Settings, row: dict[str, Any], report: ProjectReport) -> None:
    """Warn when the prod bucket is above high_water; prod is never evicted."""
    high = _to_bytes(settings.retention.prod.high_water, row.get("quota_gb"))
    if high is None:
        return
    try:
        provider = provider_for(settings, row["name"], row["prod_bucket"], "prod")
        used = provider.usage()
    except (StorageCredentialsError,) + _STORAGE_ERRORS as e:
        logger.debug("No prod usage for %s: %s", row["name"], e)
        return
    if used >= high:
        logger.warning(
            "Prod bucket of %s holds %d bytes, above high_water (%d); nothing is evicted",
            row["name"], used, high,
        )


class _DevPass:
    """Evict from one project's dev bucket, tracking the bucket usage as it goes."""

    def __init__(self, db: Database, provider: Any, row: dict[str, Any], report: ProjectReport, now: float):
        self.db, self.provider, self.row, self.report, self.now = db, provider, row, report, now
        self.name = row["name"]
        self.usage = 0
        self.seen: set[str] = set()

    def evict(self, build: dict[str, Any], reason: str) -> None:
        build_id = build["id"]
        self.seen.add(build_id)
        if self.report.dry_run:
            freed = _meta_bytes(build)
        else:
            try:
                _, freed = self.provider.delete_prefix(f"artifacts/{self.name}/{build_id}/")
            except _STORAGE_ERRORS as e:
                logger.error("Deleting artifacts of build %s (%s) failed: %s", build_id, self.name, e)
                self.report.failures.append((build_id, str(e)))
                return
            self.db.builds.mark_artifacts_expired(build_id, reason, now=self.now)
        self.usage = max(self.usage - freed, 0)
        self.report.evicted.append((build_id, reason, freed))
        logger.info(
            "Evicted artifacts of build %s (%s): %d bytes, %s%s",
            build_id, self.name, freed, reason, " (dry run)" if self.report.dry_run else "",
        )

    def expire_old(self, ttl: float) -> None:
        for build in self.db.builds.evictable_builds(self.name, self.now - ttl):
            self.evict(build, "ttl")
        if self.report.evicted and not self.report.dry_run:
            self.usage = self.provider.usage()

    def relieve_pressure(self, rules: EnvRetention) -> Optional[str]:
        """Evict oldest builds from high_water down to low_water; return a skip reason or None."""
        quota = self.row.get("quota_gb")
        high = _to_bytes(rules.high_water, quota)
        low = _to_bytes(rules.low_water, quota)
        if rules.high_water is not None and high is None:
            logger.warning(
                "Project %s: high_water is a percentage but it has no quota; no pressure eviction",
                self.name,
            )
            return "percent threshold without a project quota"
        if high is None:
            return None
        low = high if low is None else low
        self.report.high_water_bytes, self.report.low_water_bytes = high, low
        if self.usage < high:
            return None
        for build in self.db.builds.evictable_builds(self.name, self.now - rules.min_age):
            if self.usage <= low:
                break
            if build["id"] not in self.seen:
                self.evict(build, "pressure")
        if self.usage >= high:
            logger.error(
                "Project %s: dev bucket still holds %d bytes (high_water %d) and nothing is left to evict",
                self.name, self.usage, high,
            )
        return None


def _dev_usage(settings: Settings, row: dict[str, Any]) -> Optional[int]:
    """Bytes held by the dev bucket, None when it has no storage configured."""
    try:
        return provider_for(settings, row["name"], row["dev_bucket"], "dev").usage()
    except StorageCredentialsError:
        return None


def _housekeep_dev(
    db: Database, settings: Settings, row: dict[str, Any], report: ProjectReport, now: float
) -> None:
    rules = settings.retention.dev
    try:
        provider = provider_for(settings, row["name"], row["dev_bucket"], "dev")
    except StorageCredentialsError as e:
        report.skipped_reason = "no dev storage configured"
        logger.debug("Skipping %s: %s", row["name"], e)
        return
    run = _DevPass(db, provider, row, report, now)
    run.usage = provider.usage()
    if rules.artifacts is not None:
        run.expire_old(rules.artifacts)
    report.skipped_reason = run.relieve_pressure(rules)
    report.usage_after = run.usage


def _log_summary(report: ProjectReport) -> None:
    logger.info(
        "Housekeeping %s: %d evicted, %d worktrees expired, %d failed, usage %s -> %s%s",
        report.project, len(report.evicted), len(report.worktrees), len(report.failures),
        report.usage_before, report.usage_after,
        f", skipped: {report.skipped_reason}" if report.skipped_reason else "",
    )
