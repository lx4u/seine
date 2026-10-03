# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0
"""Shared data models and wire protocols for distributed seine."""

from typing import Any, Literal
from pydantic import BaseModel, Field


def format_quota(project: dict[str, Any]) -> str:
    """Quota of a project row as text for listings."""
    quota = project.get("quota_gb")
    return "none" if quota is None else f"{quota:g}GB"


class ProjectModel(BaseModel):
    """Registered project entity."""
    id: str
    name: str
    prod_bucket: str
    dev_bucket: str
    created_at: float


class ProjectMemberModel(BaseModel):
    """Project membership and role assignment."""
    project_id: str
    user_id: str
    role: str = "developer"
    created_at: float


class BuildModel(BaseModel):
    """Submitted or executed build record."""
    id: str
    project: str
    target_arch: str = "amd64"
    is_release: bool = False
    status: str = "queued"
    worktree_digest: str = ""
    spec_file: str = "spec.yaml"
    options: dict[str, Any] = Field(default_factory=dict)
    created_at: float
    started_at: float | None = None
    finished_at: float | None = None
    artifact_urls: list[str] = Field(default_factory=list)
    error_message: str | None = None
    user_id: str | None = None


class ArtifactMeta(BaseModel):
    """A build deliverable as reported by the worker that produced it."""
    name: str
    size: int
    sha256: str


EXPIRY_REASONS = {"ttl": "age", "pressure": "storage pressure"}


def format_expiry_reason(reason: str | None) -> str:
    """Words for an artifacts_expired_reason code, e.g. 'storage pressure'."""
    return EXPIRY_REASONS.get(reason or "", reason or "unknown")


def expired_text(reason: str | None) -> str:
    """Words for an artifacts_expired_reason code, e.g. 'expired (age)'."""
    return f"expired ({format_expiry_reason(reason)})"


class BuildResponse(BuildModel):
    """Detailed build response including artifact download URLs."""
    download_urls: dict[str, str] = Field(default_factory=dict)
    artifacts: list[ArtifactMeta] = Field(default_factory=list)
    # Set once housekeeping evicted the artifacts; the build row stays.
    artifacts_expired_at: float | None = None
    artifacts_expired_reason: str | None = None


class JobModel(BaseModel):
    """Discrete task job within a build."""
    id: str
    build_id: str
    kind: str = "image"
    target_arch: str = "amd64"
    package_name: str | None = None
    worker_id: str | None = None
    status: str = "queued"
    created_at: float
    started_at: float | None = None
    finished_at: float | None = None


class WorkerModel(BaseModel):
    """Registered worker daemon."""
    id: str
    hostname: str
    native_arch: str
    arch_scores: dict[str, float] = Field(default_factory=dict)
    concurrency_slots: int = 1
    free_disk_gb: float = 0.0
    token: str
    status: str = "online"
    last_seen: float
    created_at: float


class TokenModel(BaseModel):
    """Personal access token or worker registration token."""
    token: str
    user_id: str
    kind: str = "pat"
    created_at: float
    expires_at: float | None = None


Project = ProjectModel
ProjectMember = ProjectMemberModel
Build = BuildModel
Job = JobModel
Worker = WorkerModel
Token = TokenModel


class WorkerCapabilities(BaseModel):
    """Advertised worker hardware and architecture capabilities."""
    native_arch: str
    arch_scores: dict[str, float] = Field(default_factory=dict)
    concurrency_slots: int = 1
    free_disk_gb: float = 0.0
    tools: dict[str, bool] = Field(default_factory=dict)


class RegisterWorkerRequest(BaseModel):
    """Payload sent by seine-agent upon registration."""
    worker_id: str
    hostname: str
    capabilities: WorkerCapabilities


class RegisterWorkerResponse(BaseModel):
    """Server registration confirmation and minted worker bearer token."""
    worker_id: str
    worker_token: str
    status: str = "registered"


class HeartbeatRequest(BaseModel):
    """Periodic worker heartbeat."""
    worker_id: str
    free_disk_gb: float
    running_jobs: list[str] | None = None


class ClaimJobRequest(BaseModel):
    """Worker requesting a matching queued job."""
    worker_id: str


class JobS3(BaseModel):
    """S3 access scoped to one bucket; sent only to the worker that owns the job."""
    endpoint: str
    region: str = "garage"
    bucket: str
    access_key: str = Field(repr=False)
    secret_key: str = Field(repr=False)


class JobManifest(BaseModel):
    """Discrete job assignment dispatched to a worker."""
    job_id: str
    build_id: str
    project: str
    kind: str = "image"
    target_arch: str = "amd64"
    worktree_digest: str = ""
    s3_bucket: str = ""
    s3: JobS3 | None = None
    spec_file: str = "spec.yaml"
    package_name: str | None = None
    options: dict[str, Any] = Field(default_factory=dict)
    transient_secrets: dict[str, Any] = Field(default_factory=dict, repr=False)


class JobStatusUpdateRequest(BaseModel):
    """Worker status report for a claimed job."""
    worker_id: str
    build_id: str
    job_id: str
    status: str
    error_message: str | None = None
    artifact_urls: list[str] = Field(default_factory=list)
    artifacts: list[dict[str, Any]] = Field(default_factory=list)


# The only build options the server accepts; the scheduler and agent read these.
BUILD_OPTION_KEYS = frozenset({
    "packages_only", "s3_cache", "verbose", "require_native", "min_arch_score",
    "--require-native", "--min-arch-score",
})


class BuildSubmitRequest(BaseModel):
    """Build request submitted by client (CLI or TUI)."""
    project: str
    worktree_digest: str
    target_arch: str = "amd64"
    is_release: bool = False
    spec_file: str = "spec.yaml"
    options: dict[str, Any] = Field(default_factory=dict)
    transient_secrets: dict[str, Any] = Field(default_factory=dict, repr=False)


class BuildSubmitResponse(BaseModel):
    """Build submission acknowledgment."""
    build_id: str
    status: str = "queued"
    project: str
    target_arch: str


class LogChunk(BaseModel):
    """Real-time log event transmitted over WebSocket."""
    build_id: str
    source: str = "stdout"
    text: str
    timestamp: float


class ProjectCreateRequest(BaseModel):
    """Project creation request payload."""
    name: str
    dev_bucket: str | None = None
    prod_bucket: str | None = None
    provision_buckets: bool = False


class ProjectUpdateRequest(BaseModel):
    """Project update payload; omitted fields are left unchanged, null clears the quota."""
    quota_gb: float | None = Field(default=None, gt=0)


class StorageGcRequest(BaseModel):
    """Housekeeping request; without a project every project is processed."""
    project: str | None = None
    dry_run: bool = False


class MemberAddRequest(BaseModel):
    """Project member assignment payload."""
    user_id: str
    role: str = "developer"


class TokenIssueRequest(BaseModel):
    """Token generation request payload."""
    user_id: str
    kind: Literal["pat"] = "pat"
    days: int | None = None


class UserCreateRequest(BaseModel):
    """User creation payload."""
    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,63}$")
    is_admin: bool = False


class UserUpdateRequest(BaseModel):
    """User update payload; omitted fields are left unchanged."""
    is_admin: bool | None = None
    active: bool | None = None


class UserProfileResponse(BaseModel):
    """User profile and project role map for client capability negotiation."""
    id: str
    is_admin: bool
    projects: dict[str, str] = Field(default_factory=dict)
    default_project: str | None = None


class UserPreferencesRequest(BaseModel):
    """Preferences a user changes about themselves; null clears the default project."""
    default_project: str | None


class WorkerRosterItem(BaseModel):
    """Worker node record displayed in fleet roster."""
    id: str
    hostname: str
    native_arch: str
    arch_scores: dict[str, float] = Field(default_factory=dict)
    concurrency_slots: int = 1
    free_disk_gb: float = 0.0
    status: str = "online"
    last_seen: float
    created_at: float


class WorkerRosterResponse(BaseModel):
    """Roster of registered worker nodes."""
    workers: list[WorkerRosterItem] = Field(default_factory=list)


class WorkerPauseRequest(BaseModel):
    """Payload to pause or unpause job scheduling on a worker."""
    paused: bool = True
