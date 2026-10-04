# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0
"""REST API and WebSocket server for distributed seine builds."""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
import sqlite3
import time
import uuid
from urllib.parse import quote
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
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, StreamingResponse

from seine.distributed.common.models import (
    BuildResponse,
    BuildSubmitRequest,
    BuildSubmitResponse,
    ClaimJobRequest,
    HeartbeatRequest,
    JobArtifactory,
    JobManifest,
    JobStatusUpdateRequest,
    MemberAddRequest,
    ProjectCreateRequest,
    ProjectUpdateRequest,
    RegisterWorkerRequest,
    RegisterWorkerResponse,
    StorageGcRequest,
    StorageInfo,
    TokenIssueRequest,
    UserCreateRequest,
    UserPreferencesRequest,
    UserProfileResponse,
    UserUpdateRequest,
    WorkerPauseRequest,
    WorkerRosterItem,
    WorkerRosterResponse,
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
from seine.distributed.server.db import Database, LastAdminError, UserBusyError
from seine.distributed.server.housekeeping import HousekeepingBusy, run_housekeeping
from seine.distributed.server.reaper import Reaper
from seine.distributed.server.settings import S3_ENVIRONMENTS, Settings
from seine.distributed.server.storage import (
    StorageCredentialsError, check_job_credentials, env_name, job_storage, provider_for, storage_type,
)
from seine.distributed.server.transient import TransientSecrets
from seine.distributed.server.events import ProjectEvents, build_event, serve_events
from seine.distributed.server.ws import BroadcastHub, forget_finished_build, serve_stream
from seine.storage.artifactory.client import ArtifactoryError
from seine.storage.s3.client import S3ClientError

logger = logging.getLogger("seine.server.api")

_JOB_STATUSES = ("running", "completed", "failed", "cancelled")
_TERMINAL_STATES = ("completed", "failed", "cancelled")
MAX_HEARTBEAT_JOBS = 100
MAX_JOB_ID_LENGTH = 128


def _clean_error(message: Optional[str]) -> Optional[str]:
    """Make a worker-supplied failure reason safe to store and print."""
    text = "".join(c for c in " ".join((message or "").split()) if c.isprintable())
    return text[:1000] or None


def _get_enrollment_token(request: Request) -> str:
    return request.app.state.enrollment_token or ""


def _get_transient_secrets(request: Request) -> TransientSecrets:
    return request.app.state.transient_secrets


def _get_storage_provider(request: Request, project: str, bucket: str, env: str) -> Any:
    """Return the injected provider, else one using the project's key pair for env."""
    if hasattr(request.app.state, "storage_provider") and request.app.state.storage_provider is not None:
        return request.app.state.storage_provider
    try:
        return provider_for(request.app.state.settings, project, bucket, env)
    except StorageCredentialsError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e)) from e


def _purge_project_storage(settings: Settings, proj: dict[str, Any]) -> list[dict[str, Any]]:
    """Empty the dev and prod buckets of a project; 502 on the first failure."""
    purged = []
    for env in S3_ENVIRONMENTS:
        bucket = proj.get(f"{env}_bucket")
        if not bucket:
            continue
        try:
            count, size = provider_for(settings, proj["name"], bucket, env).purge()
        except (StorageCredentialsError, S3ClientError) as e:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"could not empty bucket '{bucket}': {e}",
            ) from e
        purged.append({"env": env, "bucket": bucket, "objects": count, "bytes": size})
    return purged


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
    db.scheduler.native_grace = settings.native_grace
    db.scheduler.stale_after = stale_after
    db.scheduler.job_lost_grace = settings.job_lost_grace

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        reaper = Reaper(app.state.db, stale_after, reap_interval, app.state.transient_secrets, settings)
        app.state.reaper = reaper
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
    app.state.transient_secrets = TransientSecrets(settings.secret_ttl)
    app.state.hub = BroadcastHub()
    app.state.events = ProjectEvents()
    db.builds.listeners.append(
        lambda build: app.state.events.publish(build["project"], build_event(build)))

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError):
        # The default answer echoes the rejected input, secrets included.
        errors = [{"loc": e["loc"], "msg": e["msg"], "type": e["type"]} for e in exc.errors()]
        return JSONResponse(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, content={"detail": errors})

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
        running = req.running_jobs
        if running is not None and (
            len(running) > MAX_HEARTBEAT_JOBS or any(len(j) > MAX_JOB_ID_LENGTH for j in running)
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid running_jobs"
            )
        app_db = get_db(request)
        app_db.heartbeat_worker(req.worker_id, req.free_disk_gb)
        if running is not None:
            app_db.scheduler.reconcile_worker_jobs(req.worker_id, running)
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
        settings = request.app.state.settings
        secrets = dict(secrets_mgr.get(build_id, {}))
        own = secrets.pop("artifactory", None)
        try:
            if storage_type(settings) == "artifactory" and own:
                # The build brought its own credential: it runs as its user, not as this server.
                job["storage"] = JobArtifactory(
                    endpoint=settings.artifactory_endpoint, bucket=job["s3_bucket"], **own)
            elif storage_type(settings) == "artifactory" and settings.artifactory_job_tokens == "required":
                raise StorageCredentialsError(
                    "this server requires a build to bring its own Artifactory token and this one "
                    "has none (the server restarted, or the secret expired while it was queued)")
            else:
                job["storage"] = job_storage(
                    settings, job["project"], job["s3_bucket"],
                    env_name(build.get("is_release", False)),
                )
        except StorageCredentialsError as e:
            logger.warning("Failing job %s of build %s: %s", job["job_id"], build_id, e)
            app_db.update_job_status(job["job_id"], "failed", error_message=str(e))
            secrets_mgr.pop(build_id, None)
            forget_finished_build(app_db, request.app.state.hub, build_id)
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        job["transient_secrets"] = secrets
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

        error = _clean_error(req.error_message) if req.status == "failed" else None
        app_db.update_job_status(job_id, req.status, error_message=error)
        if req.artifact_urls or artifact_meta:
            app_db.builds.update_status(
                build_id=build_id,
                status=req.status,
                artifact_urls=req.artifact_urls,
                artifact_meta=artifact_meta,
            )

        secrets_mgr = _get_transient_secrets(request)
        build = app_db.get_build(build_id)
        if build and build.get("status") in _TERMINAL_STATES:
            secrets_mgr.pop(build_id, None)
            forget_finished_build(app_db, request.app.state.hub, build_id)
            reaper = getattr(request.app.state, "reaper", None)
            if reaper is not None:
                reaper.request_housekeeping(build["project"])

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

    def refresh_worktree(provider: Any, project: str, digest: str) -> None:
        """Restart the expiry age of a staged worktree; a failure is logged, never raised."""
        try:
            provider.refresh_worktree(project, digest)
        except Exception as e:
            logger.warning("Could not refresh worktree %s of %s: %s", digest, project, e)

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
        refresh_worktree(provider, project, digest)
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
        project_row = load_project(app_db, req.project)
        roles = ("releaser", "admin") if req.is_release else ("developer", "releaser", "admin")
        require_member(app_db, token_record, req.project, roles)
        refuse_prod_for_dev_only(project_row, req.is_release)
        user_id = token_record["user_id"]
        try:
            validation.check_build_options(req.options)
            validation.check_transient_secrets(req.transient_secrets)
        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e

        env = env_name(req.is_release)
        settings = request.app.state.settings
        own = req.transient_secrets.get("artifactory")
        if storage_type(settings) != "artifactory":
            req.transient_secrets.pop("artifactory", None)
        elif own is None and settings.artifactory_job_tokens == "required":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="this server requires your own Artifactory token: set SEINE_ARTIFACTORY_TOKEN "
                       "(or save one in the keyring or the credentials file) and submit again")
        elif own is not None:
            try:
                check_job_credentials(settings, project_row[f"{env}_bucket"], own)
            except ArtifactoryError as e:
                refused = e.status_code in (401, 403)
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN if refused else status.HTTP_502_BAD_GATEWAY,
                    detail=("your Artifactory credential cannot read and write repo "
                            f"'{project_row[f'{env}_bucket']}': {e}") if refused
                    else f"Artifactory check failed: {e}") from e
        provider = _get_storage_provider(request, req.project, project_row[f"{env}_bucket"], env)
        try:
            staged = provider.has_worktree(req.project, req.worktree_digest)
        except Exception as e:
            logger.warning("Worktree check failed for project %s: %s", req.project, e)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Storage error while checking the worktree: {e}",
            ) from e
        if not staged:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"worktree {req.worktree_digest} is not staged for {env} builds",
            )
        refresh_worktree(provider, req.project, req.worktree_digest)

        build_id = f"bld-{uuid.uuid4().hex[:8]}"
        app_db.create_build(
            build_id=build_id,
            project=req.project,
            target_arch=req.target_arch,
            worktree_digest=req.worktree_digest,
            spec_file=(req.spec_files or [req.spec_file])[0],
            spec_files=req.spec_files or [req.spec_file],
            spec_digest=req.spec_digest,
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

    def build_bucket(app_db: Any, build: dict[str, Any]) -> str:
        """The bucket (repo) a build keeps its worktree and artifacts in."""
        project = build["project"]
        project_row = app_db.get_project(project)
        is_release = build.get("is_release", False)
        if project_row:
            return project_row["prod_bucket"] if is_release else project_row["dev_bucket"]
        return f"seine-{project}-prod" if is_release else f"seine-{project}-dev"

    def proxied_downloads(request: Request) -> bool:
        """True when clients fetch artifacts through this server (Artifactory, 'proxy')."""
        settings = request.app.state.settings
        return storage_type(settings) == "artifactory" and settings.artifactory_downloads == "proxy"

    def build_response(request: Request, build: dict[str, Any]) -> BuildResponse:
        app_db = get_db(request)
        download_urls: dict[str, str] = {}
        manifest = build.get("artifact_meta") or []
        expired = bool(build.get("artifacts_expired_at"))
        if build.get("status") == "completed" and manifest and not expired:
            project = build["project"]
            is_release = build.get("is_release", False)
            bucket = build_bucket(app_db, build)

            try:
                provider = _get_storage_provider(request, project, bucket, env_name(is_release))
                for entry in manifest:
                    name, artifact_key = entry["name"], entry["key"]
                    try:
                        if proxied_downloads(request):
                            # Relative: the client adds its server's address and its own token.
                            url = f"/api/v1/builds/{build['id']}/artifacts/{quote(name, safe='')}"
                        else:
                            url = provider.generate_download_url(project, artifact_key)
                        if url:
                            download_urls[name] = url
                    except Exception as e:
                        logger.warning(
                            "Failed to generate download URL for artifact %s in project %s: %s",
                            artifact_key, project, e,
                        )
            except Exception as e:
                logger.warning(
                    "Failed to resolve storage provider for project %s: %s",
                    project, e,
                )

        build_data = dict(build)
        build_data["download_urls"] = download_urls
        build_data["artifacts"] = [
            {"name": m["name"], "size": m["size"], "sha256": m["sha256"],
             "subdir": m.get("subdir")} for m in manifest
        ]
        return BuildResponse(**build_data)

    @app.get("/api/v1/builds/{build_id}/artifacts/{name}")
    def download_artifact(
        build_id: str,
        name: str,
        request: Request,
        token_record: dict[str, Any] = Depends(current_user),
    ):
        """Stream one artifact of a build, for backends whose storage URLs need credentials."""
        app_db = get_db(request)
        build = app_db.get_build(build_id)
        if not build:
            raise HTTPException(status_code=404, detail="Build not found")
        require_member(app_db, token_record, build["project"])
        if build.get("artifacts_expired_at"):
            raise HTTPException(status_code=410, detail="artifacts expired")
        entry = next((m for m in build.get("artifact_meta") or [] if m["name"] == name), None)
        if build.get("status") != "completed" or entry is None:
            raise HTTPException(status_code=404, detail="Artifact not found")
        provider = _get_storage_provider(
            request, build["project"], build_bucket(app_db, build),
            env_name(build.get("is_release", False)))
        if not hasattr(provider, "open_artifact"):
            raise HTTPException(status_code=501, detail="this storage backend has no download proxy")
        try:
            upstream = provider.open_artifact(build["project"], entry["key"])
        except Exception as e:
            logger.warning("Artifact %s of build %s: %s", name, build_id, e)
            gone = getattr(e, "status_code", None) == 404
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND if gone else status.HTTP_502_BAD_GATEWAY,
                detail="artifact not found in storage" if gone else "storage error") from e

        def chunks():
            try:
                yield from upstream.iter_content(chunk_size=1 << 20)
            finally:
                upstream.close()

        return StreamingResponse(
            chunks(), media_type="application/octet-stream",
            headers={"Content-Length": str(entry["size"]),
                     "X-Checksum-Sha256": entry["sha256"],
                     "Content-Disposition": f'attachment; filename="{name}"'})

    @app.get("/api/v1/builds/{build_id}", response_model=BuildResponse)
    def get_build_status(
        build_id: str,
        request: Request,
        token_record: dict[str, Any] = Depends(current_user),
    ):
        app_db = get_db(request)
        build = app_db.get_build(build_id)
        if not build:
            raise HTTPException(status_code=404, detail="Build not found")
        require_member(app_db, token_record, build["project"])
        return build_response(request, build)

    @app.get("/api/v1/projects/{project}/builds/match", response_model=BuildResponse)
    def match_build(
        project: str,
        spec_digest: str,
        request: Request,
        target_arch: Optional[str] = None,
        token_record: dict[str, Any] = Depends(current_user),
    ):
        app_db = get_db(request)
        load_project(app_db, project)
        require_member(app_db, token_record, project)
        build = app_db.find_build_by_spec(project, spec_digest, target_arch)
        if not build:
            raise HTTPException(status_code=404, detail="No build matches this spec")
        return build_response(request, build)

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
        # Follow the uid, so a recycled username does not own the old builds.
        submitter = app_db.users.get(token_record["user_id"])
        if not (build.get("user_uid") and submitter and build["user_uid"] == submitter["uid"]):
            require_project_admin(app_db, token_record, build["project"])
        if not app_db.scheduler.request_cancel(build_id):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Build already finished",
            )
        finished = app_db.get_build(build_id)
        if finished and finished["status"] in _TERMINAL_STATES:
            _get_transient_secrets(request).pop(build_id)
        return {"status": "cancelling", "build_id": build_id}

    @app.websocket("/api/v1/projects/{project}/events")
    async def websocket_events(websocket: WebSocket, project: str):
        await serve_events(websocket, websocket.app.state.events, websocket.app.state.db, project)

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

    @app.patch("/api/v1/projects/{project}")
    def update_project(
        project: str,
        req: ProjectUpdateRequest,
        request: Request,
        tok: dict[str, Any] = Depends(current_user),
    ):
        app_db = get_db(request)
        require_system_admin(app_db, tok)
        if "quota_gb" in req.model_fields_set:
            if not app_db.projects.set_quota(project, req.quota_gb):
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Project '{project}' not found",
                )
        proj = app_db.projects.get(project)
        if not proj:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Project '{project}' not found",
            )
        return proj

    @app.post("/api/v1/storage/gc")
    def storage_gc(
        req: StorageGcRequest,
        request: Request,
        tok: dict[str, Any] = Depends(current_user),
    ):
        app_db = get_db(request)
        require_system_admin(app_db, tok)
        app_settings = request.app.state.settings
        try:
            reports = run_housekeeping(app_db, app_settings, project=req.project, dry_run=req.dry_run)
        except HousekeepingBusy as e:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
        return {
            "retention_enabled": app_settings.retention is not None,
            "reports": [
                {
                    "project": r.project,
                    "dry_run": r.dry_run,
                    "evicted": [{"build": b, "reason": why, "bytes": n} for b, why, n in r.evicted],
                    "failures": [{"build": b, "error": err} for b, err in r.failures],
                    "worktrees": [{"digest": d, "bytes": n} for d, n in r.worktrees],
                    "lifecycle": r.lifecycle,
                    "usage_before": r.usage_before,
                    "usage_after": r.usage_after,
                    "high_water_bytes": r.high_water_bytes,
                    "low_water_bytes": r.low_water_bytes,
                    "skipped_reason": r.skipped_reason,
                }
                for r in reports
            ],
        }

    @app.delete("/api/v1/projects/{project}")
    def delete_project(
        project: str,
        request: Request,
        purge_storage: bool = False,
        tok: dict[str, Any] = Depends(current_user),
    ):
        app_db = get_db(request)
        if purge_storage:
            require_system_admin(app_db, tok)
        else:
            require_project_admin(app_db, tok, project)
        proj = app_db.projects.get(project)
        if not proj:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Project '{project}' not found",
            )
        purged = []
        if purge_storage:
            if app_db.builds.has_active_builds(project):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Project '{project}' still has builds in progress",
                )
            purged = _purge_project_storage(request.app.state.settings, proj)
        app_db.projects.delete(project)
        return {"deleted": True, "project": project, "purged": purged}

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

    @app.delete("/api/v1/users/{user_id}")
    def delete_user(
        user_id: str,
        request: Request,
        purge: bool = False,
        anonymize: bool = False,
        delete_home: bool = False,
        tok: dict[str, Any] = Depends(current_user),
    ):
        app_db = get_db(request)
        require_system_admin(app_db, tok)
        if not purge:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Deletion requires purge=true (deactivate the user to keep their history)",
            )
        user = app_db.users.get(user_id)
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=f"User '{user_id}' not found"
            )
        if user["id"] == tok["user_id"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="You cannot delete your own account"
            )
        home = app_db.home_project(user["id"]) if delete_home else None
        if home:
            if app_db.builds.has_active_builds(home["id"]):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Project '{home['id']}' still has builds in progress",
                )
            _purge_project_storage(request.app.state.settings, home)
        try:
            res = app_db.purge_user(user["id"], anonymize=anonymize, delete_home=delete_home)
        except (LastAdminError, UserBusyError) as e:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
        if res is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=f"User '{user_id}' not found"
            )
        return res

    def _storage_info(settings: Settings) -> StorageInfo:
        if storage_type(settings) == "artifactory":
            return StorageInfo(
                type="artifactory", endpoint=settings.artifactory_endpoint,
                job_tokens=settings.artifactory_job_tokens, downloads=settings.artifactory_downloads)
        return StorageInfo(type="s3")

    def _profile(db: Database, user_token: dict[str, Any], settings: Settings) -> UserProfileResponse:
        user_id = user_token["user_id"]
        is_admin = is_system_admin(db, user_token)
        rows = db.conn.execute(
            "SELECT project_id, role FROM project_members WHERE user_id = ?",
            (user_id,),
        ).fetchall()
        projects = {r["project_id"]: r["role"] for r in rows}
        user = db.users.get(user_id)
        default = user["default_project"] if user else None
        # An administrator picks any project; anybody else only keeps one they belong to.
        if default and not (is_admin or default in projects):
            default = None
        return UserProfileResponse(
            id=user_id,
            uid=user["uid"] if user else "",
            is_admin=is_admin,
            projects=projects,
            default_project=default,
            storage=_storage_info(settings),
        )

    @app.get("/api/v1/me", response_model=UserProfileResponse)
    async def get_current_user_profile(
        request: Request,
        user_token: dict[str, Any] = Depends(current_user),
    ) -> UserProfileResponse:
        return _profile(get_db(request), user_token, request.app.state.settings)

    @app.patch("/api/v1/me", response_model=UserProfileResponse)
    async def update_current_user_preferences(
        payload: UserPreferencesRequest,
        request: Request,
        user_token: dict[str, Any] = Depends(current_user),
    ) -> UserProfileResponse:
        db = get_db(request)
        project_id = None
        if payload.default_project is not None:
            project = load_project(db, payload.default_project)
            require_member(db, user_token, project["id"])
            project_id = project["id"]
        db.users.set_default_project(user_token["user_id"], project_id)
        return _profile(db, user_token, request.app.state.settings)

    @app.get("/api/v1/workers", response_model=WorkerRosterResponse)
    async def list_workers(
        request: Request,
        user_token: dict[str, Any] = Depends(current_user),
    ) -> WorkerRosterResponse:
        db = get_db(request)
        workers = await run_in_threadpool(db.workers.list)
        return WorkerRosterResponse(workers=[WorkerRosterItem(**w) for w in workers])

    @app.post("/api/v1/workers/{worker_id}/pause")
    async def pause_worker(
        worker_id: str,
        payload: WorkerPauseRequest,
        request: Request,
        user_token: dict[str, Any] = Depends(current_user),
    ) -> dict[str, Any]:
        db = get_db(request)
        require_system_admin(db, user_token)
        ok = await run_in_threadpool(db.workers.pause, worker_id, payload.paused)
        if not ok:
            raise HTTPException(status_code=404, detail=f"Worker '{worker_id}' not found")
        return {"ok": True, "worker_id": worker_id, "status": "paused" if payload.paused else "online"}

    @app.delete("/api/v1/workers/{worker_id}")
    async def delete_worker(
        worker_id: str,
        request: Request,
        user_token: dict[str, Any] = Depends(current_user),
    ) -> dict[str, Any]:
        db = get_db(request)
        require_system_admin(db, user_token)
        ok = await run_in_threadpool(db.workers.delete, worker_id)
        if not ok:
            raise HTTPException(status_code=404, detail=f"Worker '{worker_id}' not found")
        return {"ok": True, "deleted": worker_id}

    return app

