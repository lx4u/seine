# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0
"""Server-side administrative commands operating directly on Database."""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from typing import Any, Optional

from seine.distributed.common.models import format_quota
from seine.distributed.server.db import Database
from seine.distributed.server.settings import Settings
from seine.distributed.server.storage import provider_for


def project_create(
    db: Database,
    name: str,
    dev_bucket: Optional[str] = None,
    prod_bucket: Optional[str] = None,
    provision_buckets: bool = False,
    storage_provider: Optional[Any] = None,
    settings: Optional[Settings] = None,
) -> dict[str, Any]:
    """Create a project and optionally provision its S3 buckets."""
    proj = db.projects.create(name=name, dev_bucket=dev_bucket, prod_bucket=prod_bucket)
    if provision_buckets:
        for bkt, env in ((proj["dev_bucket"], "dev"), (proj["prod_bucket"], "prod")):
            if storage_provider is not None:
                provider = storage_provider
            else:
                provider = provider_for(settings or Settings(), name, bkt, env)
            if hasattr(provider, "ensure_bucket"):
                provider.ensure_bucket()
    return proj


def project_list(db: Database) -> list[dict[str, Any]]:
    return db.projects.list()


def project_update(db: Database, name: str, **fields: Any) -> Optional[dict[str, Any]]:
    """Apply the given fields to a project; None when it does not exist."""
    if "quota_gb" in fields:
        db.projects.set_quota(name, fields["quota_gb"])
    return db.projects.get(name)


def project_delete(db: Database, name: str) -> bool:
    return db.projects.delete(name)


def _require_user(db: Database, user_id: str) -> None:
    if db.users.get(user_id) is None:
        raise ValueError(f"user '{user_id}' not found")


def user_create(
    db: Database, user_id: str, is_admin: bool = False, new_user_project: str = "none"
) -> dict[str, Any]:
    """Create a user account, with the project that new_user_project calls for."""
    try:
        return db.provision_new_user(user_id, is_admin=is_admin, mode=new_user_project)
    except sqlite3.IntegrityError:
        raise ValueError(f"user '{user_id}' already exists")


def user_list(db: Database) -> list[dict[str, Any]]:
    return db.users.list()


def user_update(
    db: Database,
    user_id: str,
    is_admin: Optional[bool] = None,
    active: Optional[bool] = None,
) -> dict[str, Any]:
    """Change a user's flags; refuses to drop the last active administrator."""
    user = db.users.update(user_id, is_admin=is_admin, active=active)
    if user is None:
        raise ValueError(f"user '{user_id}' not found")
    return user


def user_purge(
    db: Database, user_id: str, anonymize: bool = False, delete_home: bool = False
) -> dict[str, Any]:
    """Erase a user; the buckets of a deleted home project are left alone."""
    res = db.purge_user(user_id, anonymize=anonymize, delete_home=delete_home)
    if res is None:
        raise ValueError(f"user '{user_id}' not found")
    return res


def member_add(db: Database, project: str, user_id: str, role: str) -> dict[str, Any]:
    _require_user(db, user_id)
    return db.projects.add_member(project, user_id, role)


def member_remove(db: Database, project: str, user_id: str) -> bool:
    return db.projects.remove_member(project, user_id)


def member_list(db: Database, project: str) -> list[dict[str, Any]]:
    return db.projects.list_members(project)


def token_issue(
    db: Database,
    user_id: str,
    kind: str = "pat",
    days: Optional[int] = None,
) -> dict[str, Any]:
    """Issue a personal access token for an existing user."""
    if kind != "pat":
        raise ValueError(f"cannot issue tokens of kind '{kind}'")
    _require_user(db, user_id)
    expires_at = time.time() + (days * 86400.0) if days is not None else None
    return db.tokens.issue(user_id=user_id, kind=kind, expires_at=expires_at)


def token_revoke(db: Database, token_id: str) -> bool:
    return db.tokens.revoke(token_id)


def token_list(
    db: Database,
    user_id: Optional[str] = None,
    kind: Optional[str] = None,
) -> list[dict[str, Any]]:
    """List issued tokens with optional filters."""
    return db.tokens.list(user_id=user_id, kind=kind)


def setup_admin_subparsers(admin_parser: argparse.ArgumentParser) -> None:
    """Register project, member, user, and token subcommands on admin_parser."""
    admin_sub = admin_parser.add_subparsers(dest="admin_command")

    p_parser = admin_sub.add_parser("project", help="Project operations")
    p_sub = p_parser.add_subparsers(dest="project_action")

    p_create = p_sub.add_parser("create", help="Create project")
    p_create.add_argument("name", help="Project name")
    p_create.add_argument("--dev-bucket", default=None, help="Custom dev S3 bucket")
    p_create.add_argument("--prod-bucket", default=None, help="Custom prod S3 bucket")
    p_create.add_argument("--provision-buckets", action="store_true", help="Provision buckets on S3")

    p_sub.add_parser("list", help="List projects")

    p_upd = p_sub.add_parser("update", help="Update project settings")
    p_upd.add_argument("name", help="Project name")
    quota = p_upd.add_mutually_exclusive_group()
    quota.add_argument("--quota-gb", type=float, default=None, help="Storage quota in GB")
    quota.add_argument("--no-quota", action="store_true", help="Remove the storage quota")

    p_del = p_sub.add_parser("delete", help="Delete project")
    p_del.add_argument("name", help="Project name")

    m_parser = admin_sub.add_parser("member", help="Member operations")
    m_sub = m_parser.add_subparsers(dest="member_action")

    m_add = m_sub.add_parser("add", help="Add or update project member")
    m_add.add_argument("project", help="Project name")
    m_add.add_argument("user_id", help="User ID")
    m_add.add_argument("role", choices=["developer", "releaser", "admin"], help="Member role")

    m_remove = m_sub.add_parser("remove", help="Remove project member")
    m_remove.add_argument("project", help="Project name")
    m_remove.add_argument("user_id", help="User ID")

    m_list = m_sub.add_parser("list", help="List project members")
    m_list.add_argument("project", help="Project name")

    u_parser = admin_sub.add_parser("user", help="User operations")
    u_sub = u_parser.add_subparsers(dest="user_action")

    u_create = u_sub.add_parser("create", help="Create user")
    u_create.add_argument("user_id", help="User ID")
    u_create.add_argument("--is-admin", action="store_true", help="Make the user a system administrator")

    u_sub.add_parser("list", help="List users")

    u_update = u_sub.add_parser("update", help="Update user")
    u_update.add_argument("user_id", help="User ID")
    u_update.add_argument("--active", action=argparse.BooleanOptionalAction, default=None, help="Enable or disable the user")
    u_update.add_argument("--is-admin", action=argparse.BooleanOptionalAction, default=None, help="Grant or revoke system administration")

    u_delete = u_sub.add_parser("delete", help="Delete a user for good")
    u_delete.add_argument("user_id", help="User ID")
    u_delete.add_argument("--purge", action="store_true", help="Confirm the permanent erasure")
    u_delete.add_argument("--anonymize", action="store_true", help='Rename the user on their builds to "deleted-user"')
    u_delete.add_argument("--delete-home", action="store_true", help="Also delete their home project and its bucket")

    t_parser = admin_sub.add_parser("token", help="Token operations")
    t_sub = t_parser.add_subparsers(dest="token_action")

    t_issue = t_sub.add_parser("issue", help="Issue token")
    t_issue.add_argument("user_id", help="User ID")
    t_issue.add_argument("--kind", choices=["pat"], default="pat", help="Token kind")
    t_issue.add_argument("--days", type=int, default=None, help="Expiration in days")

    t_revoke = t_sub.add_parser("revoke", help="Revoke token")
    t_revoke.add_argument("token_id", help="Token id to revoke")

    t_list = t_sub.add_parser("list", help="List tokens")
    t_list.add_argument("--user-id", default=None, help="Filter by user ID")
    t_list.add_argument("--kind", default=None, help="Filter by token kind")


def handle_admin_command(args: argparse.Namespace, db: Optional[Database] = None) -> int:
    """Execute parsed admin command against Database."""
    close_db = False
    if db is None:
        db_path = getattr(args, "db_path", None) or os.environ.get("SEINE_DB_PATH", "seine.db")
        db = Database(db_path)
        close_db = True

    try:
        cmd = getattr(args, "admin_command", None)
        if cmd == "project":
            act = getattr(args, "project_action", None)
            if act == "create":
                proj = project_create(
                    db,
                    args.name,
                    dev_bucket=args.dev_bucket,
                    prod_bucket=args.prod_bucket,
                    provision_buckets=args.provision_buckets,
                    settings=Settings.load() if args.provision_buckets else None,
                )
                print(f"Created project: {proj['name']} (dev: {proj['dev_bucket']}, prod: {proj['prod_bucket']})")
                return 0
            elif act == "list":
                projects = project_list(db)
                for p in projects:
                    print(f"{p['name']}\tdev={p['dev_bucket']}\tprod={p['prod_bucket']}\tquota={format_quota(p)}")
                return 0
            elif act == "update":
                if args.no_quota:
                    fields = {"quota_gb": None}
                elif args.quota_gb is not None:
                    fields = {"quota_gb": args.quota_gb}
                else:
                    sys.stderr.write("error: nothing to update (use --quota-gb or --no-quota)\n")
                    return 1
                try:
                    proj = project_update(db, args.name, **fields)
                except ValueError as e:
                    sys.stderr.write(f"error: {e}\n")
                    return 1
                if proj is None:
                    sys.stderr.write(f"error: project '{args.name}' not found\n")
                    return 1
                print(f"Updated project: {proj['name']} (quota: {format_quota(proj)})")
                return 0
            elif act == "delete":
                ok = project_delete(db, args.name)
                if ok:
                    print(f"Deleted project: {args.name}")
                    return 0
                else:
                    sys.stderr.write(f"error: project '{args.name}' not found\n")
                    return 1
            else:
                sys.stderr.write("error: missing or invalid project action (create, list, update, delete)\n")
                return 1

        elif cmd == "member":
            act = getattr(args, "member_action", None)
            if act == "add":
                mem = member_add(db, args.project, args.user_id, args.role)
                print(f"Added member {mem['user_id']} ({mem['role']}) to {mem['project_id']}")
                return 0
            elif act == "remove":
                ok = member_remove(db, args.project, args.user_id)
                if ok:
                    print(f"Removed member {args.user_id} from {args.project}")
                    return 0
                else:
                    sys.stderr.write(f"error: member '{args.user_id}' not found in project '{args.project}'\n")
                    return 1
            elif act == "list":
                members = member_list(db, args.project)
                for m in members:
                    print(f"{m['user_id']}\t{m['role']}")
                return 0
            else:
                sys.stderr.write("error: missing or invalid member action (add, remove, list)\n")
                return 1

        elif cmd == "user":
            act = getattr(args, "user_action", None)
            if act == "create":
                user = user_create(db, args.user_id, is_admin=args.is_admin,
                                   new_user_project=Settings.load().new_user_project)
                project = f" (default project: {user['default_project']})" if user["default_project"] else ""
                print(f"Created user: {user['id']}{' (admin)' if user['is_admin'] else ''}{project}")
                return 0
            elif act == "list":
                for u in user_list(db):
                    print(f"{u['id']}\tadmin={int(u['is_admin'])}\tactive={int(u['active'])}")
                return 0
            elif act == "update":
                user = user_update(db, args.user_id, is_admin=args.is_admin, active=args.active)
                print(f"Updated user: {user['id']} (admin={int(user['is_admin'])}, active={int(user['active'])})")
                return 0
            elif act == "delete":
                if not args.purge:
                    sys.stderr.write("error: user deletion requires --purge (use 'user update --no-active' to disable an account)\n")
                    return 1
                u = user_purge(db, args.user_id, anonymize=args.anonymize, delete_home=args.delete_home)
                print(f"Purged user: {u['id']} (uid: {u['uid']}, deleted {u['deleted_tokens']} tokens, removed {u['removed_memberships']} memberships)")
                if u["deleted_home"]:
                    print(f"Project {u['deleted_home']} deleted; its bucket was not emptied")
                return 0
            else:
                sys.stderr.write("error: missing or invalid user action (create, list, update, delete)\n")
                return 1

        elif cmd == "token":
            act = getattr(args, "token_action", None)
            if act == "issue":
                tok = token_issue(db, args.user_id, kind=args.kind, days=args.days)
                exp = f", expires: {tok['expires_at']}" if tok.get("expires_at") else ""
                print(f"Issued token {tok['id']} (user: {tok['user_id']}, kind: {tok['kind']}{exp})")
                print(f"Secret (shown only once): {tok['token']}")
                return 0
            elif act == "revoke":
                ok = token_revoke(db, args.token_id)
                if ok:
                    print(f"Revoked token: {args.token_id}")
                    return 0
                else:
                    sys.stderr.write(f"error: token not found\n")
                    return 1
            elif act == "list":
                tokens = token_list(db, user_id=getattr(args, "user_id", None), kind=getattr(args, "kind", None))
                for t in tokens:
                    exp = f"\texpires={t['expires_at']}" if t.get("expires_at") else ""
                    print(f"{t['id']}\tuser={t['user_id']}\tkind={t['kind']}{exp}")
                return 0
            else:
                sys.stderr.write("error: missing or invalid token action (issue, revoke, list)\n")
                return 1

        else:
            sys.stderr.write("error: missing or invalid admin command (project, member, user, token)\n")
            return 1

    except ValueError as err:
        sys.stderr.write(f"error: {err}\n")
        return 1
    finally:
        if close_db:
            db.close()


def run_server_admin(argv: list[str], db: Optional[Database] = None) -> int:
    """Parse argv and execute local server admin command."""
    parser = argparse.ArgumentParser(prog="seine-server admin", description="Server admin CLI")
    parser.add_argument("--db-path", default=None, help="Database path")
    setup_admin_subparsers(parser)
    args = parser.parse_args(argv)
    return handle_admin_command(args, db=db)
