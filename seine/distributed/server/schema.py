# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0
"""SQLite schema and in-place migrations for seine-server."""

from __future__ import annotations

import sqlite3


def init_db(conn: sqlite3.Connection) -> None:
    """Initialize relational database schema."""
    with conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY,
            name TEXT UNIQUE NOT NULL,
            prod_bucket TEXT NOT NULL,
            dev_bucket TEXT NOT NULL,
            dev_only INTEGER NOT NULL DEFAULT 0,
            quota_gb REAL,
            created_at REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS project_members (
            project_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('developer', 'releaser', 'admin')),
            created_at REAL NOT NULL,
            PRIMARY KEY (project_id, user_id),
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS builds (
            id TEXT PRIMARY KEY,
            project TEXT NOT NULL,
            target_arch TEXT NOT NULL,
            is_release INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'queued',
            worktree_digest TEXT NOT NULL DEFAULT '',
            spec_file TEXT NOT NULL DEFAULT 'spec.yaml',
            spec_files TEXT NOT NULL DEFAULT '[]',
            spec_digest TEXT NOT NULL DEFAULT '',
            options TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL,
            started_at REAL,
            finished_at REAL,
            artifact_urls TEXT NOT NULL DEFAULT '[]',
            artifact_meta TEXT NOT NULL DEFAULT '[]',
            error_message TEXT,
            user_id TEXT,
            artifacts_expired_at REAL,
            artifacts_expired_reason TEXT,
            FOREIGN KEY (project) REFERENCES projects(name) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS workers (
            id TEXT PRIMARY KEY,
            hostname TEXT NOT NULL,
            native_arch TEXT NOT NULL,
            arch_scores TEXT NOT NULL DEFAULT '{}',
            free_disk_gb REAL NOT NULL DEFAULT 0.0,
            concurrency_slots INTEGER NOT NULL DEFAULT 1,
            token_hash TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'online',
            last_seen REAL NOT NULL,
            created_at REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            build_id TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'image',
            target_arch TEXT NOT NULL,
            package_name TEXT,
            worker_id TEXT,
            status TEXT NOT NULL DEFAULT 'queued',
            created_at REAL NOT NULL,
            cancel_requested INTEGER NOT NULL DEFAULT 0,
            attempts INTEGER NOT NULL DEFAULT 0,
            started_at REAL,
            finished_at REAL,
            FOREIGN KEY (build_id) REFERENCES builds(id) ON DELETE CASCADE,
            FOREIGN KEY (worker_id) REFERENCES workers(id) ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            is_admin INTEGER NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1,
            created_at REAL,
            default_project TEXT REFERENCES projects(id) ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS tokens (
            id TEXT PRIMARY KEY,
            token_hash TEXT NOT NULL UNIQUE,
            user_id TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'pat' CHECK (kind IN ('pat', 'admin', 'worker')),
            created_at REAL NOT NULL,
            expires_at REAL
        );

        CREATE INDEX IF NOT EXISTS idx_project_members_user ON project_members(user_id);
        CREATE INDEX IF NOT EXISTS idx_builds_project ON builds(project);
        CREATE INDEX IF NOT EXISTS idx_jobs_build_id ON jobs(build_id);
        CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
        CREATE INDEX IF NOT EXISTS idx_workers_token ON workers(token_hash);
        CREATE INDEX IF NOT EXISTS idx_tokens_user_id ON tokens(user_id);
        """)
        try:
            conn.execute(
                "ALTER TABLE workers ADD COLUMN concurrency_slots INTEGER NOT NULL DEFAULT 1"
            )
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE projects ADD COLUMN dev_only INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute(
                "ALTER TABLE users ADD COLUMN default_project "
                "TEXT REFERENCES projects(id) ON DELETE SET NULL"
            )
        except sqlite3.OperationalError:
            pass
        for stmt in (
            "ALTER TABLE projects ADD COLUMN quota_gb REAL",
            "ALTER TABLE builds ADD COLUMN artifacts_expired_at REAL",
            "ALTER TABLE builds ADD COLUMN artifacts_expired_reason TEXT",
            "ALTER TABLE builds ADD COLUMN spec_files TEXT NOT NULL DEFAULT '[]'",
            "ALTER TABLE builds ADD COLUMN spec_digest TEXT NOT NULL DEFAULT ''",
        ):
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError:
                pass
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_builds_project_spec_digest "
            "ON builds(project, spec_digest, created_at DESC)"
        )
