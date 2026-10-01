# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import fnmatch
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Optional

from seine.credentials import FEED_AUTH_ENV
from seine.distributed.common.models import JobManifest, JobS3
from seine.distributed.common.s3 import provider_from


def find_seine_binary() -> str:
    """Return the seine binary path, preferring the active virtual environment."""
    candidate = os.path.join(os.path.dirname(sys.executable), "seine")
    if os.path.exists(candidate):
        return candidate
    found = shutil.which("seine")
    return found if found else "seine"


# Names a build may inherit from the agent; everything else is dropped.
_ENV_NAMES = frozenset([
    "PATH", "HOME", "LANG", "TZ", "TERM", "TMPDIR", "DBUS_SESSION_BUS_ADDRESS",
    "http_proxy", "https_proxy", "no_proxy", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
])
_ENV_PREFIXES = ("LC_", "XDG_", "CONTAINERS_")
# SEINE_* names holding secrets or keys (the agent's tokens, signing keys...).
_SECRET_MARKERS = ("TOKEN", "PASSWORD", "SECRET", "KEY")

_JOB_DIR_NAME = re.compile(r"^bld-[\w-]+$")
# Rootless podman keeps its overlay mounts in the user namespace: unmount them there first.
_UNMOUNT_AND_REMOVE = (
    'J="$1"\n'
    'grep -F " $J/" /proc/self/mounts | awk \'{print $2}\' | sort -r | '
    'while read -r m; do umount -l "$m" 2>/dev/null || true; done\n'
    'chmod -R u+rwX "$J" 2>/dev/null || true\n'
    'rm -rf "$J"'
)
# Never inherited: the agent holds no S3 keys, a job brings its own.
_S3_ENV_PREFIXES = ("AWS_", "SEINE_S3_")


def feed_secrets(manifest: JobManifest) -> dict[str, dict[str, str]]:
    """Return the job's feed credentials as {feed id: {login, password}}."""
    feeds = manifest.transient_secrets.get("feeds")
    return feeds if isinstance(feeds, dict) else {}


def child_env(
    manifest: JobManifest,
    build_dir: str,
    environ: Optional[dict] = None,
    feedauth_file: Optional[str] = None,
) -> dict:
    """Build the environment of a build: an allowlist of the agent's, plus the job's S3 keys."""
    environ = os.environ if environ is None else environ
    env = {}
    for name, value in environ.items():
        if name.startswith(_S3_ENV_PREFIXES) or name in ("SEINE_CREDENTIALS_FILE", FEED_AUTH_ENV):
            continue
        if name in _ENV_NAMES or name.startswith(_ENV_PREFIXES):
            env[name] = value
        elif name.startswith("SEINE_") and not any(m in name for m in _SECRET_MARKERS):
            env[name] = value
    if manifest.s3 and manifest.options.get("s3_cache"):
        env["AWS_ACCESS_KEY_ID"] = manifest.s3.access_key
        env["AWS_SECRET_ACCESS_KEY"] = manifest.s3.secret_key
        # Keep a credentials file of the agent's user out of the key lookup.
        env["SEINE_CREDENTIALS_FILE"] = os.path.join(os.path.dirname(build_dir), "no-credentials.json")
    if feedauth_file:
        env[FEED_AUTH_ENV] = feedauth_file
    env["SEINE_BUILD_DIR"] = build_dir
    env["SEINE_BUILD_ID"] = manifest.build_id
    # Ensure child seine process flushes stdout/stderr line by line.
    env["PYTHONUNBUFFERED"] = "1"
    return env


class SpecPathError(ValueError):
    """The job's spec file is not a file inside its job directory."""


def resolve_spec(job_dir: str, spec_file: str) -> str:
    """Return spec_file relative to job_dir, refusing anything that leaves it."""
    if os.path.isabs(spec_file):
        raise SpecPathError(f"spec path {spec_file!r} is absolute")
    root = os.path.realpath(job_dir)
    path = os.path.realpath(os.path.join(root, spec_file))
    if path == root or not path.startswith(root + os.sep):
        raise SpecPathError(f"spec path {spec_file!r} escapes the job directory")
    if not os.path.isfile(path):
        raise SpecPathError(f"spec file {spec_file!r} not found in the job directory")
    return os.path.relpath(path, root)


class SubprocessExecutor:
    """Executes build jobs in isolated subprocesses with live log streaming."""

    # Seconds between SIGTERM and SIGKILL when stopping a build.
    kill_grace = 10.0
    # Pauses before the retries of a job directory wipe.
    wipe_backoff = (2.0, 5.0)

    def __init__(self, work_dir: str):
        self.work_dir = os.path.abspath(work_dir)
        self._cancel = threading.Event()
        self.failure_reason: Optional[str] = None

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def cancel(self) -> None:
        """Ask the running build, if any, to stop."""
        self._cancel.set()

    def clear_cancel(self) -> None:
        self._cancel.clear()

    def job_dir(self, build_id: str) -> str:
        return os.path.join(self.work_dir, "jobs", build_id)

    def secrets_dir(self, build_id: Optional[str] = None) -> str:
        """Return where job secrets live, outside every job directory."""
        root = os.path.join(self.work_dir, "secrets")
        return os.path.join(root, build_id) if build_id else root

    def write_feed_secrets(self, manifest: JobManifest) -> Optional[str]:
        """Write the job's feed credentials to a private file and return its path, if any."""
        feeds = feed_secrets(manifest)
        if not feeds:
            return None
        os.makedirs(self.secrets_dir(), mode=0o700, exist_ok=True)
        directory = self.secrets_dir(manifest.build_id)
        os.makedirs(directory, mode=0o700, exist_ok=True)
        os.chmod(directory, 0o700)
        path = os.path.join(directory, "feeds.json")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(feeds, f)
        return path

    def remove_feed_secrets(self, build_id: Optional[str] = None) -> None:
        """Delete the secrets of one build, or of every build when build_id is None."""
        if build_id is not None and build_id != os.path.basename(build_id):
            return
        shutil.rmtree(self.secrets_dir(build_id), ignore_errors=True)

    def _pull_worktree(self, manifest: JobManifest, job_dir: str) -> None:
        provider = provider_from(manifest.s3)
        if provider.pull_worktree(manifest.project, manifest.worktree_digest, dest_dir=job_dir) is None:
            raise FileNotFoundError(
                f"worktree {manifest.worktree_digest} not found in bucket {manifest.s3.bucket}"
            )

    def _wait(self, proc: subprocess.Popen, on_log: Callable[[str, str], None]) -> int:
        """Wait for the build; stop its process group once cancelled."""
        while True:
            try:
                return proc.wait(timeout=0.1)
            except subprocess.TimeoutExpired:
                if self.cancelled:
                    break
        on_log("system", "[agent] Cancelling build...\n")
        self._signal_group(proc, signal.SIGTERM)
        try:
            code = proc.wait(timeout=self.kill_grace)
        except subprocess.TimeoutExpired:
            on_log("system", "[agent] Build ignored SIGTERM, killing it\n")
            code = None
        self._signal_group(proc, signal.SIGKILL)
        return code if code is not None else proc.wait()

    @staticmethod
    def _signal_group(proc: subprocess.Popen, sig: int) -> None:
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass

    def cleanup_containers(
        self,
        manifest: JobManifest,
        env: dict,
        on_log: Callable[[str, str], None],
    ) -> None:
        """Remove the containers labelled with this build's id."""
        if not shutil.which("podman", path=env.get("PATH")):
            return
        # Same storage seine's engine wrapper uses for this build.
        root = env.get("SEINE_CONTAINERS_DIR") or os.path.join(env["SEINE_BUILD_DIR"], "containers")
        podman = ["podman", "--root", root, "--runroot", os.path.join(root, "run")]
        label = f"label=seine.build_id={manifest.build_id}"

        def run(args, timeout):
            try:
                proc = subprocess.run(
                    podman + args, env=env, capture_output=True, text=True, timeout=timeout,
                )
            except (OSError, subprocess.TimeoutExpired) as e:
                on_log("system", f"[agent] podman {args[0]} failed: {e}\n")
                return None
            if proc.returncode != 0:
                on_log("system", f"[agent] podman {args[0]} failed: {proc.stderr.strip()}\n")
                return None
            return proc.stdout

        listed = run(["ps", "-aq", "--filter", label], timeout=60)
        ids = listed.split() if listed else []
        if ids:
            on_log("system", f"[agent] Removing {len(ids)} container(s) left by the build\n")
            run(["rm", "-f", *ids], timeout=300)

    def wipe_job_dir(
        self,
        build_id: str,
        on_log: Optional[Callable[[str, str], None]] = None,
    ) -> None:
        """Remove jobs/<build_id>, and nothing else; never raises, only logs."""
        def log(text):
            try:
                if on_log:
                    on_log("system", f"[agent] {text}\n")
            except Exception:
                pass

        try:
            self._wipe_job_dir(build_id, log)
        except Exception as e:
            log(f"Could not remove the job directory of {build_id}: {e}")

    def _wipe_job_dir(self, build_id: str, log: Callable[[str], None]) -> None:
        jobs = os.path.join(os.path.realpath(self.work_dir), "jobs")
        path = os.path.join(jobs, build_id)
        if not build_id or os.path.islink(path) or os.path.realpath(path) != path \
                or os.path.dirname(path) != jobs:
            log(f"Refusing to remove {path}: not a job directory")
            return
        if not os.path.lexists(path):
            return

        for attempt, pause in enumerate((0.0, *self.wipe_backoff), start=1):
            if pause:
                time.sleep(pause)
            log(f"Removing {path} (attempt {attempt})")
            shutil.rmtree(path, onexc=lambda func, name, exc: None)
            if not os.path.lexists(path):
                return
            try:
                proc = subprocess.run(
                    ["podman", "unshare", "bash", "-c", _UNMOUNT_AND_REMOVE, "_", path],
                    capture_output=True, text=True, timeout=300,
                )
                if proc.returncode != 0:
                    log(f"podman unshare failed: {proc.stderr.strip()}")
            except (OSError, subprocess.TimeoutExpired) as e:
                log(f"podman unshare failed: {e}")
            if not os.path.lexists(path):
                return
        log(f"Could not remove {path}")

    def wipe_stale_jobs(self, on_log: Optional[Callable[[str, str], None]] = None) -> None:
        """Remove the job directories and containers a crashed agent left; never raises."""
        def log(text):
            try:
                if on_log:
                    on_log("system", f"[agent] {text}\n")
            except Exception:
                pass

        try:
            self._wipe_stale_jobs(log, on_log)
        except Exception as e:
            log(f"Could not clean the stale job directories: {e}")

    def _wipe_stale_jobs(self, log, on_log) -> None:
        jobs = os.path.join(self.work_dir, "jobs")
        try:
            names = sorted(os.listdir(jobs))
        except FileNotFoundError:
            return
        for name in names:
            path = os.path.join(jobs, name)
            if not _JOB_DIR_NAME.match(name) or os.path.islink(path) or not os.path.isdir(path):
                continue
            log(f"Removing stale job directory {path}")
            try:
                self._remove_stale_containers(path, log)
            except Exception as e:
                log(f"Could not remove the containers of {name}: {e}")
            self.wipe_job_dir(name, on_log=on_log)

    @staticmethod
    def _remove_stale_containers(job_dir: str, log: Callable[[str], None]) -> None:
        root = os.path.join(job_dir, "build", "containers")
        if not os.path.isdir(root) or not shutil.which("podman"):
            return
        try:
            proc = subprocess.run(
                ["podman", "--root", root, "--runroot", os.path.join(root, "run"), "rm", "-af"],
                capture_output=True, text=True, timeout=300,
            )
            if proc.returncode != 0:
                log(f"podman rm failed: {proc.stderr.strip()}")
        except (OSError, subprocess.TimeoutExpired) as e:
            log(f"podman rm failed: {e}")

    def execute_job(
        self,
        manifest: JobManifest,
        on_log: Callable[[str, str], None],
    ) -> int:
        try:
            return self._execute_job(manifest, on_log)
        finally:
            self.remove_feed_secrets(manifest.build_id)

    def _execute_job(
        self,
        manifest: JobManifest,
        on_log: Callable[[str, str], None],
    ) -> int:
        self.failure_reason = None
        job_dir = self.job_dir(manifest.build_id)
        build_dir = os.path.join(job_dir, "build")
        os.makedirs(build_dir, exist_ok=True)

        if not manifest.worktree_digest:
            on_log("system", f"[agent] Job {manifest.build_id} has no worktree digest\n")
            return 1
        if manifest.s3 is None:
            on_log("system", f"[agent] Job {manifest.build_id} came without S3 access\n")
            return 1
        on_log("system", f"[agent] Pulling worktree {manifest.worktree_digest} from {manifest.s3_bucket}...\n")
        try:
            self._pull_worktree(manifest, job_dir)
        except Exception as e:
            on_log("system", f"[agent] Failed to pull worktree: {e}\n")
            self.failure_reason = f"failed to pull worktree: {e}"
            return 1

        try:
            rel_spec = resolve_spec(job_dir, manifest.spec_file)
        except SpecPathError as e:
            on_log("system", f"[agent] Refusing job {manifest.build_id}: {e}\n")
            return 1

        env = child_env(manifest, build_dir, feedauth_file=self.write_feed_secrets(manifest))

        cmd = [find_seine_binary(), "build"]
        if manifest.options.get("packages_only"):
            cmd.append("--packages-only")
        if manifest.options.get("s3_cache"):
            cmd += [
                "--s3-cache",
                f"--s3-endpoint={manifest.s3.endpoint}",
                f"--s3-bucket={manifest.s3.bucket}",
                f"--s3-region={manifest.s3.region}",
            ]
        cmd.append(rel_spec)

        on_log("system", f"[agent] Launching {' '.join(cmd)} in {job_dir}\n")

        try:
            proc = subprocess.Popen(
                cmd,
                cwd=job_dir,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                start_new_session=True,
            )
        except OSError as e:
            on_log("system", f"[agent] Failed to start {cmd[0]}: {e}\n")
            return 1

        def stream_output(pipe, source):
            try:
                for line in iter(pipe.readline, ""):
                    on_log(source, line)
            finally:
                pipe.close()

        t_out = threading.Thread(target=stream_output, args=(proc.stdout, "stdout"))
        t_err = threading.Thread(target=stream_output, args=(proc.stderr, "stderr"))
        t_out.start()
        t_err.start()

        try:
            return_code = self._wait(proc, on_log)
            # A container engine daemon may keep the pipes open after a kill.
            t_out.join(timeout=10 if self.cancelled else None)
            t_err.join(timeout=10 if self.cancelled else None)
        finally:
            self.cleanup_containers(manifest, env, on_log)

        on_log("system", f"[agent] Build {manifest.build_id} finished with exit code {return_code}\n")
        return return_code

    def harvest_artifacts(self, build_id_or_path: str) -> list[str]:
        if os.path.isdir(build_id_or_path):
            return harvest_artifacts(build_id_or_path)
        job_dir = os.path.join(self.work_dir, "jobs", build_id_or_path)
        return harvest_artifacts(job_dir)

    def upload_artifacts(
        self,
        manifest: JobManifest,
        on_log: Optional[Callable[[str, str], None]] = None,
        provider: Optional[Any] = None,
    ) -> list[dict[str, Any]]:
        job_dir = os.path.join(self.work_dir, "jobs", manifest.build_id)
        return upload_artifacts(
            job_dir=job_dir,
            project=manifest.project,
            build_id=manifest.build_id,
            provider=provider,
            s3=manifest.s3,
            on_log=on_log,
        )


DELIVERABLE_PATTERNS = [
    "*.img",
    "*.raw",
    "*.qcow2",
    "*.iso",
    "*.rootfs.tar",
    "*.digest",
    "*.recipe",
    "*.boot-signers*",
    "*.sbom*",
]


def harvest_artifacts(path: str) -> list[str]:
    """Harvest deliverable and metadata files from build/deploy or directory."""
    deploy_dir = path
    candidate = os.path.join(path, "build", "deploy")
    if os.path.isdir(candidate):
        deploy_dir = candidate
    elif os.path.isdir(os.path.join(path, "deploy")):
        deploy_dir = os.path.join(path, "deploy")

    if not os.path.isdir(deploy_dir):
        return []

    results = []
    for root, _, files in os.walk(deploy_dir):
        for f in sorted(files):
            for pat in DELIVERABLE_PATTERNS:
                if fnmatch.fnmatch(f, pat):
                    results.append(os.path.abspath(os.path.join(root, f)))
                    break
    results.sort()
    return results


def upload_artifacts(
    job_dir: str,
    project: str,
    build_id: str,
    provider: Optional[Any] = None,
    s3: Optional[JobS3] = None,
    on_log: Optional[Callable[[str, str], None]] = None,
) -> list[dict[str, Any]]:
    """Upload harvested deliverables to S3; return their {name, key, sha256, size}."""
    files = harvest_artifacts(job_dir)
    if not files:
        return []

    if provider is None:
        if s3 is None:
            raise ValueError("the job came without S3 access")
        provider = provider_from(s3)

    uploaded = []
    for file_path in files:
        name = os.path.basename(file_path)
        if on_log:
            on_log("system", f"[agent] Uploading artifact {name} to S3...\n")
        try:
            info = provider.push_artifact_info(project, build_id, file_path, artifact_name=name)
        except Exception as e:
            if on_log:
                on_log("system", f"[agent] Failed to upload artifact {name}: {e}\n")
            raise
        uploaded.append(info)
        if on_log:
            on_log("system", f"[agent] Uploaded artifact {info['key']}\n")
    return uploaded
