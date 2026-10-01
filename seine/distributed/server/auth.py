# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0
"""FastAPI authentication and authorization helpers for seine-server."""

from __future__ import annotations

import hmac
from typing import Any, Iterable, Optional

from fastapi import Header, HTTPException, Request, status

from seine.distributed.server.db import Database, validate_project_name


def parse_bearer(authorization: Optional[str]) -> Optional[str]:
    """Return the token of a 'Bearer <token>' header, or None."""
    if not authorization or not authorization.startswith("Bearer "):
        return None
    return authorization.split("Bearer ", 1)[1].strip() or None


def enrollment_token_ok(authorization: Optional[str], expected: str) -> bool:
    """Return True if the header carries the enrollment token."""
    token = parse_bearer(authorization)
    return bool(expected) and token is not None and hmac.compare_digest(token.encode(), expected.encode())


def get_db(request: Request) -> Database:
    """Return the database of the app serving the request."""
    return request.app.state.db


def current_user(
    request: Request,
    authorization: Optional[str] = Header(None),
) -> dict[str, Any]:
    """Return the record of the valid user token, or raise 401."""
    token = parse_bearer(authorization)
    if token is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid authorization header",
        )
    db = get_db(request)
    record = db.tokens.validate(token)
    if not record:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired bearer token",
        )
    user = db.users.get(record["user_id"])
    if user is None or not user["active"]:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unknown or disabled user account",
        )
    return record


def current_worker(
    request: Request,
    authorization: Optional[str] = Header(None),
) -> dict[str, Any]:
    """Return the worker owning the bearer token, or raise 401/403."""
    token = parse_bearer(authorization)
    if token is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
        )
    worker = get_db(request).get_worker_by_token(token)
    if not worker:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid worker token",
        )
    return worker


def require_worker_id(worker: dict[str, Any], worker_id: str) -> None:
    """Raise 403 unless the request names the authenticated worker."""
    if worker["id"] != worker_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid worker token",
        )


def load_project(db: Database, name: str) -> dict[str, Any]:
    """Return the project row; raise 400 for a bad name and 404 if unknown."""
    try:
        validate_project_name(name)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    project = db.get_project(name)
    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project '{name}' not found",
        )
    return project


def is_system_admin(db: Database, token_record: dict[str, Any]) -> bool:
    """Return True if the token's user is an active system administrator."""
    user = db.users.get(token_record.get("user_id", ""))
    return bool(user and user["is_admin"] and user["active"])


def require_system_admin(db: Database, token_record: dict[str, Any]) -> None:
    """Raise 403 unless the token belongs to a system administrator."""
    if not is_system_admin(db, token_record):
        user_id = token_record.get("user_id", "")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"User '{user_id}' is not an administrator",
        )


def require_project_admin(db: Database, token_record: dict[str, Any], project: str) -> None:
    """Raise 403 unless the token is a project admin or system-wide admin."""
    if is_system_admin(db, token_record):
        return
    user_id = token_record.get("user_id", "")
    member = db.projects.get_member(project, user_id)
    if not member or member.get("role") != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"User '{user_id}' does not have admin role in project '{project}'",
        )


def require_member(
    db: Database,
    token_record: dict[str, Any],
    project: str,
    roles: Optional[Iterable[str]] = None,
) -> dict[str, Any]:
    """Return the token record if it may act on the project, else raise 403."""
    if is_system_admin(db, token_record):
        return token_record
    user_id = token_record.get("user_id", "")
    member = db.projects.get_member(project, user_id)
    if not member:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"User '{user_id}' is not a member of project '{project}'",
        )
    if roles is not None and member.get("role") not in tuple(roles):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"User '{user_id}' does not have the required role in project '{project}'",
        )
    return token_record


def member_projects(db: Database, user_id: str) -> set[str]:
    """Return the names of the projects the user is a member of."""
    rows = db.conn.execute(
        "SELECT project_id FROM project_members WHERE user_id = ?", (user_id,)
    ).fetchall()
    return {r["project_id"] for r in rows}
