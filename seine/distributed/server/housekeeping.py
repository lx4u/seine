# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0
"""Delete expired and over-quota dev artifacts according to the 'retention:' settings."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from seine.distributed.server.db import Database
from seine.distributed.server.settings import EnvRetention, Settings, Threshold
from seine.distributed.server.storage import StorageCredentialsError, provider_for
from seine.storage.s3.client import S3ClientError

logger = logging.getLogger("seine.server.housekeeping")

_lock = threading.Lock()


class HousekeepingBusy(RuntimeError):
    """Another housekeeping run is in progress."""


@dataclass
class ProjectReport:
    """What one run did (or, in a dry run, would do) for one project."""

    project: str
    dry_run: bool = False
    # (build id, reason, bytes) per evicted build
    evicted: list[tuple[str, str, int]] = field(default_factory=list)
    # (build id, error) per build whose objects could not be deleted
    failures: list[tuple[str, str]] = field(default_factory=list)
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
                raise ValueError(f"unknown project: {project}")
        reports = []
        for row in projects:
            report = ProjectReport(project=row["name"], dry_run=dry_run)
            reports.append(report)
            try:
                _prod_check(settings, row, report)
                _housekeep_dev(db, settings, row, report, now)
            except S3ClientError as e:
                report.skipped_reason = f"storage error: {e}"
                logger.error("Housekeeping of %s failed: %s", row["name"], e)
            _log_summary(report)
        return reports
    finally:
        _lock.release()


def _prod_check(settings: Settings, row: dict[str, Any], report: ProjectReport) -> None:
    """Warn when the prod bucket is above high_water; prod is never evicted."""
    high = _to_bytes(settings.retention.prod.high_water, row.get("quota_gb"))
    if high is None:
        return
    try:
        provider = provider_for(settings, row["name"], row["prod_bucket"], "prod")
        used = provider.usage()
    except (StorageCredentialsError, S3ClientError) as e:
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
            except S3ClientError as e:
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
    report.usage_before = run.usage = provider.usage()
    if rules.artifacts is not None:
        run.expire_old(rules.artifacts)
    report.skipped_reason = run.relieve_pressure(rules)
    report.usage_after = run.usage


def _log_summary(report: ProjectReport) -> None:
    logger.info(
        "Housekeeping %s: %d evicted, %d failed, usage %s -> %s%s",
        report.project, len(report.evicted), len(report.failures),
        report.usage_before, report.usage_after,
        f", skipped: {report.skipped_reason}" if report.skipped_reason else "",
    )
