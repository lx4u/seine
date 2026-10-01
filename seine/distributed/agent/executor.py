# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import os
import shutil
import signal
import subprocess
import sys
import threading
from typing import Callable, Optional

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
# Never inherited: the agent holds no S3 keys, a job brings its own.
_S3_ENV_PREFIXES = ("AWS_", "SEINE_S3_")


def child_env(manifest: JobManifest, build_dir: str, environ: Optional[dict] = None) -> dict:
    """Build the environment of a build: an allowlist of the agent's, plus the job's S3 keys."""
    environ = os.environ if environ is None else environ
    env = {}
    for name, value in environ.items():
        if name.startswith(_S3_ENV_PREFIXES) or name == "SEINE_CREDENTIALS_FILE":
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

    def __init__(self, work_dir: str):
        self.work_dir = os.path.abspath(work_dir)
        self._cancel = threading.Event()

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

    def _pull_worktree(self, manifest: JobManifest, job_dir: str) -> None:
        provider = provider_from(manifest.s3)
        provider.pull_worktree(manifest.project, manifest.worktree_digest, dest_dir=job_dir)

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

        shutil.rmtree(path, onexc=lambda func, name, exc: None)
        if not os.path.lexists(path):
            return
        log(f"Could not remove {path} directly, trying podman unshare")
        try:
            proc = subprocess.run(
                ["podman", "unshare", "rm", "-rf", path],
                capture_output=True, text=True, timeout=300,
            )
            if proc.returncode != 0:
                log(f"podman unshare rm failed: {proc.stderr.strip()}")
        except (OSError, subprocess.TimeoutExpired) as e:
            log(f"podman unshare rm failed: {e}")
        if os.path.lexists(path):
            log(f"Could not remove {path}")

    def execute_job(
        self,
        manifest: JobManifest,
        on_log: Callable[[str, str], None],
    ) -> int:
        job_dir = self.job_dir(manifest.build_id)
        build_dir = os.path.join(job_dir, "build")
        os.makedirs(build_dir, exist_ok=True)

        env = child_env(manifest, build_dir)

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
            return 1

        try:
            rel_spec = resolve_spec(job_dir, manifest.spec_file)
        except SpecPathError as e:
            on_log("system", f"[agent] Refusing job {manifest.build_id}: {e}\n")
            return 1

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
