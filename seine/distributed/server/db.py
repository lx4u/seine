# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0
"""Database repository layer for seine-server."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import sqlite3
import threading
import time
import uuid
from typing import Any, Callable, Optional, Union

from seine.distributed.server.schema import init_db

logger = logging.getLogger(__name__)


PROJECT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")
HOME_PREFIX = "home-"


def validate_project_name(name: str) -> str:
    """Return name if it is a valid project name, raise ValueError otherwise."""
    if not isinstance(name, str) or not PROJECT_NAME_RE.match(name):
        raise ValueError(
            f"Invalid project name {name!r}: use 2-41 chars of a-z, 0-9 and '-', "
            "starting with a letter or digit"
        )
    return name


def check_not_reserved(name: str, dev_only: bool = False) -> None:
    """Raise ValueError for a home- name given to anything but a dev-only home project."""
    if name.startswith(HOME_PREFIX) and not dev_only:
        raise ValueError(f"project names starting with '{HOME_PREFIX}' are reserved for home projects")


def hash_secret(secret: str) -> str:
    """Return the sha256 hex digest stored in place of a token."""
    return hashlib.sha256(secret.encode()).hexdigest()


def connect_db(db_path: str = "seine.db") -> sqlite3.Connection:
    """Connect to SQLite with WAL mode and foreign keys enabled."""
    conn = sqlite3.connect(db_path, check_same_thread=False, uri=db_path.startswith("file:"))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


class ThreadLocalConnection:
    """Give each thread its own SQLite connection, opened on first use."""

    def __init__(self, db_path: str):
        if db_path == ":memory:":
            # Shared cache: threads see one in-memory database (tests only, no busy wait).
            db_path = f"file:seine-mem-{uuid.uuid4().hex}?mode=memory&cache=shared"
        self._path = db_path
        self._local = threading.local()
        self._opened: list[sqlite3.Connection] = []
        self._lock = threading.Lock()
        # The first connection also keeps an in-memory database alive.
        self._get()

    def _get(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = connect_db(self._path)
            self._local.conn = conn
            with self._lock:
                self._opened.append(conn)
        return conn

    def execute(self, *args: Any) -> sqlite3.Cursor:
        return self._get().execute(*args)

    def executescript(self, script: str) -> sqlite3.Cursor:
        return self._get().executescript(script)

    def commit(self) -> None:
        self._get().commit()

    def rollback(self) -> None:
        self._get().rollback()

    def __enter__(self) -> "ThreadLocalConnection":
        self._get().__enter__()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self._get().__exit__(exc_type, exc_val, exc_tb)

    def close(self) -> None:
        with self._lock:
            for conn in self._opened:
                conn.close()
            self._opened.clear()
        self._local = threading.local()


class ProjectRepo:
    """Repository managing project definitions and team memberships."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    @staticmethod
    def _to_dict(row: sqlite3.Row) -> dict[str, Any]:
        res = dict(row)
        res["dev_only"] = bool(res["dev_only"])
        return res

    def create(
        self,
        name: str,
        prod_bucket: Optional[str] = None,
        dev_bucket: Optional[str] = None,
        id: Optional[str] = None,
        dev_only: bool = False,
    ) -> dict[str, Any]:
        validate_project_name(name)
        check_not_reserved(name, dev_only)
        if dev_only and prod_bucket:
            raise ValueError("a dev-only project has no prod bucket")
        proj_id = id or name
        dev = dev_bucket or f"seine-{name}-dev"
        prod = "" if dev_only else (prod_bucket or f"seine-{name}-prod")
        now = time.time()
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO projects (id, name, prod_bucket, dev_bucket, dev_only, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (proj_id, name, prod, dev, 1 if dev_only else 0, now),
            )
        return {
            "id": proj_id,
            "name": name,
            "prod_bucket": prod,
            "dev_bucket": dev,
            "dev_only": dev_only,
            "quota_gb": None,
            "created_at": now,
        }

    def ensure(
        self,
        name: str,
        dev_bucket: Optional[str] = None,
        prod_bucket: Optional[str] = None,
        id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Ensure a project exists, updating buckets only if explicitly specified."""
        validate_project_name(name)
        existing = self.get(name)
        if existing:
            if dev_bucket is not None or prod_bucket is not None:
                dev = dev_bucket if dev_bucket is not None else existing["dev_bucket"]
                prod = prod_bucket if prod_bucket is not None else existing["prod_bucket"]
                with self.conn:
                    self.conn.execute(
                        "UPDATE projects SET dev_bucket = ?, prod_bucket = ? WHERE id = ?",
                        (dev, prod, existing["id"]),
                    )
                return self.get(name)  # type: ignore
            return existing

        check_not_reserved(name)
        proj_id = id or name
        dev = dev_bucket or f"seine-{name}-dev"
        prod = prod_bucket or f"seine-{name}-prod"
        now = time.time()
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO projects (id, name, prod_bucket, dev_bucket, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    dev_bucket = excluded.dev_bucket,
                    prod_bucket = excluded.prod_bucket
                """,
                (proj_id, name, prod, dev, now),
            )
        return self.get(name)  # type: ignore

    def get(self, id_or_name: str) -> Optional[dict[str, Any]]:
        cur = self.conn.execute(
            "SELECT * FROM projects WHERE id = ? OR name = ?",
            (id_or_name, id_or_name),
        )
        row = cur.fetchone()
        return self._to_dict(row) if row else None

    def list(self) -> list[dict[str, Any]]:
        cur = self.conn.execute("SELECT * FROM projects ORDER BY created_at ASC")
        return [self._to_dict(r) for r in cur.fetchall()]

    def set_quota(self, id_or_name: str, quota_gb: Optional[float]) -> bool:
        """Set the storage quota in GB; None removes it."""
        if quota_gb is not None and quota_gb <= 0:
            raise ValueError("quota must be greater than zero")
        with self.conn:
            cur = self.conn.execute(
                "UPDATE projects SET quota_gb = ? WHERE id = ? OR name = ?",
                (quota_gb, id_or_name, id_or_name),
            )
            return cur.rowcount > 0

    def delete(self, id_or_name: str) -> bool:
        with self.conn:
            cur = self.conn.execute(
                "DELETE FROM projects WHERE id = ? OR name = ?",
                (id_or_name, id_or_name),
            )
            return cur.rowcount > 0

    def add_member(
        self,
        project_id: str,
        user_id: str,
        role: str = "developer",
    ) -> dict[str, Any]:
        if role not in ("developer", "releaser", "admin"):
            raise ValueError(f"Invalid role: {role}. Must be developer, releaser, or admin")
        proj = self.get(project_id)
        resolved_id = proj["id"] if proj else project_id
        now = time.time()
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO project_members (project_id, user_id, role, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(project_id, user_id) DO UPDATE SET
                    role = excluded.role
                """,
                (resolved_id, user_id, role, now),
            )
        return {
            "project_id": resolved_id,
            "user_id": user_id,
            "role": role,
            "created_at": now,
        }

    def get_member(self, project_id: str, user_id: str) -> Optional[dict[str, Any]]:
        proj = self.get(project_id)
        resolved_id = proj["id"] if proj else project_id
        cur = self.conn.execute(
            "SELECT * FROM project_members WHERE project_id = ? AND user_id = ?",
            (resolved_id, user_id),
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def list_members(self, project_id: str) -> list[dict[str, Any]]:
        proj = self.get(project_id)
        resolved_id = proj["id"] if proj else project_id
        cur = self.conn.execute(
            "SELECT * FROM project_members WHERE project_id = ? ORDER BY user_id ASC",
            (resolved_id,),
        )
        return [dict(r) for r in cur.fetchall()]

    def remove_member(self, project_id: str, user_id: str) -> bool:
        proj = self.get(project_id)
        resolved_id = proj["id"] if proj else project_id
        with self.conn:
            cur = self.conn.execute(
                "DELETE FROM project_members WHERE project_id = ? AND user_id = ?",
                (resolved_id, user_id),
            )
            # A default project the user can no longer reach would only mislead.
            self.conn.execute(
                "UPDATE users SET default_project = NULL WHERE id = ? AND default_project = ?",
                (user_id, resolved_id),
            )
            return cur.rowcount > 0


class BuildRepo:
    """Repository managing builds and associated job dispatches."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        # Called with the build row after its state or artifacts changed.
        self.listeners: list[Callable[[dict[str, Any]], None]] = []

    def changed(self, build_id: str) -> None:
        build = self.get(build_id)
        for listener in self.listeners if build else ():
            try:
                listener(build)
            except Exception:
                logger.exception("build listener failed for %s", build_id)

    def create(
        self,
        id: str,
        project: str,
        target_arch: str = "amd64",
        is_release: bool = False,
        worktree_digest: str = "",
        spec_file: str = "spec.yaml",
        spec_files: Optional[list[str]] = None,
        spec_digest: str = "",
        options: Optional[dict[str, Any]] = None,
        status: str = "queued",
        artifact_urls: Optional[list[str]] = None,
        user_id: Optional[str] = None,
    ) -> dict[str, Any]:
        now = time.time()
        opts_json = json.dumps(options or {})
        urls_json = json.dumps(artifact_urls or [])
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO builds (
                    id, project, target_arch, is_release, status,
                    worktree_digest, spec_file, spec_files, spec_digest, options,
                    created_at, artifact_urls, user_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    id,
                    project,
                    target_arch,
                    1 if is_release else 0,
                    status,
                    worktree_digest,
                    spec_file,
                    json.dumps(spec_files or [spec_file]),
                    spec_digest,
                    opts_json,
                    now,
                    urls_json,
                    user_id,
                ),
            )
        return self.get(id)  # type: ignore

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict[str, Any]:
        res = dict(row)
        res["is_release"] = bool(res["is_release"])
        res["options"] = json.loads(res["options"]) if res.get("options") else {}
        res["spec_files"] = json.loads(res["spec_files"] or "[]") or [res["spec_file"]]
        res["artifact_urls"] = json.loads(res["artifact_urls"]) if res.get("artifact_urls") else []
        res["artifact_meta"] = json.loads(res["artifact_meta"]) if res.get("artifact_meta") else []
        return res

    def latest_for_spec(
        self, project: str, spec_digest: str, target_arch: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """Return the newest build of a project that carries this spec digest."""
        if not spec_digest:
            return None
        sql = "SELECT * FROM builds WHERE project = ? AND spec_digest = ?"
        args: list[Any] = [project, spec_digest]
        if target_arch:
            sql += " AND target_arch = ?"
            args.append(target_arch)
        row = self.conn.execute(sql + " ORDER BY created_at DESC LIMIT 1", args).fetchone()
        return self._decode(row) if row else None

    def get(self, build_id: str) -> Optional[dict[str, Any]]:
        cur = self.conn.execute("SELECT * FROM builds WHERE id = ?", (build_id,))
        row = cur.fetchone()
        if not row:
            return None
        return self._decode(row)

    def list(
        self,
        project: Optional[str] = None,
        status: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM builds WHERE 1=1"
        params: list[Any] = []
        if project:
            query += " AND project = ?"
            params.append(project)
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY created_at DESC"
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)

        cur = self.conn.execute(query, tuple(params))
        return [self._decode(r) for r in cur.fetchall()]

    def update_status(
        self,
        build_id: str,
        status: str,
        started_at: Optional[float] = None,
        finished_at: Optional[float] = None,
        artifact_urls: Optional[list[str]] = None,
        artifact_meta: Optional[list[dict[str, Any]]] = None,
        error_message: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        updates = ["status = ?"]
        params: list[Any] = [status]
        if started_at is not None:
            updates.append("started_at = ?")
            params.append(started_at)
        if finished_at is not None:
            updates.append("finished_at = ?")
            params.append(finished_at)
        elif status in ("completed", "failed", "cancelled") and finished_at is None:
            updates.append("finished_at = ?")
            params.append(time.time())
        if artifact_urls is not None:
            updates.append("artifact_urls = ?")
            params.append(json.dumps(artifact_urls))
        if artifact_meta is not None:
            updates.append("artifact_meta = ?")
            params.append(json.dumps(artifact_meta))
        if error_message is not None:
            updates.append("error_message = ?")
            params.append(error_message)

        params.append(build_id)
        with self.conn:
            self.conn.execute(
                f"UPDATE builds SET {', '.join(updates)} WHERE id = ?",
                tuple(params),
            )
        self.changed(build_id)
        return self.get(build_id)

    def mark_artifacts_expired(
        self, build_id: str, reason: str, now: Optional[float] = None
    ) -> bool:
        """Drop the download targets of a build whose objects were deleted.

        The artifact metadata stays, so clients can still list what it held.
        """
        if reason not in ("ttl", "pressure"):
            raise ValueError(f"unknown expiry reason: {reason}")
        with self.conn:
            cur = self.conn.execute(
                """
                UPDATE builds
                SET artifact_urls = '[]',
                    artifacts_expired_at = ?, artifacts_expired_reason = ?
                WHERE id = ?
                """,
                (time.time() if now is None else now, reason, build_id),
            )
            expired = cur.rowcount > 0
        if expired:
            self.changed(build_id)
        return expired

    def evictable_builds(self, project: str, older_than: float) -> list[dict[str, Any]]:
        """Finished non-release builds of a project that still hold artifacts.

        Only builds finished before `older_than` (epoch seconds), oldest first.
        """
        cur = self.conn.execute(
            """
            SELECT * FROM builds
            WHERE project = ? AND is_release = 0
              AND status IN ('completed', 'failed', 'cancelled')
              AND artifact_urls != '[]'
              AND artifacts_expired_at IS NULL
              AND finished_at IS NOT NULL AND finished_at < ?
            ORDER BY finished_at ASC
            """,
            (project, older_than),
        )
        return [self.get(r["id"]) for r in cur.fetchall()]  # type: ignore

    def has_active_builds(self, project: str) -> bool:
        """True while the project has a build that has not finished."""
        cur = self.conn.execute(
            "SELECT 1 FROM builds WHERE project = ? "
            "AND status NOT IN ('completed', 'failed', 'cancelled') LIMIT 1",
            (project,),
        )
        return cur.fetchone() is not None

    def active_worktree_digests(self, project: str) -> set[str]:
        """Worktree digests of the project's builds that have not finished."""
        cur = self.conn.execute(
            """
            SELECT DISTINCT worktree_digest FROM builds
            WHERE project = ? AND status NOT IN ('completed', 'failed', 'cancelled')
            """,
            (project,),
        )
        return {r["worktree_digest"] for r in cur.fetchall()}

    def create_job(
        self,
        id: str,
        build_id: str,
        kind: str = "image",
        target_arch: str = "amd64",
        package_name: Optional[str] = None,
        worker_id: Optional[str] = None,
        status: str = "queued",
    ) -> dict[str, Any]:
        now = time.time()
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO jobs (id, build_id, kind, target_arch, package_name, worker_id, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (id, build_id, kind, target_arch, package_name, worker_id, status, now),
            )
        return self.get_job(id)  # type: ignore

    def get_job(self, job_id: str) -> Optional[dict[str, Any]]:
        cur = self.conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
        row = cur.fetchone()
        return dict(row) if row else None

    def list_jobs(
        self,
        build_id: Optional[str] = None,
        status: Optional[str] = None,
        worker_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM jobs WHERE 1=1"
        params: list[Any] = []
        if build_id:
            query += " AND build_id = ?"
            params.append(build_id)
        if status:
            query += " AND status = ?"
            params.append(status)
        if worker_id:
            query += " AND worker_id = ?"
            params.append(worker_id)
        query += " ORDER BY created_at ASC"

        cur = self.conn.execute(query, tuple(params))
        return [dict(r) for r in cur.fetchall()]

    def update_job_status(
        self,
        job_id: str,
        status: str,
        worker_id: Optional[str] = None,
        started_at: Optional[float] = None,
        finished_at: Optional[float] = None,
        error_message: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        updates = ["status = ?"]
        params: list[Any] = [status]
        if worker_id is not None:
            updates.append("worker_id = ?")
            params.append(worker_id)
        if started_at is not None:
            updates.append("started_at = ?")
            params.append(started_at)
        if finished_at is not None:
            updates.append("finished_at = ?")
            params.append(finished_at)
        elif status in ("completed", "failed", "cancelled") and finished_at is None:
            updates.append("finished_at = ?")
            params.append(time.time())

        params.append(job_id)
        with self.conn:
            self.conn.execute(
                f"UPDATE jobs SET {', '.join(updates)} WHERE id = ?",
                tuple(params),
            )
            cur = self.conn.execute("SELECT build_id FROM jobs WHERE id = ?", (job_id,))
            row = cur.fetchone()
            if row:
                b_id = row["build_id"]
                if status in ("failed", "cancelled"):
                    self.conn.execute(
                        "UPDATE jobs SET status = 'cancelled', finished_at = ? "
                        "WHERE build_id = ? AND status = 'queued'",
                        (time.time(), b_id),
                    )
                    left = self.conn.execute(
                        "SELECT COUNT(*) FROM jobs WHERE build_id = ? "
                        "AND status IN ('queued', 'claimed', 'running')",
                        (b_id,),
                    ).fetchone()[0]
                    if status == "failed" or left == 0:
                        self.update_status(b_id, status, error_message=error_message)
                elif status == "completed":
                    cur_jobs = self.conn.execute(
                        "SELECT status FROM jobs WHERE build_id = ?",
                        (b_id,),
                    )
                    all_done = all(r["status"] == "completed" for r in cur_jobs.fetchall())
                    if all_done:
                        self.update_status(b_id, "completed")
        return self.get_job(job_id)


class WorkerRepo:
    """Repository managing worker registrations, capabilities, and heartbeats."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def register(
        self,
        id: str,
        hostname: str,
        native_arch: str,
        arch_scores: dict[str, float],
        free_disk_gb: float,
        token: str,
        status: str = "online",
        concurrency_slots: int = 1,
    ) -> dict[str, Any]:
        now = time.time()
        scores_json = json.dumps(arch_scores or {})
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO workers (
                    id, hostname, native_arch, arch_scores, free_disk_gb,
                    concurrency_slots, token_hash, status, last_seen, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    hostname = excluded.hostname,
                    native_arch = excluded.native_arch,
                    arch_scores = excluded.arch_scores,
                    free_disk_gb = excluded.free_disk_gb,
                    concurrency_slots = excluded.concurrency_slots,
                    token_hash = excluded.token_hash,
                    status = excluded.status,
                    last_seen = excluded.last_seen
                """,
                (
                    id,
                    hostname,
                    native_arch,
                    scores_json,
                    free_disk_gb,
                    concurrency_slots,
                    hash_secret(token),
                    status,
                    now,
                    now,
                ),
            )
        return self.get(id)  # type: ignore

    def heartbeat(
        self,
        worker_id: str,
        free_disk_gb: float,
        running_jobs: Optional[list[str]] = None,
    ) -> bool:
        now = time.time()
        with self.conn:
            cur = self.conn.execute(
                "UPDATE workers SET free_disk_gb = ?, last_seen = ?, status = 'online' WHERE id = ?",
                (free_disk_gb, now, worker_id),
            )
            return cur.rowcount > 0

    def get(self, worker_id: str) -> Optional[dict[str, Any]]:
        cur = self.conn.execute("SELECT * FROM workers WHERE id = ?", (worker_id,))
        row = cur.fetchone()
        if not row:
            return None
        return self._to_dict(row)

    @staticmethod
    def _to_dict(row: sqlite3.Row) -> dict[str, Any]:
        res = dict(row)
        res.pop("token_hash", None)
        res["arch_scores"] = json.loads(res["arch_scores"]) if res.get("arch_scores") else {}
        return res

    def get_by_token(self, token: str) -> Optional[dict[str, Any]]:
        cur = self.conn.execute(
            "SELECT * FROM workers WHERE token_hash = ?", (hash_secret(token),)
        )
        row = cur.fetchone()
        return self._to_dict(row) if row else None

    def list(self, status: Optional[str] = None) -> list[dict[str, Any]]:
        """Return all workers sorted by hostname and ID."""
        query = "SELECT * FROM workers"
        params: list[Any] = []
        if status:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY hostname ASC, id ASC"

        cur = self.conn.execute(query, tuple(params))
        return [self._to_dict(r) for r in cur.fetchall()]

    def pause(self, worker_id: str, paused: bool = True) -> bool:
        """Toggle worker between 'paused' and 'online' state."""
        status = "paused" if paused else "online"
        with self.conn:
            cur = self.conn.execute(
                "UPDATE workers SET status = ? WHERE id = ?",
                (status, worker_id),
            )
            return cur.rowcount > 0

    def update_status(self, worker_id: str, status: str) -> bool:
        now = time.time()
        with self.conn:
            cur = self.conn.execute(
                "UPDATE workers SET status = ?, last_seen = ? WHERE id = ?",
                (status, now, worker_id),
            )
            return cur.rowcount > 0

    def delete(self, worker_id: str) -> bool:
        with self.conn:
            cur = self.conn.execute("DELETE FROM workers WHERE id = ?", (worker_id,))
            return cur.rowcount > 0


class LastAdminError(ValueError):
    """Raised when a change would leave no active system administrator."""


class UserRepo:
    """Repository managing user accounts."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    @staticmethod
    def _to_dict(row: sqlite3.Row) -> dict[str, Any]:
        res = dict(row)
        res["is_admin"] = bool(res["is_admin"])
        res["active"] = bool(res["active"])
        return res

    def create(self, id: str, is_admin: bool = False) -> dict[str, Any]:
        with self.conn:
            self.conn.execute(
                "INSERT INTO users (id, is_admin, active, created_at) VALUES (?, ?, 1, ?)",
                (id, 1 if is_admin else 0, time.time()),
            )
        return self.get(id)  # type: ignore

    def get(self, user_id: str) -> Optional[dict[str, Any]]:
        row = self.conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return self._to_dict(row) if row else None

    def list(self) -> list[dict[str, Any]]:
        cur = self.conn.execute("SELECT * FROM users ORDER BY id ASC")
        return [self._to_dict(r) for r in cur.fetchall()]

    def update(
        self,
        user_id: str,
        is_admin: Optional[bool] = None,
        active: Optional[bool] = None,
    ) -> Optional[dict[str, Any]]:
        """Change a user's flags; return None if unknown, refuse to drop the last active admin."""
        # BEGIN IMMEDIATE so two writers cannot each leave the other as the last admin.
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            user = self.get(user_id)
            if user is None:
                self.conn.rollback()
                return None
            new_admin = user["is_admin"] if is_admin is None else is_admin
            new_active = user["active"] if active is None else active
            if user["is_admin"] and user["active"] and not (new_admin and new_active):
                admins = self.conn.execute(
                    "SELECT COUNT(*) FROM users WHERE is_admin = 1 AND active = 1"
                ).fetchone()[0]
                if admins <= 1:
                    raise LastAdminError("Cannot demote or deactivate the last active administrator")
            self.conn.execute(
                "UPDATE users SET is_admin = ?, active = ? WHERE id = ?",
                (1 if new_admin else 0, 1 if new_active else 0, user_id),
            )
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise
        return self.get(user_id)

    def set_default_project(self, user_id: str, project_id: Optional[str]) -> bool:
        """Set the default project; None clears it."""
        with self.conn:
            cur = self.conn.execute(
                "UPDATE users SET default_project = ? WHERE id = ?",
                (project_id, user_id),
            )
            return cur.rowcount > 0

    def set_admin(self, user_id: str, is_admin: bool) -> bool:
        with self.conn:
            cur = self.conn.execute(
                "UPDATE users SET is_admin = ? WHERE id = ?",
                (1 if is_admin else 0, user_id),
            )
            return cur.rowcount > 0

    def set_active(self, user_id: str, active: bool) -> bool:
        with self.conn:
            cur = self.conn.execute(
                "UPDATE users SET active = ? WHERE id = ?",
                (1 if active else 0, user_id),
            )
            return cur.rowcount > 0


class TokenRepo:
    """Repository managing access tokens, stored only as sha256 hashes."""

    _COLUMNS = "id, user_id, kind, created_at, expires_at"

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def issue(
        self,
        user_id: str,
        kind: str = "pat",
        token: Optional[str] = None,
        expires_at: Optional[float] = None,
    ) -> dict[str, Any]:
        """Create a token and return it; the raw secret is only ever returned here."""
        tok = token or secrets.token_hex(24)
        tok_id = f"tok_{secrets.token_hex(4)}"
        now = time.time()
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO tokens (id, token_hash, user_id, kind, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (tok_id, hash_secret(tok), user_id, kind, now, expires_at),
            )
        return {
            "id": tok_id,
            "token": tok,
            "user_id": user_id,
            "kind": kind,
            "created_at": now,
            "expires_at": expires_at,
        }

    def validate(self, token: str) -> Optional[dict[str, Any]]:
        # The secret is 192 random bits, so a lookup by its hash leaks nothing useful.
        row = self.conn.execute(
            f"SELECT {self._COLUMNS} FROM tokens WHERE token_hash = ?",
            (hash_secret(token),),
        ).fetchone()
        if not row:
            return None
        res = dict(row)
        if res.get("expires_at") is not None and time.time() > res["expires_at"]:
            return None
        return res

    def get(self, token_id: str) -> Optional[dict[str, Any]]:
        row = self.conn.execute(
            f"SELECT {self._COLUMNS} FROM tokens WHERE id = ?", (token_id,)
        ).fetchone()
        return dict(row) if row else None

    def revoke(self, token_id: str) -> bool:
        with self.conn:
            cur = self.conn.execute("DELETE FROM tokens WHERE id = ?", (token_id,))
            return cur.rowcount > 0

    def list(
        self,
        user_id: Optional[str] = None,
        kind: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        query = f"SELECT {self._COLUMNS} FROM tokens WHERE 1=1"
        params: list[Any] = []
        if user_id:
            query += " AND user_id = ?"
            params.append(user_id)
        if kind:
            query += " AND kind = ?"
            params.append(kind)
        query += " ORDER BY created_at DESC"
        cur = self.conn.execute(query, tuple(params))
        return [dict(r) for r in cur.fetchall()]


class Database:
    """Unified database entrypoint providing access to entity repositories."""

    def __init__(
        self,
        config: Optional[Union[str, dict[str, Any]]] = None,
        db_path: Optional[str] = None,
    ):
        if config is None and db_path is None:
            cfg = {"type": "sqlite", "path": "seine.db"}
        elif isinstance(config, str):
            cfg = {"type": "sqlite", "path": config}
        elif isinstance(config, dict):
            if "database" in config and isinstance(config["database"], dict):
                cfg = config["database"]
            else:
                cfg = config
        elif db_path is not None:
            cfg = {"type": "sqlite", "path": db_path}
        else:
            cfg = {"type": "sqlite", "path": "seine.db"}

        db_type = cfg.get("type", "sqlite")
        if db_type == "sqlite":
            path = cfg.get("path", "seine.db")
            self.conn = ThreadLocalConnection(path)
            init_db(self.conn)
            self.engine = "sqlite"
            self.projects = ProjectRepo(self.conn)
            self.builds = BuildRepo(self.conn)
            self.workers = WorkerRepo(self.conn)
            self.users = UserRepo(self.conn)
            self.tokens = TokenRepo(self.conn)
            from seine.distributed.server.scheduler import BuildScheduler
            self.scheduler = BuildScheduler(self)
        elif db_type == "postgres":
            raise NotImplementedError("PostgreSQL database engine is not yet implemented")
        else:
            raise ValueError(f"Unsupported database engine type: {db_type}")

    @classmethod
    def from_config(cls, config: Union[str, dict[str, Any]]) -> "Database":
        return cls(config)

    def close(self) -> None:
        if hasattr(self, "conn") and self.conn:
            self.conn.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    def ensure_project(
        self,
        name: str,
        dev_bucket: Optional[str] = None,
        prod_bucket: Optional[str] = None,
    ) -> dict[str, Any]:
        return self.projects.ensure(name, dev_bucket=dev_bucket, prod_bucket=prod_bucket)

    def get_project(self, name: str) -> Optional[dict[str, Any]]:
        return self.projects.get(name)

    def provision_new_user(
        self, user_id: str, is_admin: bool = False, mode: str = "none"
    ) -> dict[str, Any]:
        """Create a user; unless an administrator or mode is "none", also give them a project.

        mode "auto" makes a dev-only home project, any other value joins the
        existing project of that name. Either becomes their default project.
        All or nothing: a user that cannot be provisioned is not created.
        """
        if is_admin or mode == "none":
            return self.users.create(user_id, is_admin=is_admin)
        now = time.time()
        with self.conn:
            self.conn.execute(
                "INSERT INTO users (id, is_admin, active, created_at) VALUES (?, 0, 1, ?)",
                (user_id, now),
            )
            if mode == "auto":
                project_id = self._free_home_name(user_id)
                self.conn.execute(
                    "INSERT INTO projects (id, name, prod_bucket, dev_bucket, dev_only, created_at) "
                    "VALUES (?, ?, '', ?, 1, ?)",
                    (project_id, project_id, f"seine-{project_id}-dev", now),
                )
            else:
                row = self.conn.execute(
                    "SELECT id FROM projects WHERE id = ? OR name = ?", (mode, mode)
                ).fetchone()
                if row is None:
                    raise ValueError(f"new_user_project '{mode}' is not an existing project")
                project_id = row["id"]
            self.conn.execute(
                "INSERT INTO project_members (project_id, user_id, role, created_at) "
                "VALUES (?, ?, 'developer', ?)",
                (project_id, user_id, now),
            )
            self.conn.execute(
                "UPDATE users SET default_project = ? WHERE id = ?", (project_id, user_id)
            )
        return self.users.get(user_id)  # type: ignore[return-value]

    def _free_home_name(self, user_id: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", user_id.lower()).strip("-")[:30] or "user"
        for n in range(1, 100):
            name = f"{HOME_PREFIX}{slug}" + (f"-{n}" if n > 1 else "")
            if not self.conn.execute(
                "SELECT 1 FROM projects WHERE id = ? OR name = ?", (name, name)
            ).fetchone():
                return name
        raise ValueError(f"no free home project name for user '{user_id}'")

    def upsert_worker(
        self,
        worker_id: str,
        hostname: str,
        native_arch: str,
        arch_scores: dict[str, float],
        free_disk_gb: float,
        token: str,
        concurrency_slots: int = 1,
    ) -> dict[str, Any]:
        return self.workers.register(
            id=worker_id,
            hostname=hostname,
            native_arch=native_arch,
            arch_scores=arch_scores,
            free_disk_gb=free_disk_gb,
            token=token,
            concurrency_slots=concurrency_slots,
        )

    def get_worker(self, worker_id: str) -> Optional[dict[str, Any]]:
        return self.workers.get(worker_id)

    def get_worker_by_token(self, token: str) -> Optional[dict[str, Any]]:
        return self.workers.get_by_token(token)

    def heartbeat_worker(self, worker_id: str, free_disk_gb: float) -> bool:
        return self.workers.heartbeat(worker_id, free_disk_gb)

    def create_build(
        self,
        build_id: str,
        project: str,
        target_arch: str,
        worktree_digest: str,
        spec_file: str = "spec.yaml",
        is_release: bool = False,
        options: Optional[dict[str, Any]] = None,
        user_id: Optional[str] = None,
        spec_files: Optional[list[str]] = None,
        spec_digest: str = "",
    ) -> str:
        self.ensure_project(project)
        self.builds.create(
            id=build_id,
            project=project,
            target_arch=target_arch,
            is_release=is_release,
            worktree_digest=worktree_digest,
            spec_file=spec_file,
            spec_files=spec_files,
            spec_digest=spec_digest,
            options=options,
            user_id=user_id,
        )
        job_id = f"job-{build_id}-img"
        self.builds.create_job(
            id=job_id,
            build_id=build_id,
            kind="image",
            target_arch=target_arch,
        )
        return build_id

    def create_build_with_fanout(
        self,
        build_id: str,
        project: str,
        spec: dict[str, Any],
        target_arch: str = "amd64",
        worktree_digest: str = "",
        spec_file: str = "spec.yaml",
        is_release: bool = False,
        options: Optional[dict[str, Any]] = None,
        cached_packages: Optional[Union[set[str], list[str], Callable[[str, str], bool]]] = None,
        user_id: Optional[str] = None,
        spec_files: Optional[list[str]] = None,
        spec_digest: str = "",
    ) -> str:
        self.ensure_project(project)
        self.builds.create(
            id=build_id,
            project=project,
            target_arch=target_arch,
            is_release=is_release,
            worktree_digest=worktree_digest,
            spec_file=spec_file,
            spec_files=spec_files,
            spec_digest=spec_digest,
            options=options,
            user_id=user_id,
        )
        self.scheduler.decompose_and_create_jobs(
            build_id=build_id,
            spec=spec,
            target_arch=target_arch,
            cached_packages=cached_packages,
        )
        return build_id

    def get_build(self, build_id: str) -> Optional[dict[str, Any]]:
        return self.builds.get(build_id)

    def find_build_by_spec(
        self, project: str, spec_digest: str, target_arch: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        return self.builds.latest_for_spec(project, spec_digest, target_arch)

    def claim_next_job(self, worker_id: str) -> Optional[dict[str, Any]]:
        return self.scheduler.claim_job(worker_id)

    def update_job_status(self, job_id: str, status: str, error_message: Optional[str] = None) -> None:
        self.builds.update_job_status(job_id, status, error_message=error_message)

