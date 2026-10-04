# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import json
import os
import signal
import socket
import threading
import tempfile
import time
from typing import Callable, Optional

import requests

from seine import vault
from seine.distributed.agent.detect import detect_capabilities
from seine.distributed.agent.executor import SubprocessExecutor, feed_secrets
from seine.distributed.common import storage as job_storage
from seine.distributed.agent.stream import LogStreamer, redacting
from seine.distributed.common.models import (
    ClaimJobRequest,
    HeartbeatRequest,
    JobManifest,
    JobStatusUpdateRequest,
    RegisterWorkerRequest,
    RegisterWorkerResponse,
)
from seine.distributed.common.transport import check_server_url, requests_verify

FINAL_STATUSES = ("completed", "failed", "cancelled")
# The server will never accept these results, so retrying is pointless.
REJECTED_CODES = (403, 404, 409, 410)
MAX_BACKOFF = 30.0
ARTIFACT_FIELDS = ("name", "key", "sha256", "size")


class WorkerAgent:
    """Daemon running on a build machine, claiming jobs from seine-server."""

    def __init__(
        self,
        server_url: str,
        enrollment_token: str,
        work_dir: str = "/var/tmp/seine-agent",
        worker_id: Optional[str] = None,
        ca_cert: Optional[str] = None,
        insecure: bool = False,
        heartbeat_interval: float = 30.0,
        report_retry_total: float = 600.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        check_server_url(server_url, insecure=insecure)
        self.server_url = server_url.rstrip("/")
        self.enrollment_token = enrollment_token
        self.work_dir = os.path.abspath(work_dir)
        self.hostname = socket.gethostname()
        self.worker_id = worker_id or f"worker-{self.hostname}-{detect_capabilities().native_arch}"
        self.worker_token: Optional[str] = None
        self.ca_cert = ca_cert
        self.heartbeat_interval = heartbeat_interval
        self.report_retry_total = report_retry_total
        self._sleep = sleep
        self._clock = clock
        self.pending_dir = os.path.join(self.work_dir, "pending")
        self._reporting: set[str] = set()
        self._reporting_lock = threading.Lock()
        self._last_heartbeat_ok = True
        self.executor = SubprocessExecutor(self.work_dir)
        self._running = False
        self._shutdown = threading.Event()
        self._stop_heartbeat = threading.Event()
        self._current_job_id: Optional[str] = None

    def _auth_headers(self, token: Optional[str] = None) -> dict:
        return {
            "Authorization": f"Bearer {token or self.worker_token}",
            "Content-Type": "application/json",
        }

    def register(self) -> None:
        caps = detect_capabilities(self.work_dir)
        req = RegisterWorkerRequest(
            worker_id=self.worker_id,
            hostname=self.hostname,
            capabilities=caps,
        )
        url = f"{self.server_url}/api/v1/workers/register"
        resp = requests.post(
            url,
            json=req.model_dump(),
            headers=self._auth_headers(self.enrollment_token),
            timeout=10,
            verify=requests_verify(self.ca_cert),
        )
        resp.raise_for_status()
        data = RegisterWorkerResponse(**resp.json())
        self.worker_token = data.worker_token
        print(f"[agent] Registered {self.worker_id} (arch: {caps.native_arch}) with {self.server_url}")

    def claim_job(self) -> Optional[JobManifest]:
        url = f"{self.server_url}/api/v1/workers/claim"
        req = ClaimJobRequest(worker_id=self.worker_id)
        resp = requests.post(url, json=req.model_dump(), headers=self._auth_headers(), timeout=10,
                             verify=requests_verify(self.ca_cert))
        if resp.status_code == 204:
            return None
        resp.raise_for_status()
        return JobManifest(**resp.json())

    def update_job_status(
        self,
        job_id: str,
        build_id: str,
        status: str,
        err: Optional[str] = None,
        artifacts: Optional[list[dict]] = None,
    ) -> None:
        """Report a job status; a final one is persisted and resent until the server answers."""
        if status not in FINAL_STATUSES:
            try:
                self._post_status(job_id, build_id, status, err, artifacts)
            except Exception as e:
                print(f"[agent] Failed to report job status: {e}")
            return
        try:
            self._write_pending(job_id, build_id, status, err, artifacts)
        except Exception as e:
            print(f"[agent] Could not persist the result of job {job_id}: {e}")
        self._report_with_retry(job_id, build_id, status, err, artifacts)

    def _post_status(self, job_id, build_id, status, err, artifacts) -> int:
        """Post one status report and return the HTTP status code."""
        url = f"{self.server_url}/api/v1/workers/jobs/{job_id}/status"
        req = JobStatusUpdateRequest(
            worker_id=self.worker_id,
            build_id=build_id,
            job_id=job_id,
            status=status,
            error_message=err,
            artifact_urls=[a["key"] for a in artifacts or []],
            artifacts=artifacts or [],
        )
        resp = requests.post(url, json=req.model_dump(), headers=self._auth_headers(), timeout=10,
                             verify=requests_verify(self.ca_cert))
        return resp.status_code

    def _pending_path(self, job_id: str) -> str:
        return os.path.join(self.pending_dir, f"{os.path.basename(job_id)}.json")

    def _write_pending(self, job_id, build_id, status, err, artifacts) -> None:
        """Write a final result atomically; it holds no secret, only the report itself."""
        if err:
            for secret in vault.secrets():
                err = err.replace(secret, "<redacted>")
        record = {
            "job_id": job_id,
            "build_id": build_id,
            "status": status,
            "error": err,
            "artifacts": [{k: a[k] for k in ARTIFACT_FIELDS if k in a} for a in artifacts or []],
            "written_at": time.time(),
        }
        os.makedirs(self.pending_dir, mode=0o700, exist_ok=True)
        os.chmod(self.pending_dir, 0o700)
        fd, tmp = tempfile.mkstemp(dir=self.pending_dir, prefix=".tmp-", suffix=".json")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(record, f)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, self._pending_path(job_id))
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _pending_job_ids(self) -> list[str]:
        try:
            names = os.listdir(self.pending_dir)
        except OSError:
            return []
        return sorted(n[:-5] for n in names if n.endswith(".json") and not n.startswith("."))

    def _attempt_pending(self, job_id, build_id, status, err, artifacts) -> bool:
        """Send a result once; return True when it needs no further attempt."""
        try:
            code = self._post_status(job_id, build_id, status, err, artifacts)
        except Exception as e:
            print(f"[agent] Failed to report job status: {e}")
            return False
        if 200 <= code < 300:
            pass
        elif code in REJECTED_CODES:
            print(f"[agent] Server refused the result of job {job_id} (HTTP {code}), dropping it")
        else:
            print(f"[agent] Failed to report job status: HTTP {code}")
            return False
        try:
            os.unlink(self._pending_path(job_id))
        except OSError:
            pass
        return True

    def _report_with_retry(self, job_id, build_id, status, err, artifacts) -> None:
        with self._reporting_lock:
            self._reporting.add(job_id)
        try:
            start, delay = self._clock(), 1.0
            while not self._attempt_pending(job_id, build_id, status, err, artifacts):
                if self._clock() - start + delay > self.report_retry_total:
                    print(f"[agent] Result of job {job_id} kept in {self.pending_dir} for a later retry")
                    return
                self._sleep(delay)
                delay = min(delay * 2, MAX_BACKOFF)
        finally:
            with self._reporting_lock:
                self._reporting.discard(job_id)

    def flush_pending(self) -> None:
        """Send every persisted result once; one failure never blocks the others, never raises."""
        for job_id in self._pending_job_ids():
            with self._reporting_lock:
                if job_id in self._reporting:
                    continue
            try:
                with open(self._pending_path(job_id)) as f:
                    rec = json.load(f)
                self._attempt_pending(
                    rec["job_id"], rec["build_id"], rec["status"], rec.get("error"), rec.get("artifacts")
                )
            except Exception as e:
                print(f"[agent] Could not resend the result of job {job_id}: {e}")

    def run_job(self, manifest: JobManifest) -> None:
        """Run a claimed job and stream stdout/stderr over WebSocket."""
        print(f"[agent] Starting job {manifest.job_id} (build: {manifest.build_id}, target: {manifest.target_arch})")
        self.executor.clear_cancel()
        self._current_job_id = manifest.job_id
        status, err, artifacts = "failed", None, []
        if manifest.storage:
            for secret in job_storage.secret_values(manifest.storage):
                vault.record_secret(secret)
        for pair in feed_secrets(manifest).values():
            vault.record_secret(pair.get("login"))
            vault.record_secret(pair.get("password"))
        try:
            with LogStreamer(
                self.server_url, manifest.build_id, self.worker_token or "", ca_cert=self.ca_cert
            ) as streamer:
                send = redacting(streamer.send)
                self.executor.on_event = streamer.send_event
                try:
                    status, err, artifacts = self._execute(manifest, send)
                finally:
                    try:
                        self.executor.wipe_job_dir(manifest.build_id, on_log=send)
                    except Exception as e:
                        print(f"[agent] Cleanup of job {manifest.job_id} failed: {e}")
        except Exception as e:
            print(f"[agent] Error running job {manifest.job_id}: {e}")
            status, err, artifacts = "failed", str(e), []
        finally:
            self.executor.on_event = None
            self._current_job_id = None
        self.update_job_status(
            manifest.job_id, manifest.build_id, status, err=err, artifacts=artifacts
        )
        print(f"[agent] Completed job {manifest.job_id} with status {status}")

    def _execute(self, manifest: JobManifest, on_log) -> tuple[str, Optional[str], list[dict]]:
        """Run the build and upload its artifacts; return (status, error, artifacts)."""
        if self._shutdown.is_set():
            return "failed", "agent shutting down", []
        ret = self.executor.execute_job(manifest, on_log=on_log)
        if self._shutdown.is_set():
            return "failed", "agent shutting down", []
        if self.executor.cancelled:
            return "cancelled", None, []
        if ret != 0:
            return "failed", self.executor.failure_reason or f"build exited with code {ret}", []
        try:
            return "completed", None, self.executor.upload_artifacts(manifest, on_log=on_log)
        except Exception as e:
            return "failed", f"artifact upload failed: {e}", []

    def _send_heartbeat(self) -> list[str]:
        """Send one heartbeat and return the job ids the server wants cancelled."""
        caps = detect_capabilities(self.work_dir)
        url = f"{self.server_url}/api/v1/workers/heartbeat"
        job_id = self._current_job_id
        running = sorted({*self._pending_job_ids(), *([job_id] if job_id else [])})
        try:
            resp = requests.post(
                url,
                json={
                    "worker_id": self.worker_id,
                    "free_disk_gb": caps.free_disk_gb,
                    "running_jobs": running,
                },
                headers=self._auth_headers(),
                timeout=5,
                verify=requests_verify(self.ca_cert),
            )
            resp.raise_for_status()
            self._last_heartbeat_ok = True
            cancel = resp.json().get("cancel", [])
            return [str(j) for j in cancel] if isinstance(cancel, list) else []
        except Exception as e:
            self._last_heartbeat_ok = False
            print(f"[agent] Heartbeat failed: {e}")
            return []

    def _heartbeat_loop(self) -> None:
        """Heartbeat for the whole life of the agent, also while a job runs."""
        while not self._stop_heartbeat.is_set():
            cancel = self._send_heartbeat()
            if self._last_heartbeat_ok:
                self.flush_pending()
            job_id = self._current_job_id
            if job_id and job_id in cancel:
                print(f"[agent] Server asked to cancel job {job_id}")
                self.executor.cancel()
            self._stop_heartbeat.wait(self.heartbeat_interval)

    def stop(self) -> None:
        self._running = False

    def shutdown(self) -> None:
        """Stop polling and cancel the running job, if any."""
        self._shutdown.set()
        self.stop()
        self.executor.cancel()

    def start(self, poll_interval: float = 2.0) -> None:
        """Register, then claim and run jobs while a thread heartbeats."""
        def _handle_signal(signum, frame):
            print("\n[agent] Shutting down...")
            self.shutdown()

        self.executor.remove_feed_secrets()
        # Orphaned builds are stopped by systemd's cgroup kill (KillMode=mixed), not here.
        self.executor.wipe_stale_jobs(on_log=lambda source, text: print(text, end=""))
        signal.signal(signal.SIGTERM, _handle_signal)
        signal.signal(signal.SIGINT, _handle_signal)

        self.register()
        self.flush_pending()
        self._running = True
        self._stop_heartbeat.clear()
        heartbeat = threading.Thread(target=self._heartbeat_loop, daemon=True)
        heartbeat.start()

        print("[agent] Worker agent running, polling for jobs...")
        try:
            while self._running:
                try:
                    job = self.claim_job()
                    if job:
                        self.run_job(job)
                    else:
                        time.sleep(poll_interval)
                except Exception as e:
                    print(f"[agent] Polling error: {e}")
                    time.sleep(poll_interval)
        finally:
            self._stop_heartbeat.set()
            heartbeat.join(timeout=10)
