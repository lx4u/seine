# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0
"""Remote administration client for projects, members, and access tokens."""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any, Optional

import requests


class AdminClient:
    """Client for managing projects, members, users, and tokens via REST API."""

    def __init__(
        self,
        server_url: Optional[str] = None,
        token: Optional[str] = None,
        timeout: float = 10.0,
        ca_cert: Optional[str] = None,
    ):
        self.server_url = (server_url or os.environ.get("SEINE_SERVER_URL", "http://localhost:8000")).rstrip("/")
        self.token = token or os.environ.get("SEINE_TOKEN")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.verify = ca_cert or True

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def project_create(
        self,
        name: str,
        dev_bucket: Optional[str] = None,
        prod_bucket: Optional[str] = None,
        provision_buckets: bool = False,
    ) -> dict[str, Any]:
        url = f"{self.server_url}/api/v1/projects"
        payload = {
            "name": name,
            "dev_bucket": dev_bucket,
            "prod_bucket": prod_bucket,
            "provision_buckets": provision_buckets,
        }
        resp = self.session.post(url, json=payload, headers=self._headers(), timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def project_list(self) -> list[dict[str, Any]]:
        url = f"{self.server_url}/api/v1/projects"
        resp = self.session.get(url, headers=self._headers(), timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def project_delete(self, name: str) -> dict[str, Any]:
        url = f"{self.server_url}/api/v1/projects/{name}"
        resp = self.session.delete(url, headers=self._headers(), timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def member_add(
        self,
        project: str,
        user_id: str,
        role: str = "developer",
    ) -> dict[str, Any]:
        url = f"{self.server_url}/api/v1/projects/{project}/members"
        payload = {"user_id": user_id, "role": role}
        resp = self.session.post(url, json=payload, headers=self._headers(), timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def member_remove(self, project: str, user_id: str) -> dict[str, Any]:
        url = f"{self.server_url}/api/v1/projects/{project}/members/{user_id}"
        resp = self.session.delete(url, headers=self._headers(), timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def member_list(self, project: str) -> list[dict[str, Any]]:
        url = f"{self.server_url}/api/v1/projects/{project}/members"
        resp = self.session.get(url, headers=self._headers(), timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def user_create(self, user_id: str, is_admin: bool = False) -> dict[str, Any]:
        url = f"{self.server_url}/api/v1/users"
        payload = {"id": user_id, "is_admin": is_admin}
        resp = self.session.post(url, json=payload, headers=self._headers(), timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def user_list(self) -> list[dict[str, Any]]:
        url = f"{self.server_url}/api/v1/users"
        resp = self.session.get(url, headers=self._headers(), timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def user_update(
        self,
        user_id: str,
        is_admin: Optional[bool] = None,
        active: Optional[bool] = None,
    ) -> dict[str, Any]:
        url = f"{self.server_url}/api/v1/users/{user_id}"
        payload = {"is_admin": is_admin, "active": active}
        resp = self.session.patch(url, json=payload, headers=self._headers(), timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def token_issue(
        self,
        user_id: str,
        kind: str = "pat",
        days: Optional[int] = None,
    ) -> dict[str, Any]:
        url = f"{self.server_url}/api/v1/tokens"
        payload = {"user_id": user_id, "kind": kind, "days": days}
        resp = self.session.post(url, json=payload, headers=self._headers(), timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def token_revoke(self, token_id: str) -> dict[str, Any]:
        url = f"{self.server_url}/api/v1/tokens/{token_id}"
        resp = self.session.delete(url, headers=self._headers(), timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def token_list(
        self,
        user_id: Optional[str] = None,
        kind: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        url = f"{self.server_url}/api/v1/tokens"
        params = {}
        if user_id:
            params["user_id"] = user_id
        if kind:
            params["kind"] = kind
        resp = self.session.get(url, params=params, headers=self._headers(), timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()


def run_client_admin(argv: list[str]) -> int:
    """Parse client CLI arguments and invoke remote administration API."""
    parser = argparse.ArgumentParser(prog="seine admin", description="Remote administration CLI")
    parser.add_argument("--server", default=None, help="Server URL (default: $SEINE_SERVER_URL or http://localhost:8000)")
    parser.add_argument("--token", default=None, help="Bearer token (default: $SEINE_TOKEN)")
    parser.add_argument("--ca-cert", metavar="PATH", default=os.environ.get("SEINE_CA_CERT"),
                        help="CA bundle to verify the server's TLS certificate with (default: $SEINE_CA_CERT)")

    subparsers = parser.add_subparsers(dest="command")

    p_parser = subparsers.add_parser("project", help="Project operations")
    p_sub = p_parser.add_subparsers(dest="action")

    p_create = p_sub.add_parser("create", help="Create project")
    p_create.add_argument("name", help="Project name")
    p_create.add_argument("--dev-bucket", default=None, help="Custom dev S3 bucket")
    p_create.add_argument("--prod-bucket", default=None, help="Custom prod S3 bucket")
    p_create.add_argument("--provision-buckets", action="store_true", help="Provision buckets on S3")

    p_sub.add_parser("list", help="List projects")

    p_del = p_sub.add_parser("delete", help="Delete project")
    p_del.add_argument("name", help="Project name")

    m_parser = subparsers.add_parser("member", help="Member operations")
    m_sub = m_parser.add_subparsers(dest="action")

    m_add = m_sub.add_parser("add", help="Add or update project member")
    m_add.add_argument("project", help="Project name")
    m_add.add_argument("user_id", help="User ID")
    m_add.add_argument("role", choices=["developer", "releaser", "admin"], help="Member role")

    m_remove = m_sub.add_parser("remove", help="Remove project member")
    m_remove.add_argument("project", help="Project name")
    m_remove.add_argument("user_id", help="User ID")

    m_list = m_sub.add_parser("list", help="List project members")
    m_list.add_argument("project", help="Project name")

    u_parser = subparsers.add_parser("user", help="User operations")
    u_sub = u_parser.add_subparsers(dest="action")

    u_create = u_sub.add_parser("create", help="Create user")
    u_create.add_argument("user_id", help="User ID")
    u_create.add_argument("--is-admin", action="store_true", help="Make the user a system administrator")

    u_sub.add_parser("list", help="List users")

    u_update = u_sub.add_parser("update", help="Update user")
    u_update.add_argument("user_id", help="User ID")
    u_update.add_argument("--active", action=argparse.BooleanOptionalAction, default=None, help="Enable or disable the user")
    u_update.add_argument("--is-admin", action=argparse.BooleanOptionalAction, default=None, help="Grant or revoke system administration")

    t_parser = subparsers.add_parser("token", help="Token operations")
    t_sub = t_parser.add_subparsers(dest="action")

    t_issue = t_sub.add_parser("issue", help="Issue token")
    t_issue.add_argument("user_id", help="User ID")
    t_issue.add_argument("--kind", choices=["pat"], default="pat", help="Token kind")
    t_issue.add_argument("--days", type=int, default=None, help="Expiration in days")

    t_revoke = t_sub.add_parser("revoke", help="Revoke token")
    t_revoke.add_argument("token_id", help="Token id to revoke")

    t_list = t_sub.add_parser("list", help="List tokens")
    t_list.add_argument("--user-id", default=None, help="Filter by user ID")
    t_list.add_argument("--kind", default=None, help="Filter by token kind")

    args = parser.parse_args(argv)
    client = AdminClient(server_url=args.server, token=args.token, ca_cert=args.ca_cert)

    try:
        if args.command == "project":
            if args.action == "create":
                proj = client.project_create(
                    name=args.name,
                    dev_bucket=args.dev_bucket,
                    prod_bucket=args.prod_bucket,
                    provision_buckets=args.provision_buckets,
                )
                print(f"Created project: {proj['name']} (dev: {proj['dev_bucket']}, prod: {proj['prod_bucket']})")
                return 0
            elif args.action == "list":
                projects = client.project_list()
                for p in projects:
                    buckets = f"\tdev={p['dev_bucket']}\tprod={p['prod_bucket']}" if "dev_bucket" in p else ""
                    print(f"{p['name']}{buckets}")
                return 0
            elif args.action == "delete":
                client.project_delete(args.name)
                print(f"Deleted project: {args.name}")
                return 0
            else:
                sys.stderr.write("error: missing or invalid project action (create, list, delete)\n")
                return 1

        elif args.command == "member":
            if args.action == "add":
                mem = client.member_add(args.project, args.user_id, args.role)
                print(f"Added member {mem['user_id']} ({mem['role']}) to {mem['project_id']}")
                return 0
            elif args.action == "remove":
                client.member_remove(args.project, args.user_id)
                print(f"Removed member {args.user_id} from {args.project}")
                return 0
            elif args.action == "list":
                members = client.member_list(args.project)
                for m in members:
                    print(f"{m['user_id']}\t{m['role']}")
                return 0
            else:
                sys.stderr.write("error: missing or invalid member action (add, remove, list)\n")
                return 1

        elif args.command == "user":
            if args.action == "create":
                user = client.user_create(args.user_id, is_admin=args.is_admin)
                print(f"Created user: {user['id']}{' (admin)' if user['is_admin'] else ''}")
                return 0
            elif args.action == "list":
                for u in client.user_list():
                    print(f"{u['id']}\tadmin={int(u['is_admin'])}\tactive={int(u['active'])}")
                return 0
            elif args.action == "update":
                user = client.user_update(args.user_id, is_admin=args.is_admin, active=args.active)
                print(f"Updated user: {user['id']} (admin={int(user['is_admin'])}, active={int(user['active'])})")
                return 0
            else:
                sys.stderr.write("error: missing or invalid user action (create, list, update)\n")
                return 1

        elif args.command == "token":
            if args.action == "issue":
                tok = client.token_issue(args.user_id, kind=args.kind, days=args.days)
                exp = f", expires: {tok['expires_at']}" if tok.get("expires_at") else ""
                print(f"Issued token {tok['id']} (user: {tok['user_id']}, kind: {tok['kind']}{exp})")
                print(f"Secret (shown only once): {tok['token']}")
                return 0
            elif args.action == "revoke":
                client.token_revoke(args.token_id)
                print(f"Revoked token: {args.token_id}")
                return 0
            elif args.action == "list":
                tokens = client.token_list(user_id=getattr(args, "user_id", None), kind=getattr(args, "kind", None))
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

    except requests.HTTPError as err:
        detail = None
        try:
            detail = err.response.json().get("detail")
        except Exception:
            pass
        msg = detail or str(err)
        sys.stderr.write(f"error: {msg}\n")
        return 1
    except requests.RequestException as err:
        sys.stderr.write(f"error: {err}\n")
        return 1
