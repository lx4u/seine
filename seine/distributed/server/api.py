# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0
"""REST API and WebSocket server for distributed seine builds."""

from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Request,
    WebSocket,
    status,
)
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response

from seine.distributed.common.models import (
    BuildSubmitRequest,
    BuildSubmitResponse,
    ClaimJobRequest,
    HeartbeatRequest,
    JobManifest,
    JobStatusUpdateRequest,
    MemberAddRequest,
    ProjectCreateRequest,
    RegisterWorkerRequest,
    RegisterWorkerResponse,
    TokenIssueRequest,
    UserCreateRequest,
    UserUpdateRequest,
)
from seine.distributed.server import uploads, validation
from seine.distributed.server.auth import (
    current_user,
    current_worker,
    enrollment_token_ok,
    get_db,
    is_system_admin,
    load_project,
    member_projects,
    require_member,
    require_project_admin,
    require_system_admin,
    require_worker_id,
)
from seine.distributed.server.db import Database
from seine.distributed.server.reaper import Reaper
from seine.distributed.server.settings import S3_ENVIRONMENTS, Settings
from seine.distributed.server.storage import StorageCredentialsError, env_name, job_s3, provider_for
from seine.distributed.server.ws import BroadcastHub, forget_finished_build, serve_stream


def _get_db(request: Request) -> Database:
    if hasattr(request.app.state, "db") and request.app.state.db is not None:
        return request.app.state.db
    global _default_db
    if _default_db is None:
        db_path = os.environ.get("SEINE_DB_PATH", "seine.db")
        _default_db = Database(db_path)
    return _default_db


def _get_enrollment_token(request: Request) -> str:
    return request.app.state.enrollment_token or ""


def _get_transient_secrets(request: Request) -> dict[str, dict[str, Any]]:
    if not hasattr(request.app.state, "transient_secrets") or request.app.state.transient_secrets is None:
        request.app.state.transient_secrets = {}
    return request.app.state.transient_secrets


def _get_storage_provider(request: Request, project: str, bucket: str, env: str) -> Any:
    """Return the injected provider, else one using the project's key pair for env."""
    if hasattr(request.app.state, "storage_provider") and request.app.state.storage_provider is not None:
        return request.app.state.storage_provider
    try:
        return provider_for(request.app.state.settings, project, bucket, env)
    except StorageCredentialsError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e)) from e


def refuse_prod_for_dev_only(project_row: dict[str, Any], wants_prod: bool) -> None:
    """Raise 400 when prod storage or a release build is asked of a dev-only project."""
    if wants_prod and project_row["dev_only"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Project '{project_row['name']}' is dev-only: it has no prod storage and no release builds",
        )


def create_app(
    settings: Optional[Settings] = None,
    db: Optional[Database] = None,
    enrollment_token: Optional[str] = None,
    storage_provider: Optional[Any] = None,
    max_upload_bytes: Optional[int] = None,
    stale_after: Optional[float] = None,
    reap_interval: Optional[float] = None,
) -> FastAPI:
    """Create a seine-server FastAPI application; explicit arguments override settings."""
    settings = settings or Settings()
    owns_db = db is None
    if db is None:
        db = Database(settings.db_path)
    if stale_after is None:
        stale_after = settings.stale_after
    if reap_interval is None:
        reap_interval = settings.reap_interval

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        reaper = Reaper(app.state.db, stale_after, reap_interval)
        reaper.start()
        try:
            yield
        finally:
            reaper.stop()
            if owns_db:
                app.state.db.close()

    app = FastAPI(title="seine-server", version="0.1", lifespan=lifespan)

    app.state.db = db
    app.state.settings = settings
    app.state.enrollment_token = enrollment_token if enrollment_token is not None else settings.enrollment_token
    app.state.storage_provider = storage_provider
    app.state.max_upload_bytes = max_upload_bytes if max_upload_bytes is not None else settings.max_upload_bytes
    app.state.transient_secrets = {}
    app.state.hub = BroadcastHub()

    @app.post("/api/v1/workers/register", response_model=RegisterWorkerResponse)
    def register_worker(
        req: RegisterWorkerRequest,
        request: Request,
        authorization: Optional[str] = Header(None),
    ):
        expected_token = _get_enrollment_token(request)
        if not enrollment_token_ok(authorization, expected_token):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or missing enrollment token",
            )

        app_db = get_db(request)
        worker_token = secrets.token_hex(24)
        app_db.upsert_worker(
            worker_id=req.worker_id,
            hostname=req.hostname,
            native_arch=req.capabilities.native_arch,
            arch_scores=req.capabilities.arch_scores,
            free_disk_gb=req.capabilities.free_disk_gb,
            token=worker_token,
            concurrency_slots=req.capabilities.concurrency_slots,
        )
        return RegisterWorkerResponse(
            worker_id=req.worker_id,
            worker_token=worker_token,
            status="registered",
        )

    @app.post("/api/v1/workers/heartbeat")
    def worker_heartbeat(
        req: HeartbeatRequest,
        request: Request,
        worker: dict[str, Any] = Depends(current_worker),
    ):
        require_worker_id(worker, req.worker_id)
        app_db = get_db(request)
        app_db.heartbeat_worker(req.worker_id, req.free_disk_gb)
        return {
            "status": "ok",
            "cancel": app_db.scheduler.cancel_requested_jobs(req.worker_id),
        }

    @app.post("/api/v1/workers/claim")
    def claim_job(
        req: ClaimJobRequest,
        request: Request,
        worker: dict[str, Any] = Depends(current_worker),
    ):
        require_worker_id(worker, req.worker_id)
        app_db = get_db(request)
        job = app_db.scheduler.claim_job(req.worker_id)
        if not job:
            return Response(status_code=status.HTTP_204_NO_CONTENT)

        secrets_mgr = _get_transient_secrets(request)
        build_id = job.get("build_id", "")
        build = app_db.get_build(build_id) or {}
        try:
            job["s3"] = job_s3(
                request.app.state.settings, job["project"], job["s3_bucket"],
                env_name(build.get("is_release", False)),
            )
        except StorageCredentialsError as e:
            logger.warning("Failing job %s of build %s: %s", job["job_id"], build_id, e)
            app_db.update_job_status(job["job_id"], "failed", error_message=str(e))
            secrets_mgr.pop(build_id, None)
            forget_finished_build(app_db, request.app.state.hub, build_id)
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        job["transient_secrets"] = secrets_mgr.get(build_id, {})
        return JobManifest(**job)

    @app.post("/api/v1/workers/jobs/{job_id}/status")
    def update_job_status(
        job_id: str,
        req: JobStatusUpdateRequest,
        request: Request,
        worker: dict[str, Any] = Depends(current_worker),
    ):
        require_worker_id(worker, req.worker_id)
        app_db = get_db(request)
        if not app_db.scheduler.job_belongs_to(job_id, worker["id"]):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Job is not assigned to this worker",
            )
        if req.status not in _JOB_STATUSES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid job status '{req.status}'",
            )

        # The build comes from the job row, never from the request body.
        build_id = app_db.builds.get_job(job_id)["build_id"]
        build = app_db.get_build(build_id)
        prefix = f"artifacts/{build['project']}/{build_id}/"
        if any(not url.startswith(prefix) for url in req.artifact_urls):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Artifact keys must start with '{prefix}'",
            )

        try:
            artifact_meta = validation.check_artifacts(req.artifacts, prefix)
        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e

        app_db.update_job_status(job_id, req.status)
        if req.artifact_urls or artifact_meta:
            app_db.builds.update_status(
                build_id=build_id,
                status=req.status,
                artifact_urls=req.artifact_urls,
                artifact_meta=artifact_meta,
            )

        secrets_mgr = _get_transient_secrets(request)
        build = app_db.get_build(build_id)
        if req.status in ("completed", "failed", "cancelled") or (
            build and build.get("status") in ("completed", "failed", "cancelled")
        ):
            secrets_mgr.pop(build_id, None)
            forget_finished_build(app_db, request.app.state.hub, build_id)

        return {"status": "updated"}

    def upload_target(
        project: str,
        request: Request,
        env: str = "dev",
        token_record: dict[str, Any] = Depends(current_user),
    ) -> tuple[dict[str, Any], str]:
        app_db = get_db(request)
        project_row = load_project(app_db, project)
        if env not in S3_ENVIRONMENTS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unknown environment '{env}', expected dev or prod",
            )
        roles = ("releaser", "admin") if env == "prod" else None
        require_member(app_db, token_record, project, roles)
        refuse_prod_for_dev_only(project_row, env == "prod")
        return project_row, env

    def stage_worktree(
        request: Request, project: str, target: tuple[dict[str, Any], str],
        temp_path: str, total_bytes: int, digest: str,
    ) -> dict[str, Any]:
        project_row, env = target
        if total_bytes == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Uploaded worktree bundle is empty",
            )
        provider = _get_storage_provider(request, project, project_row[f"{env}_bucket"], env)
        try:
            if hasattr(provider, "ensure_bucket"):
                provider.ensure_bucket()
            pushed = provider.push_worktree(project, digest, temp_path)
        except Exception as e:
            logger.warning("Worktree staging failed for project %s: %s", project, e)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Storage error while staging worktree: {e}",
            )
        if pushed is False:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Storage refused the worktree bundle",
            )
        return {"digest": digest, "bytes": total_bytes, "status": "staged"}

    @app.post("/api/v1/projects/{project}/worktrees")
    async def upload_worktree(
        project: str,
        request: Request,
        target: tuple[dict[str, Any], str] = Depends(upload_target),
    ):
        limit = request.app.state.max_upload_bytes
        if request.headers.get("content-type", "").split(";")[0].strip() != "application/octet-stream":
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                detail="Send the worktree bundle as application/octet-stream",
            )
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > limit:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"Upload exceeds {limit} bytes",
            )

        hasher = hashlib.sha256()
        temp_path, total_bytes = await uploads.save_upload(request.stream(), hasher, limit)
        try:
            return await run_in_threadpool(
                stage_worktree, request, project, target,
                temp_path, total_bytes, hasher.hexdigest(),
            )
        finally:
            os.unlink(temp_path)

    @app.post("/api/v1/builds", response_model=BuildSubmitResponse)
    def submit_build(
        req: BuildSubmitRequest,
        request: Request,
        token_record: dict[str, Any] = Depends(current_user),
    ):
        app_db = get_db(request)
        load_project(app_db, req.project)
        roles = ("releaser", "admin") if req.is_release else ("developer", "releaser", "admin")
        require_member(app_db, token_record, req.project, roles)
        refuse_prod_for_dev_only(project_row, req.is_release)
        user_id = token_record["user_id"]

        build_id = f"bld-{uuid.uuid4().hex[:8]}"
        app_db.create_build(
            build_id=build_id,
            project=req.project,
            target_arch=req.target_arch,
            worktree_digest=req.worktree_digest,
            spec_file=req.spec_file,
            is_release=req.is_release,
            options=req.options,
            user_id=user_id,
        )

        if req.transient_secrets:
            secrets_mgr = _get_transient_secrets(request)
            secrets_mgr[build_id] = dict(req.transient_secrets)

        return BuildSubmitResponse(
            build_id=build_id,
            status="queued",
            project=req.project,
            target_arch=req.target_arch,
        )

    @app.get("/api/v1/builds/{build_id}")
    async def get_build_status(build_id: str, request: Request):
        app_db = _get_db(request)
        build = app_db.get_build(build_id)
        if not build:
            raise HTTPException(status_code=404, detail="Build not found")
        return build

    @app.get("/api/v1/builds")
    def list_builds(
        request: Request,
        project: Optional[str] = None,
        status: Optional[str] = None,
        token_record: dict[str, Any] = Depends(current_user),
    ):
        app_db = get_db(request)
        builds = app_db.builds.list(project=project, status=status)
        if is_system_admin(app_db, token_record):
            return builds
        allowed = member_projects(app_db, token_record["user_id"])
        return [b for b in builds if b["project"] in allowed]

    @app.post("/api/v1/builds/{build_id}/cancel")
    def cancel_build(
        build_id: str,
        request: Request,
        token_record: dict[str, Any] = Depends(current_user),
    ):
        app_db = get_db(request)
        build = app_db.get_build(build_id)
        if not build:
            raise HTTPException(status_code=404, detail="Build not found")
        if build.get("user_id") != token_record["user_id"]:
            require_project_admin(app_db, token_record, build["project"])
        if not app_db.scheduler.request_cancel(build_id):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Build already finished",
            )
        return {"status": "cancelling", "build_id": build_id}

    @app.websocket("/api/v1/builds/{build_id}/stream")
    async def websocket_stream(websocket: WebSocket, build_id: str):
        await serve_stream(websocket, websocket.app.state.hub, websocket.app.state.db, build_id)

    @app.get("/api/v1/projects")
    def list_projects(
        request: Request,
        tok: dict[str, Any] = Depends(current_user),
    ):
        app_db = get_db(request)
        projects = app_db.projects.list()
        if is_system_admin(app_db, tok):
            return projects
        # Bucket names are for administrators; members get no more than the name.
        mine = member_projects(app_db, tok["user_id"])
        return [
            {key: p[key] for key in ("id", "name", "created_at")}
            for p in projects if p["id"] in mine
        ]

    @app.post("/api/v1/projects", status_code=status.HTTP_201_CREATED)
    def create_project(
        req: ProjectCreateRequest,
        request: Request,
        tok: dict[str, Any] = Depends(current_user),
    ):
        app_db = get_db(request)
        require_system_admin(app_db, tok)
        try:
            proj = app_db.projects.create(
                name=req.name,
                dev_bucket=req.dev_bucket,
                prod_bucket=req.prod_bucket,
            )
        except sqlite3.IntegrityError:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Project '{req.name}' already exists",
            )
        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

        if req.provision_buckets:
            for bkt, env in ((proj["dev_bucket"], "dev"), (proj["prod_bucket"], "prod")):
                provider = _get_storage_provider(request, req.name, bkt, env)
                if hasattr(provider, "ensure_bucket"):
                    provider.ensure_bucket()
        return proj

    @app.delete("/api/v1/projects/{project}")
    def delete_project(
        project: str,
        request: Request,
        tok: dict[str, Any] = Depends(current_user),
    ):
        app_db = get_db(request)
        require_project_admin(app_db, tok, project)
        proj = app_db.projects.get(project)
        if not proj:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Project '{project}' not found",
            )
        app_db.projects.delete(project)
        return {"deleted": True, "project": project}

    @app.get("/api/v1/projects/{project}/members")
    def list_project_members(
        project: str,
        request: Request,
        tok: dict[str, Any] = Depends(current_user),
    ):
        app_db = get_db(request)
        proj = app_db.projects.get(project)
        if not proj:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Project '{project}' not found",
            )
        require_project_admin(app_db, tok, project)
        return app_db.projects.list_members(project)

    @app.post("/api/v1/projects/{project}/members")
    def add_project_member(
        project: str,
        req: MemberAddRequest,
        request: Request,
        tok: dict[str, Any] = Depends(current_user),
    ):
        app_db = get_db(request)
        proj = app_db.projects.get(project)
        if not proj:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Project '{project}' not found",
            )
        require_project_admin(app_db, tok, project)
        if app_db.users.get(req.user_id) is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"User '{req.user_id}' not found",
            )
        try:
            return app_db.projects.add_member(project, req.user_id, req.role)
        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    @app.delete("/api/v1/projects/{project}/members/{user_id}")
    def remove_project_member(
        project: str,
        user_id: str,
        request: Request,
        tok: dict[str, Any] = Depends(current_user),
    ):
        app_db = get_db(request)
        proj = app_db.projects.get(project)
        if not proj:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Project '{project}' not found",
            )
        require_project_admin(app_db, tok, project)
        ok = app_db.projects.remove_member(project, user_id)
        if not ok:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Member '{user_id}' not found in project '{project}'",
            )
        return {"removed": True, "project": project, "user_id": user_id}

    @app.get("/api/v1/tokens")
    def list_tokens(
        request: Request,
        user_id: Optional[str] = None,
        kind: Optional[str] = None,
        tok: dict[str, Any] = Depends(current_user),
    ):
        app_db = get_db(request)
        require_system_admin(app_db, tok)
        return app_db.tokens.list(user_id=user_id, kind=kind)

    @app.post("/api/v1/tokens")
    def issue_token(
        req: TokenIssueRequest,
        request: Request,
        tok: dict[str, Any] = Depends(current_user),
    ):
        app_db = get_db(request)
        require_system_admin(app_db, tok)
        if app_db.users.get(req.user_id) is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"User '{req.user_id}' not found",
            )
        expires_at = time.time() + (req.days * 86400.0) if req.days is not None else None
        return app_db.tokens.issue(user_id=req.user_id, kind=req.kind, expires_at=expires_at)

    @app.delete("/api/v1/tokens/{token_id}")
    def revoke_token(
        token_id: str,
        request: Request,
        tok: dict[str, Any] = Depends(current_user),
    ):
        app_db = get_db(request)
        require_system_admin(app_db, tok)
        if not app_db.tokens.revoke(token_id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Token not found")
        return {"revoked": True, "id": token_id}

    @app.get("/api/v1/users")
    def list_users(
        request: Request,
        tok: dict[str, Any] = Depends(current_user),
    ):
        app_db = get_db(request)
        require_system_admin(app_db, tok)
        return app_db.users.list()

    @app.post("/api/v1/users", status_code=status.HTTP_201_CREATED)
    def create_user(
        req: UserCreateRequest,
        request: Request,
        tok: dict[str, Any] = Depends(current_user),
    ):
        app_db = get_db(request)
        require_system_admin(app_db, tok)
        try:
            return app_db.provision_new_user(
                req.id, is_admin=req.is_admin, mode=settings.new_user_project)
        except sqlite3.IntegrityError:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"User '{req.id}' already exists",
            )
        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))

    @app.patch("/api/v1/users/{user_id}")
    def update_user(
        user_id: str,
        req: UserUpdateRequest,
        request: Request,
        tok: dict[str, Any] = Depends(current_user),
    ):
        app_db = get_db(request)
        require_system_admin(app_db, tok)
        try:
            user = app_db.users.update(user_id, is_admin=req.is_admin, active=req.active)
        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"User '{user_id}' not found",
            )
        return user

    return app

