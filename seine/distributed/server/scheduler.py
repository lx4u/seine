# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0
"""Capability-aware build scheduler for distributed workers."""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable, Optional, Union

NATIVE_ARCH_SCORE = 1.0
CROSS_ARCH_SCORE = 0.7
EMULATION_ARCH_SCORE = 0.3
MAX_JOB_ATTEMPTS = 3
DEFAULT_NATIVE_GRACE = 30.0
DEFAULT_STALE_AFTER = 120.0
DEFAULT_JOB_LOST_GRACE = 90.0

logger = logging.getLogger("seine.server.scheduler")


def evaluate_arch_score(worker: Any, target_arch: str) -> float:
    """Evaluate worker architecture suitability score for a target architecture."""
    if hasattr(worker, "native_arch"):
        native_arch = worker.native_arch
        arch_scores = worker.arch_scores or {}
    elif isinstance(worker, dict):
        native_arch = worker.get("native_arch", "")
        arch_scores = worker.get("arch_scores") or {}
        if isinstance(arch_scores, str):
            try:
                arch_scores = json.loads(arch_scores)
            except Exception:
                arch_scores = {}
    else:
        return 0.0

    if target_arch in arch_scores:
        return float(arch_scores[target_arch])
    if target_arch == native_arch:
        return NATIVE_ARCH_SCORE
    return 0.0


def check_arch_constraints(
    score: float,
    options: Optional[dict[str, Any]] = None,
) -> bool:
    """Check if architecture score satisfies build constraints."""
    if score <= 0.0:
        return False
    if not options:
        return True

    require_native = options.get("require_native") or options.get("--require-native", False)
    if require_native and score < NATIVE_ARCH_SCORE:
        return False

    min_score = options.get("min_arch_score")
    if min_score is None:
        min_score = options.get("--min-arch-score")
    if min_score is not None:
        try:
            if score < float(min_score):
                return False
        except (ValueError, TypeError):
            pass

    return True


def _worker_id(worker: Any) -> str:
    if hasattr(worker, "id"):
        return worker.id
    if isinstance(worker, dict):
        return worker.get("id", "")
    return ""


def _worker_slots(worker: Any) -> int:
    if hasattr(worker, "concurrency_slots"):
        return int(worker.concurrency_slots)
    if isinstance(worker, dict):
        return int(worker.get("concurrency_slots", 1))
    return 1


def _worker_free_disk(worker: Any) -> float:
    if hasattr(worker, "free_disk_gb"):
        return float(worker.free_disk_gb)
    if isinstance(worker, dict):
        return float(worker.get("free_disk_gb", 0.0))
    return 0.0


def _worker_status(worker: Any) -> str:
    if hasattr(worker, "status"):
        return str(worker.status)
    if isinstance(worker, dict):
        return str(worker.get("status", "online"))
    return "online"


def is_worker_eligible(
    worker: Any,
    target_arch: str,
    options: Optional[dict[str, Any]] = None,
    active_jobs: int = 0,
) -> bool:
    """Check if worker is online, has capacity, and satisfies architecture constraints."""
    if _worker_status(worker) != "online":
        return False
    slots = _worker_slots(worker)
    if active_jobs >= slots:
        return False
    score = evaluate_arch_score(worker, target_arch)
    return check_arch_constraints(score, options)


def rank_candidate_workers(
    workers: list[Any],
    target_arch: str,
    options: Optional[dict[str, Any]] = None,
    active_job_counts: Optional[dict[str, int]] = None,
) -> list[Any]:
    """Filter and rank candidate workers for a job."""
    counts = active_job_counts or {}
    eligible: list[tuple[float, int, float, str, Any]] = []

    for w in workers:
        w_id = _worker_id(w)
        active = counts.get(w_id, 0)
        if not is_worker_eligible(w, target_arch, options=options, active_jobs=active):
            continue

        score = evaluate_arch_score(w, target_arch)
        slots = _worker_slots(w)
        free_disk = _worker_free_disk(w)
        available_slots = slots - active
        eligible.append((score, available_slots, free_disk, w_id, w))

    eligible.sort(key=lambda item: (-item[0], -item[1], -item[2], item[3]))
    return [item[4] for item in eligible]


def select_best_worker(
    workers: list[Any],
    target_arch: str,
    options: Optional[dict[str, Any]] = None,
    active_job_counts: Optional[dict[str, int]] = None,
) -> Optional[Any]:
    """Select highest ranked available worker for target architecture."""
    ranked = rank_candidate_workers(workers, target_arch, options, active_job_counts)
    return ranked[0] if ranked else None


def extract_package_name(pkg_spec: Any) -> str:
    """Extract package name from a package specification entry."""
    if isinstance(pkg_spec, str):
        return pkg_spec
    if not isinstance(pkg_spec, dict):
        return str(pkg_spec)

    if "name" in pkg_spec and isinstance(pkg_spec["name"], str):
        return pkg_spec["name"]

    source = pkg_spec.get("source", "")
    if isinstance(source, str) and "://" in source:
        scheme, rest = source.split("://", 1)
        if scheme == "apt":
            name = rest.partition("=")[0].partition("[")[0]
            if name:
                return name
        elif scheme in ("git", "https", "http", "file"):
            base = rest.split("/")[-1].split(";")[0]
            if base.endswith(".git"):
                base = base[:-4]
            elif base.endswith(".dsc"):
                base = base.split("_")[0]
            if base:
                return base
    return str(pkg_spec.get("name") or pkg_spec.get("source") or "unknown")


def decompose_build(
    build_id: str,
    spec: dict[str, Any],
    target_arch: str = "amd64",
    cached_packages: Optional[Union[set[str], list[str], Callable[[str, str], bool]]] = None,
) -> list[dict[str, Any]]:
    """Decompose build specification into discrete package jobs and an image job."""
    jobs: list[dict[str, Any]] = []
    packages = spec.get("packages") or []

    for idx, pkg_spec in enumerate(packages):
        pkg_name = extract_package_name(pkg_spec)
        pkg_arch = target_arch
        if isinstance(pkg_spec, dict):
            pkg_arch = pkg_spec.get("arch", target_arch)

        is_cached = False
        if callable(cached_packages):
            is_cached = cached_packages(pkg_name, pkg_arch)
        elif cached_packages is not None:
            is_cached = pkg_name in cached_packages or f"{pkg_name}-{pkg_arch}" in cached_packages

        if not is_cached:
            job_id = f"job-{build_id}-pkg-{pkg_name}"
            jobs.append({
                "id": job_id,
                "build_id": build_id,
                "kind": "package",
                "target_arch": pkg_arch,
                "package_name": pkg_name,
                "status": "queued",
            })

    image_job_id = f"job-{build_id}-img"
    jobs.append({
        "id": image_job_id,
        "build_id": build_id,
        "kind": "image",
        "target_arch": target_arch,
        "package_name": None,
        "status": "queued",
    })

    return jobs


def decompose_multiconfig_targets(
    build_id: str,
    spec: dict[str, Any],
    default_arch: str = "amd64",
) -> list[dict[str, Any]]:
    """Decompose multiconfig or matrix specification into target image jobs."""
    jobs: list[dict[str, Any]] = []
    multiconfig = spec.get("multiconfig")
    if isinstance(multiconfig, dict):
        for name, cfg in multiconfig.items():
            t_arch = cfg.get("arch", default_arch) if isinstance(cfg, dict) else default_arch
            jobs.append({
                "id": f"job-{build_id}-img-{name}",
                "build_id": build_id,
                "kind": "image",
                "target_arch": t_arch,
                "package_name": None,
                "target_name": name,
                "status": "queued",
            })
    elif isinstance(multiconfig, list):
        for idx, cfg in enumerate(multiconfig):
            name = cfg.get("name", f"target-{idx}") if isinstance(cfg, dict) else f"target-{idx}"
            t_arch = cfg.get("arch", default_arch) if isinstance(cfg, dict) else default_arch
            jobs.append({
                "id": f"job-{build_id}-img-{name}",
                "build_id": build_id,
                "kind": "image",
                "target_arch": t_arch,
                "package_name": None,
                "target_name": name,
                "status": "queued",
            })
    elif "targets" in spec and isinstance(spec["targets"], list):
        for idx, cfg in enumerate(spec["targets"]):
            name = cfg.get("name", f"target-{idx}") if isinstance(cfg, dict) else f"target-{idx}"
            t_arch = cfg.get("arch", default_arch) if isinstance(cfg, dict) else default_arch
            jobs.append({
                "id": f"job-{build_id}-img-{name}",
                "build_id": build_id,
                "kind": "image",
                "target_arch": t_arch,
                "package_name": None,
                "target_name": name,
                "status": "queued",
            })
    else:
        jobs.append({
            "id": f"job-{build_id}-img",
            "build_id": build_id,
            "kind": "image",
            "target_arch": spec.get("architecture", default_arch),
            "package_name": None,
            "status": "queued",
        })
    return jobs


class BuildScheduler:
    """Capability-aware scheduler for distributed build jobs."""

    def __init__(
        self,
        db: Optional[Any] = None,
        native_grace: float = DEFAULT_NATIVE_GRACE,
        stale_after: float = DEFAULT_STALE_AFTER,
        job_lost_grace: float = DEFAULT_JOB_LOST_GRACE,
        clock: Callable[[], float] = time.time,
    ):
        self.db = db
        self.native_grace = native_grace
        self.stale_after = stale_after
        self.job_lost_grace = job_lost_grace
        self.clock = clock

    def evaluate_arch_score(self, worker: Any, target_arch: str) -> float:
        return evaluate_arch_score(worker, target_arch)

    def check_arch_constraints(
        self,
        score: float,
        options: Optional[dict[str, Any]] = None,
    ) -> bool:
        return check_arch_constraints(score, options)

    def rank_workers(
        self,
        workers: list[Any],
        target_arch: str,
        options: Optional[dict[str, Any]] = None,
        active_job_counts: Optional[dict[str, int]] = None,
    ) -> list[Any]:
        return rank_candidate_workers(workers, target_arch, options, active_job_counts)

    def select_worker(
        self,
        workers: list[Any],
        target_arch: str,
        options: Optional[dict[str, Any]] = None,
        active_job_counts: Optional[dict[str, int]] = None,
    ) -> Optional[Any]:
        return select_best_worker(workers, target_arch, options, active_job_counts)

    def get_active_job_counts(self) -> dict[str, int]:
        """Get active job counts per worker from the database."""
        if not self.db:
            return {}
        cur = self.db.conn.execute(
            "SELECT worker_id, COUNT(*) as cnt FROM jobs WHERE status IN ('claimed', 'running') AND worker_id IS NOT NULL GROUP BY worker_id"
        )
        return {row["worker_id"]: row["cnt"] for row in cur.fetchall()}

    def get_worker_active_jobs(self, worker_id: str) -> int:
        """Get count of active jobs assigned to worker."""
        if not self.db:
            return 0
        cur = self.db.conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE worker_id = ? AND status IN ('claimed', 'running')",
            (worker_id,),
        )
        row = cur.fetchone()
        return row[0] if row else 0

    def is_image_job_blocked(self, build_id: str) -> bool:
        """Check if an image assembly job is blocked by pending prerequisite package jobs."""
        if not self.db:
            return False
        cur = self.db.conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE build_id = ? AND kind = 'package' AND status != 'completed'",
            (build_id,),
        )
        row = cur.fetchone()
        return (row[0] > 0) if row else False

    def decompose_build(
        self,
        build_id: str,
        spec: dict[str, Any],
        target_arch: str = "amd64",
        cached_packages: Optional[Union[set[str], list[str], Callable[[str, str], bool]]] = None,
    ) -> list[dict[str, Any]]:
        return decompose_build(build_id, spec, target_arch, cached_packages)

    def decompose_and_create_jobs(
        self,
        build_id: str,
        spec: dict[str, Any],
        target_arch: str = "amd64",
        cached_packages: Optional[Union[set[str], list[str], Callable[[str, str], bool]]] = None,
    ) -> list[dict[str, Any]]:
        """Decompose spec and persist generated jobs in database."""
        job_defs = decompose_build(build_id, spec, target_arch, cached_packages)
        created = []
        if self.db:
            for jd in job_defs:
                j = self.db.builds.create_job(
                    id=jd["id"],
                    build_id=jd["build_id"],
                    kind=jd["kind"],
                    target_arch=jd["target_arch"],
                    package_name=jd.get("package_name"),
                    status=jd.get("status", "queued"),
                )
                created.append(j)
        return created or job_defs

    def schedule_coarse_targets(
        self,
        build_id: str,
        spec: dict[str, Any],
        default_arch: str = "amd64",
    ) -> list[dict[str, Any]]:
        """Decompose and create image jobs for multiconfig target specifications."""
        job_defs = decompose_multiconfig_targets(build_id, spec, default_arch)
        created = []
        if self.db:
            for jd in job_defs:
                j = self.db.builds.create_job(
                    id=jd["id"],
                    build_id=jd["build_id"],
                    kind=jd["kind"],
                    target_arch=jd["target_arch"],
                    package_name=None,
                    status=jd.get("status", "queued"),
                )
                created.append(j)
        return created or job_defs

    def claim_job(self, worker_id: str) -> Optional[dict[str, Any]]:
        """Find and claim the best matching queued job for a worker."""
        if not self.db:
            return None

        conn = self.db.conn
        # BEGIN IMMEDIATE takes the write lock first, so two claimers cannot pick the same job.
        conn.execute("BEGIN IMMEDIATE")
        try:
            claimed = self._claim_locked(worker_id)
        except BaseException:
            conn.rollback()
            raise
        conn.commit()
        return claimed

    def _claim_locked(self, worker_id: str) -> Optional[dict[str, Any]]:
        """Pick and claim a job; the caller holds the write transaction."""
        worker = self.db.workers.get(worker_id)
        if not worker or worker.get("status") != "online":
            return None

        slots = worker.get("concurrency_slots", 1)
        if self.get_worker_active_jobs(worker_id) >= slots:
            return None
        now = self.clock()

        cur = self.db.conn.execute(
            "SELECT * FROM jobs WHERE status = 'queued' ORDER BY created_at ASC"
        )
        queued_jobs = [dict(r) for r in cur.fetchall()]

        for j in queued_jobs:
            if j["kind"] == "image":
                cur_pkg = self.db.conn.execute(
                    "SELECT COUNT(*) FROM jobs WHERE build_id = ? AND kind = 'package' AND status != 'completed'",
                    (j["build_id"],),
                )
                if cur_pkg.fetchone()[0] > 0:
                    continue

            cur_build = self.db.conn.execute(
                "SELECT * FROM builds WHERE id = ?",
                (j["build_id"],),
            )
            b_row = cur_build.fetchone()
            options = {}
            if b_row and b_row["options"]:
                try:
                    options = json.loads(b_row["options"])
                except Exception:
                    pass

            score = evaluate_arch_score(worker, j["target_arch"])
            if not check_arch_constraints(score, options):
                continue
            if now - j["created_at"] < self.native_grace and self._better_worker_idle(
                worker_id, score, j["target_arch"], options, now
            ):
                continue

            self.db.conn.execute(
                "UPDATE jobs SET status = 'claimed', worker_id = ?, started_at = ? WHERE id = ?",
                (worker_id, now, j["id"]),
            )
            self.db.conn.execute(
                "UPDATE builds SET status = 'running', started_at = ? WHERE id = ?",
                (now, j["build_id"]),
            )
            self.db.builds.changed(j["build_id"])

            b_dict = dict(b_row) if b_row else {}
            proj_name = b_dict.get("project", "")
            cur_proj = self.db.conn.execute(
                "SELECT * FROM projects WHERE name = ?",
                (proj_name,),
            )
            p_row = cur_proj.fetchone()
            is_rel = bool(b_dict.get("is_release", False))
            if p_row:
                p_dict = dict(p_row)
                s3_bkt = p_dict["prod_bucket"] if is_rel else p_dict["dev_bucket"]
            else:
                s3_bkt = f"seine-{proj_name}-prod" if is_rel else f"seine-{proj_name}-dev"

            return {
                "job_id": j["id"],
                "build_id": j["build_id"],
                "project": proj_name,
                "kind": j["kind"],
                "target_arch": j["target_arch"],
                "worktree_digest": b_dict.get("worktree_digest", ""),
                "s3_bucket": s3_bkt,
                "spec_file": b_dict.get("spec_file", "spec.yaml"),
                "spec_files": json.loads(b_dict.get("spec_files") or "[]"),
                "options": options,
                "package_name": j.get("package_name"),
            }

        return None

    def _better_worker_idle(
        self,
        worker_id: str,
        score: float,
        target_arch: str,
        options: dict[str, Any],
        now: float,
    ) -> bool:
        """Return True if a fresh, free worker other than worker_id scores higher for the job."""
        counts = self.get_active_job_counts()
        rows = self.db.conn.execute(
            "SELECT * FROM workers WHERE status = 'online' AND last_seen >= ? AND id != ?",
            (now - self.stale_after, worker_id),
        ).fetchall()
        for row in rows:
            other = self.db.workers.get(row["id"])
            if counts.get(other["id"], 0) >= other.get("concurrency_slots", 1):
                continue
            other_score = evaluate_arch_score(other, target_arch)
            if other_score > score and check_arch_constraints(other_score, options):
                return True
        return False

    def job_belongs_to(self, job_id: str, worker_id: str) -> bool:
        """Return True if the job is currently assigned to the worker."""
        if not self.db:
            return False
        row = self.db.conn.execute(
            "SELECT 1 FROM jobs WHERE id = ? AND worker_id = ?", (job_id, worker_id)
        ).fetchone()
        return row is not None

    def worker_holds_build(self, worker_id: str, build_id: str) -> bool:
        """Return True if the worker currently holds a job of the build."""
        if not self.db:
            return False
        row = self.db.conn.execute(
            "SELECT 1 FROM jobs WHERE build_id = ? AND worker_id = ? "
            "AND status IN ('claimed', 'running')",
            (build_id, worker_id),
        ).fetchone()
        return row is not None

    def reap_stale(self, now: Optional[float] = None, stale_after: float = 120.0) -> list[str]:
        """Mark silent workers offline and requeue their jobs; return the reaped job ids."""
        if not self.db:
            return []
        now = time.time() if now is None else now
        conn = self.db.conn
        stale = [
            r["id"]
            for r in conn.execute(
                "SELECT id FROM workers WHERE status = 'online' AND last_seen < ?",
                (now - stale_after,),
            ).fetchall()
        ]
        reaped: list[str] = []
        exhausted: list[str] = []
        with conn:
            for w_id in stale:
                conn.execute("UPDATE workers SET status = 'offline' WHERE id = ?", (w_id,))
                rows = conn.execute(
                    "SELECT id, attempts FROM jobs "
                    "WHERE worker_id = ? AND status IN ('claimed', 'running')",
                    (w_id,),
                ).fetchall()
                for r in rows:
                    reaped.append(r["id"])
                    if r["attempts"] + 1 >= MAX_JOB_ATTEMPTS:
                        exhausted.append(r["id"])
                        conn.execute("UPDATE jobs SET attempts = attempts + 1 WHERE id = ?", (r["id"],))
                    else:
                        conn.execute(
                            "UPDATE jobs SET status = 'queued', worker_id = NULL, "
                            "started_at = NULL, cancel_requested = 0, attempts = attempts + 1 "
                            "WHERE id = ?",
                            (r["id"],),
                        )
        for job_id in exhausted:
            self.db.builds.update_job_status(job_id, "failed")
        return reaped

    def reconcile_worker_jobs(
        self, worker_id: str, running_jobs: list[str], now: Optional[float] = None
    ) -> list[str]:
        """Requeue the worker's jobs it no longer reports; fail them after MAX_JOB_ATTEMPTS."""
        if not self.db:
            return []
        now = self.clock() if now is None else now
        conn = self.db.conn
        lost: list[str] = []
        exhausted: list[str] = []
        conn.execute("BEGIN IMMEDIATE")
        try:
            rows = conn.execute(
                "SELECT id, attempts FROM jobs WHERE worker_id = ? "
                "AND status IN ('claimed', 'running') AND started_at <= ?",
                (worker_id, now - self.job_lost_grace),
            ).fetchall()
            for r in rows:
                if r["id"] in running_jobs:
                    continue
                lost.append(r["id"])
                if r["attempts"] + 1 >= MAX_JOB_ATTEMPTS:
                    exhausted.append(r["id"])
                    conn.execute("UPDATE jobs SET attempts = attempts + 1 WHERE id = ?", (r["id"],))
                else:
                    conn.execute(
                        "UPDATE jobs SET status = 'queued', worker_id = NULL, "
                        "started_at = NULL, cancel_requested = 0, attempts = attempts + 1 "
                        "WHERE id = ?",
                        (r["id"],),
                    )
        except BaseException:
            conn.rollback()
            raise
        conn.commit()
        for job_id in lost:
            outcome = "failed" if job_id in exhausted else "requeued"
            logger.info(f"Job {job_id} lost by worker {worker_id}, {outcome}")
        for job_id in exhausted:
            self.db.builds.update_job_status(job_id, "failed", error_message="job lost by its worker")
        return lost

    def request_cancel(self, build_id: str) -> bool:
        """Cancel queued jobs, flag running ones, and cancel the build once all are done."""
        if not self.db:
            return False
        conn = self.db.conn
        build = self.db.builds.get(build_id)
        if not build or build["status"] in ("completed", "failed", "cancelled"):
            return False
        with conn:
            conn.execute(
                "UPDATE jobs SET status = 'cancelled', finished_at = ? "
                "WHERE build_id = ? AND status = 'queued'",
                (time.time(), build_id),
            )
            conn.execute(
                "UPDATE jobs SET cancel_requested = 1 "
                "WHERE build_id = ? AND status IN ('claimed', 'running')",
                (build_id,),
            )
            left = conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE build_id = ? "
                "AND status IN ('queued', 'claimed', 'running')",
                (build_id,),
            ).fetchone()[0]
            if left == 0:
                self.db.builds.update_status(build_id, "cancelled")
        return True

    def cancel_requested_jobs(self, worker_id: str) -> list[str]:
        """Return ids of the worker's active jobs that were asked to cancel."""
        if not self.db:
            return []
        rows = self.db.conn.execute(
            "SELECT id FROM jobs WHERE worker_id = ? AND cancel_requested = 1 "
            "AND status IN ('claimed', 'running') ORDER BY created_at ASC",
            (worker_id,),
        ).fetchall()
        return [r["id"] for r in rows]
