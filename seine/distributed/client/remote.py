# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0
"""Client module for dispatching remote builds and streaming logs."""

import getpass
import hashlib
import json
import os
import sys
import threading
import time
from typing import Any, Callable, Optional, Union

import requests
from websockets.exceptions import WebSocketException

from seine.credentials import CredentialNotFound, token_source
from seine.distributed.client.demux import LogDemux
from seine.distributed.common.models import (
    BUILD_OPTION_KEYS,
    BuildSubmitRequest,
    BuildSubmitResponse,
    expired_text,
)
from seine.distributed.common.transport import (
    MAX_TOKEN_REJECTIONS,
    check_server_url,
    requests_verify,
    ws_ssl_context,
)
from seine.distributed.common.wsclient import WsClient, WsClosed
from seine.utils import format_size

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_PROTOCOL = 2
EXIT_CREDENTIALS = 3
EXIT_INTERRUPTED = 130

CLOSE_OK = (1000, 1001, 1005)
TERMINAL_STATES = ("completed", "failed", "cancelled")

CONNECT_TIMEOUT = 10
READ_TIMEOUT = 30
UPLOAD_READ_TIMEOUT = 1800
POLL_INTERVAL = 2.0
DRAIN_GRACE = 1.0
CANCEL_WAIT = 30.0
# Consecutive failed status polls before giving up on the server.
MAX_POLL_FAILURES = 15

# Close codes of the log stream, see seine/distributed/server/ws.py.
STREAM_CLOSE_REASONS = {
    4401: "authentication failed: the server rejected the token",
    4403: "not allowed to follow this build in this project",
    4404: "build not found",
    1007: "the server rejected a message (bad data)",
    1009: "the server rejected a message (too big)",
}

class RemoteError(Exception):
    """A failure to report to the user, with the exit code it maps to."""

    def __init__(self, message: str, code: int = EXIT_PROTOCOL):
        super().__init__(message)
        self.code = code


class _PollFailure(Exception):
    """A status poll that may succeed if retried."""


def _detail(resp: Any) -> str:
    try:
        detail = resp.json().get("detail")
    except (ValueError, AttributeError):
        detail = None
    return str(detail) if detail else ""


def _check_response(resp: Any, what: str) -> None:
    """Raise RemoteError for an error status, saying what failed."""
    code = resp.status_code
    if code < 400:
        return
    detail = _detail(resp)
    if code == 401:
        raise RemoteError(f"{what}: authentication failed, the server rejected the token")
    if code == 403:
        raise RemoteError(f"{what}: not allowed in this project" + (f" ({detail})" if detail else ""))
    raise RemoteError(f"{what}: server answered HTTP {code}" + (f" ({detail})" if detail else ""))


def upload_worktree(
    server_url: str,
    project: str,
    archive_path: str,
    token: Optional[str] = None,
    verify: Union[str, bool] = True,
    env: str = "dev",
) -> dict[str, Any]:
    """Upload a worktree bundle (.tar.zst) to seine-server, staged for dev or prod builds."""
    server_url = server_url.rstrip("/")
    url = f"{server_url}/api/v1/projects/{project}/worktrees"
    headers = {"Content-Type": "application/octet-stream"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    with open(archive_path, "rb") as f:
        resp = requests.post(
            url,
            data=f,
            params={"env": env},
            headers=headers,
            timeout=(CONNECT_TIMEOUT, UPLOAD_READ_TIMEOUT),
            verify=verify,
        )
    _check_response(resp, "worktree upload")
    return resp.json()


def _print_artifacts(artifact_urls: Optional[list[str]], say: Callable[[str], None] = print) -> None:
    if artifact_urls:
        say("\nArtifacts:")
        for artifact in artifact_urls:
            say(f"  - {artifact}")


def _stdout(text: str) -> None:
    sys.stdout.write(text)
    sys.stdout.flush()


def _stderr(text: str) -> None:
    sys.stderr.write(text)


class DownloadError(Exception):
    """An artifact that could not be downloaded or did not verify."""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


def _release_dir(spec_path: str) -> str:
    """Return the default download directory for a specification."""
    import yaml

    release = None
    try:
        with open(spec_path, "r", encoding="utf-8") as f:
            doc = yaml.safe_load(f)
    except (OSError, yaml.YAMLError):
        doc = None
    if isinstance(doc, dict):
        distro = doc.get("distribution")
        if isinstance(distro, dict):
            release = distro.get("release")
        elif isinstance(distro, str):
            release = distro
        release = release or doc.get("release")
    return os.path.join(".", "deploy", str(release)) if release else os.path.join(".", "deploy")


def _destination(name: Any, target_dir: str) -> str:
    """Return where artifact name goes, refusing names that leave target_dir."""
    unsafe = (
        not isinstance(name, str)
        or name in ("", ".", "..")
        or os.path.isabs(name)
        or any(c in name for c in "/\\\0")
    )
    if unsafe:
        raise DownloadError(f"refusing artifact with unsafe name {name!r}")
    root = os.path.realpath(target_dir)
    if os.path.dirname(os.path.realpath(os.path.join(root, name))) != root:
        raise DownloadError(f"refusing artifact {name!r}: it would leave {target_dir}")
    return os.path.join(root, name)


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def _download_artifact(
    name: str,
    url: str,
    expected: dict[str, Any],
    dest: str,
    verify: Union[str, bool],
    insecure: bool,
    progress: Optional[Callable[[int], None]] = None,
) -> None:
    """Stream url to dest through a .part file, checking sha256 and size.

    progress is called with the size of each chunk written.
    """
    try:
        check_server_url(url, insecure=insecure)
    except ValueError as e:
        raise DownloadError(f"{name}: {e}") from e

    part = f"{dest}.part"
    _remove(part)
    sha256 = hashlib.sha256()
    size = 0
    try:
        resp = requests.get(
            url, stream=True, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
            verify=verify, allow_redirects=False,
        )
        try:
            if resp.status_code != 200:
                raise DownloadError(
                    f"{name}: storage answered HTTP {resp.status_code}", resp.status_code)
            with open(part, "wb") as f:
                for chunk in resp.iter_content(chunk_size=65536):
                    f.write(chunk)
                    sha256.update(chunk)
                    size += len(chunk)
                    if progress:
                        progress(len(chunk))
        finally:
            resp.close()
    except (requests.RequestException, OSError, DownloadError) as e:
        _remove(part)
        if isinstance(e, DownloadError):
            raise
        raise DownloadError(f"{name}: {e}") from e

    if sha256.hexdigest() != expected["sha256"].lower() or size != expected["size"]:
        os.replace(part, f"{dest}.corrupt")
        raise DownloadError(
            f"{name} is corrupt (kept as {name}.corrupt): expected sha256 "
            f"{expected['sha256']} and {expected['size']} bytes, "
            f"got {sha256.hexdigest()} and {size} bytes"
        )
    os.replace(part, dest)


def spec_architecture(spec_path: str) -> Optional[str]:
    """Return the architecture a specification sets, None if it sets none."""
    import yaml
    from seine.build import BuildCmd

    try:
        spec = BuildCmd().load_all([spec_path])
    # The loader assumes a mapping and raises these on a malformed file.
    except (OSError, ValueError, yaml.YAMLError, AttributeError, TypeError, KeyError) as e:
        raise RemoteError(f"cannot read specification {spec_path}: {e}") from e
    if not isinstance(spec, dict):
        raise RemoteError(f"cannot read specification {spec_path}: not a YAML mapping")
    distro = spec.get("distribution")
    arch = distro.get("architecture") if isinstance(distro, dict) else None
    return str(arch) if arch else None


class LogFollower(threading.Thread):
    """Print a build's log from the WebSocket stream until told to stop."""

    def __init__(
        self,
        url: str,
        token: str,
        ssl_context: Any = None,
        out: Callable[[str], None] = _stdout,
        err: Callable[[str], None] = _stderr,
        on_event: Optional[Callable[[dict[str, Any]], None]] = None,
        demux: Optional[LogDemux] = None,
    ):
        super().__init__(daemon=True)
        self._write = out
        self._warn = err
        self._on_event = on_event
        self._demux = demux
        self.url = url
        self.token = token
        self.ssl_context = ssl_context
        self.failure: Optional[str] = None
        self._deadline: Optional[float] = None

    def stop(self, grace: float = 0.0) -> None:
        """Stop reading after grace seconds, which drain what is still queued."""
        self._deadline = time.monotonic() + grace

    def _stopped(self) -> bool:
        return self._deadline is not None and time.monotonic() >= self._deadline

    def run(self) -> None:
        ws = WsClient()
        try:
            ws.connect(self.url, self.ssl_context)
            ws.send(json.dumps({"auth": self.token}))
            while not self._stopped():
                try:
                    raw = ws.recv(timeout=0.2)
                except TimeoutError:
                    continue
                self._emit(raw)
        except WsClosed as e:
            if e.code not in CLOSE_OK:
                self._fail(STREAM_CLOSE_REASONS.get(e.code, f"stream closed (code {e.code})"))
        except (OSError, WebSocketException) as e:
            self._fail(f"cannot open the log stream: {e}")
        finally:
            ws.close()
            if self._demux is not None:
                self._demux.close()

    def _emit(self, raw: Union[str, bytes]) -> None:
        try:
            message = json.loads(raw)
        except ValueError:
            return
        if not isinstance(message, dict):
            return
        if "type" in message:
            if self._on_event is not None:
                self._on_event(message)
            return
        text = message.get("text", "")
        # With a demux the log is read back from its files, not echoed.
        if self._demux is not None:
            self._demux.write(text, message.get("task"))
        else:
            self._write(text)

    def _fail(self, reason: str) -> None:
        self.failure = reason
        self._warn(f"\n[client] log stream: {reason}; following the build by polling\n")


def _tty_prompt(context: str, fields: dict, offer_save: bool = False) -> dict:
    """Ask for the token on the terminal; the TUI passes its own prompt."""
    if not sys.stdin.isatty():
        raise CredentialNotFound("no terminal to ask on")
    sys.stderr.write(f"{context}\n")
    values: dict[str, Any] = {}
    for name, (_, secret) in fields.items():
        values[name] = (getpass.getpass if secret else input)(f"{name}: ")
    if offer_save:
        values["_save"] = input("Save it? [Y/n] ").strip().lower() not in ("n", "no")
    return values


def _tty_project_prompt(projects: dict[str, str]) -> tuple[str, bool]:
    """Ask on the terminal for a project and whether to keep it as the default."""
    names = sorted(projects)
    sys.stderr.write("Projects:\n")
    for number, name in enumerate(names, 1):
        sys.stderr.write(f"  {number}) {name} ({projects[name]})\n")
    while True:
        answer = input(f"Project [1-{len(names)}]: ").strip()
        if answer in names:
            chosen = answer
            break
        if answer.isdigit() and 1 <= int(answer) <= len(names):
            chosen = names[int(answer) - 1]
            break
    keep = input("Make it your default? [y/N] ").strip().lower() in ("y", "yes")
    return chosen, keep


class RemoteBuild:
    """One remote build: pack and upload the worktree, submit, follow."""

    def __init__(
        self,
        server_url: str,
        project: Optional[str],
        spec_file: str,
        options: Optional[dict[str, Any]] = None,
        token: Optional[str] = None,
        is_release: bool = False,
        root_dir: Optional[str] = None,
        poll_interval: float = POLL_INTERVAL,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        drain_grace: float = DRAIN_GRACE,
        cancel_wait: float = CANCEL_WAIT,
        out: Callable[[str], None] = _stdout,
        err: Callable[[str], None] = _stderr,
        prompt: Optional[Callable[..., Any]] = None,
        ask_project: Optional[Callable[[dict[str, str]], tuple[str, bool]]] = None,
        on_download: Optional[Callable[[str, str, str, int], None]] = None,
        on_event: Optional[Callable[[dict[str, Any]], None]] = None,
        log_dir: Optional[str] = None,
    ):
        # With log_dir, the streamed log goes there, one file per task, not to out.
        self.log_dir = log_dir
        # out/err/prompt let the TUI take over what would go to the terminal.
        self._out = out
        self._err = err
        self.prompt = prompt
        if ask_project is None and sys.stdin.isatty():
            ask_project = _tty_project_prompt
        self.ask_project = ask_project
        # on_download(kind, build_id, name, n): queue (n = size), start, bytes, done, failed.
        self.on_download = on_download
        # on_event(event): the structured events of the build (task_plan, task_started...).
        self.on_event = on_event
        # Set from another thread to cancel like Ctrl+C would.
        self.stop_requested = threading.Event()
        self.server_url = server_url.rstrip("/")
        self.project = project
        self.options = dict(options or {})
        self.token = token or self.options.get("token") or os.environ.get("SEINE_TOKEN")
        self.ca_cert = self.options.get("ca_cert") or os.environ.get("SEINE_CA_CERT")
        self.insecure = bool(self.options.get("insecure"))
        self.is_release = is_release
        self.root_dir = os.path.abspath(root_dir or os.getcwd())
        self.spec_file = (
            os.path.relpath(spec_file, self.root_dir) if os.path.isabs(spec_file) else spec_file
        )
        self.poll_interval = poll_interval
        self.sleep = sleep
        self.clock = clock
        self.drain_grace = drain_grace
        self.cancel_wait = cancel_wait
        self.verify = requests_verify(self.ca_cert)
        self._build = None

    def _say(self, text: str) -> None:
        self._out(text + "\n")

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def run(self) -> int:
        """Run the build and return the exit code."""
        try:
            self._check_setup()
            self._resolve_project()
            arch = self._target_arch()
            secrets = self._feed_secrets()
            digest = self._upload()
            build_id = self._submit(arch, digest, secrets)
        except RemoteError as e:
            self._err(f"error: {e}\n")
            return e.code
        except KeyboardInterrupt:
            self._say("\n[client] Interrupted before the build was submitted")
            return EXIT_INTERRUPTED

        follower = self._start_log_stream(build_id)
        try:
            try:
                info = self._wait(build_id)
            except KeyboardInterrupt:
                try:
                    return self._cancel(build_id, follower)
                except KeyboardInterrupt:
                    self._say("\n[client] Not waiting for the build to stop")
                    return EXIT_INTERRUPTED
            except RemoteError as e:
                self._err(f"error: {e}\n")
                return e.code
            return self._finish(build_id, info, follower)
        finally:
            follower.stop()

    def _check_setup(self) -> None:
        try:
            check_server_url(self.server_url, insecure=self.insecure)
        except ValueError as e:
            raise RemoteError(str(e)) from e
        if not self.token:
            self._resolve_token()

    def _resolve_token(self) -> None:
        """Find a token (keyring, credentials file, prompt) the server accepts."""
        src = token_source(self.server_url, prompt=self.prompt or _tty_prompt)
        try:
            token = src.get()["token"]
        except CredentialNotFound as e:
            raise RemoteError("no token: pass --token, set SEINE_TOKEN or save one") from e
        self.token = token
        rejected = 0
        while True:
            try:
                resp = requests.get(
                    f"{self.server_url}/api/v1/me", headers=self.headers,
                    timeout=(CONNECT_TIMEOUT, READ_TIMEOUT), verify=self.verify,
                )
            except requests.RequestException as e:
                raise RemoteError(f"cannot reach {self.server_url}: {e}") from e
            if resp.status_code != 401:
                break
            rejected += 1
            if rejected >= MAX_TOKEN_REJECTIONS:
                raise RemoteError(f"authentication failed: {rejected} tokens rejected")
            try:
                self.token = src.failed()["token"]
            except CredentialNotFound as e:
                raise RemoteError(f"authentication failed: no new token ({e})") from e
        _check_response(resp, "authentication")
        src.commit()

    def _resolve_project(self) -> None:
        """Settle on a project: the given one, the server default, the only one, or ask."""
        if self.project:
            return
        profile = self._get("/api/v1/me", "profile")
        roles = profile.get("projects") or {}
        if profile.get("default_project"):
            self._use_project(profile["default_project"], "your default project")
        elif len(roles) == 1 and not profile.get("is_admin"):
            self._use_project(next(iter(roles)), "your only project")
        else:
            self._ask_for_project(profile, roles)

    def _use_project(self, name: str, why: str) -> None:
        self.project = name
        self._say(f"[client] Project: {name} ({why})")

    def _ask_for_project(self, profile: dict[str, Any], roles: dict[str, str]) -> None:
        candidates = dict(roles)
        if profile.get("is_admin"):
            for project in self._get("/api/v1/projects", "project list"):
                candidates.setdefault(project["name"], "admin")
        if not candidates:
            raise RemoteError("no project: you are not a member of any project")
        if self.ask_project is None:
            raise RemoteError("no project: pass --project NAME or set $SEINE_PROJECT")
        try:
            chosen, keep = self.ask_project(candidates)
        except EOFError as e:
            raise RemoteError("no project chosen") from e
        self._use_project(chosen, "chosen")
        if keep:
            self._save_default_project(chosen)

    def _get(self, path: str, what: str) -> Any:
        try:
            resp = requests.get(
                f"{self.server_url}{path}", headers=self.headers,
                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT), verify=self.verify,
            )
        except requests.RequestException as e:
            raise RemoteError(f"cannot reach {self.server_url}: {e}") from e
        if resp.status_code == 404:
            raise RemoteError(f"{what}: not offered by this server, pass --project NAME")
        _check_response(resp, what)
        return resp.json()

    def _save_default_project(self, project: str) -> None:
        try:
            resp = requests.patch(
                f"{self.server_url}/api/v1/me", json={"default_project": project},
                headers=self.headers, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
                verify=self.verify,
            )
            failure = f"HTTP {resp.status_code}" if resp.status_code >= 400 else ""
        except requests.RequestException as e:
            failure = str(e)
        if failure:
            self._err(f"warning: could not save the default project: {failure}\n")
        else:
            self._say(f"[client] {project} is now your default project")

    def _target_arch(self) -> str:
        arch = self.options.get("target_arch")
        if arch:
            self._say(f"[client] Target architecture: {arch} (from the command line)")
            return arch
        arch = spec_architecture(os.path.join(self.root_dir, self.spec_file))
        if arch:
            self._say(f"[client] Target architecture: {arch} (from {self.spec_file})")
            return arch
        from seine.utils import HOST_ARCH

        self._say(
            f"[client] Target architecture: {HOST_ARCH} "
            f"(host architecture, {self.spec_file} sets none)"
        )
        return HOST_ARCH

    def _loaded_build(self):
        """Load and parse the spec once; the secrets and the worktree both need it."""
        if self._build is None:
            from seine.build import BuildCmd

            build = BuildCmd()
            build.load_all([os.path.join(self.root_dir, self.spec_file)])
            build.parse()
            self._build = build
        return self._build

    def _feed_secrets(self) -> dict[str, Any]:
        """Resolve and check the feed credentials the spec needs, as the local build does."""
        import yaml
        from seine import credentials
        from seine.build import collect_credentials
        from seine.utils import feeds

        found: dict[str, dict[str, str]] = {}
        try:
            build = self._loaded_build()
            collect_credentials([build], prompt=self.prompt)
            for feed in feeds(build.spec["distribution"]):
                if feed["auth"] is not None:
                    uri = feed["uri"].rstrip("/")
                    login, password = credentials.resolved_for(uri)
                    found[uri] = {"login": login, "password": password}
        except (credentials.CredentialError, ValueError, OSError, yaml.YAMLError) as e:
            raise RemoteError(f"feed credentials: {e}", EXIT_CREDENTIALS) from e
        finally:
            credentials.clear_resolved()
        if not found:
            return {}
        try:
            check_server_url(self.server_url)
        except ValueError as e:
            raise RemoteError(f"refusing to send feed credentials: {e}") from e
        return {"feeds": found}

    def _pack(self) -> tuple[str, str]:
        """Pack what the spec reads; the whole tree on worktree=full, or on auto if it must."""
        from seine.distributed.client import worktree

        mode = self.options.get("worktree", "auto")
        if mode == "full":
            return worktree.pack_worktree(self.root_dir)
        import yaml
        from seine.build import closure

        try:
            build = self._loaded_build()
            unmodeled = closure.unmodeled([build]) if mode == "auto" else []
            if unmodeled:
                self._say(f"[client] The playbook uses {', '.join(unmodeled)}: sending the "
                          "whole directory (--worktree=sparse to send only what is listed)")
                return worktree.pack_worktree(self.root_dir)
            return worktree.pack_sparse_worktree(self.root_dir, closure.collect([build]))
        except worktree.OutsideRootError as e:
            raise RemoteError(f"worktree: {e}; move it under the project directory") from e
        except (ValueError, OSError, yaml.YAMLError) as e:
            raise RemoteError(f"worktree: {e} (--worktree=full packs the whole tree)") from e

    def _upload(self) -> str:
        self._say(f"[client] Packaging worktree at {self.root_dir}...")
        archive_path, local_digest = self._pack()
        try:
            self._say(f"[client] Uploading worktree bundle ({local_digest})...")
            try:
                resp = upload_worktree(
                    self.server_url, self.project, archive_path,
                    token=self.token, verify=self.verify,
                    env="prod" if self.is_release else "dev",
                )
            except requests.RequestException as e:
                raise RemoteError(f"worktree upload: cannot reach {self.server_url}: {e}") from e
        finally:
            try:
                os.remove(archive_path)
            except OSError:
                pass
        digest = resp.get("digest") if isinstance(resp, dict) else None
        if not digest:
            raise RemoteError("worktree upload: the server returned no digest")
        return digest

    def _submit(self, arch: str, digest: str, secrets: Optional[dict[str, Any]] = None) -> str:
        options = {k: v for k, v in self.options.items() if k in BUILD_OPTION_KEYS}
        # Only sent when on: a server that predates the key refuses it.
        if not options.get("verbose"):
            options.pop("verbose", None)
        req = BuildSubmitRequest(
            project=self.project,
            worktree_digest=digest,
            target_arch=arch,
            is_release=self.is_release,
            spec_file=self.spec_file,
            options=options,
            transient_secrets=secrets or {},
        )
        url = f"{self.server_url}/api/v1/builds"
        self._say(f"[client] Submitting build to {url} (project: {self.project}, target_arch: {arch})...")
        try:
            resp = requests.post(
                url, json=req.model_dump(), headers=self.headers,
                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT), verify=self.verify,
            )
        except requests.RequestException as e:
            raise RemoteError(f"build submission: cannot reach {self.server_url}: {e}") from e
        _check_response(resp, "build submission")
        accepted = BuildSubmitResponse(**resp.json())
        self._say(f"[client] Build accepted as {accepted.build_id} (status: {accepted.status})")
        return accepted.build_id

    def _start_log_stream(self, build_id: str) -> LogFollower:
        ws_url = self.server_url.replace("http", "ws", 1)
        stream_url = f"{ws_url}/api/v1/builds/{build_id}/stream"
        self._say(f"[client] Connecting to live log stream: {stream_url} ...\n" + "-" * 60)
        follower = LogFollower(
            stream_url, self.token, ws_ssl_context(stream_url, self.ca_cert),
            out=self._out, err=self._err, on_event=self.on_event,
            demux=LogDemux(self.log_dir) if self.log_dir else None,
        )
        follower.start()
        return follower

    def _get_build(self, build_id: str) -> dict[str, Any]:
        url = f"{self.server_url}/api/v1/builds/{build_id}"
        try:
            resp = requests.get(
                url, headers=self.headers,
                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT), verify=self.verify,
            )
        except requests.RequestException as e:
            raise _PollFailure(str(e)) from e
        if resp.status_code >= 500:
            raise _PollFailure(f"HTTP {resp.status_code}")
        if resp.status_code == 404:
            raise RemoteError(f"build {build_id} not found")
        _check_response(resp, "build status")
        try:
            return resp.json()
        except ValueError as e:
            raise _PollFailure("invalid status response") from e

    def _wait(self, build_id: str) -> dict[str, Any]:
        """Poll until the build is in a terminal state and return its record."""
        failures = 0
        last = None
        while True:
            try:
                info = self._get_build(build_id)
                failures = 0
            except _PollFailure as e:
                failures += 1
                if failures >= MAX_POLL_FAILURES:
                    raise RemoteError(f"lost contact with {self.server_url}: {e}") from e
            else:
                status = info.get("status")
                if status in TERMINAL_STATES:
                    return info
                if status != last:
                    self._say(f"\n[client] Build {build_id} is {status}")
                    last = status
            self.sleep(self.poll_interval)
            if self.stop_requested.is_set():
                raise KeyboardInterrupt

    def _drain(self, follower: LogFollower) -> None:
        follower.stop(self.drain_grace)
        follower.join(self.drain_grace + 5)

    def _finish(self, build_id: str, info: dict[str, Any], follower: LogFollower) -> int:
        self._drain(follower)
        status = info["status"]
        self._say("-" * 60 + f"\n[client] Build {build_id} finished with status: {status.upper()}")
        if status == "completed":
            return self._deliver(info)
        if info.get("error_message"):
            self._say(f"[client] {info['error_message']}")
        _print_artifacts(info.get("artifact_urls"), self._say)
        return EXIT_FAILED

    def _report_expired(self, info: dict[str, Any]) -> None:
        self._err(
            f"[client] ERROR: artifacts of build {info.get('id')} "
            f"{expired_text(info.get('artifacts_expired_reason'))}; "
            "rebuild to get them again\n"
        )

    def _evicted_meanwhile(self, build_id: str) -> Optional[dict[str, Any]]:
        """Return the build record if its artifacts expired during the download."""
        try:
            info = self._get_build(build_id)
        except (_PollFailure, RemoteError):
            return None
        return info if info.get("artifacts_expired_at") else None

    def _deliver(self, info: dict[str, Any]) -> int:
        """Download and verify the artifacts of a completed build; return the exit code."""
        if info.get("artifacts_expired_at") and not self.options.get("no_download"):
            self._report_expired(info)
            return EXIT_FAILED
        download_urls = info.get("download_urls") or {}
        if self.options.get("no_download") or not download_urls:
            _print_artifacts(info.get("artifact_urls"), self._say)
            return EXIT_OK

        manifest = {
            a["name"]: a for a in info.get("artifacts") or []
            if isinstance(a.get("sha256"), str) and isinstance(a.get("size"), int)
        }
        target_dir = self.options.get("dest_dir") or _release_dir(
            os.path.join(self.root_dir, self.spec_file)
        )
        os.makedirs(target_dir, exist_ok=True)
        done = 0
        build_id = str(info.get("id") or "")

        def event(kind: str, name: str, n: int = 0) -> None:
            if self.on_download:
                self.on_download(kind, build_id, name, n)

        for name in download_urls:
            event("queue", name, (manifest.get(name) or {}).get("size", 0))
        for name, url in download_urls.items():
            expected = manifest.get(name)
            self._out(f"[client] Downloading {name}... ")
            try:
                event("start", name)
                dest = _destination(name, target_dir)
                if expected is None:
                    raise DownloadError(f"{name}: the server reported no checksum, not downloaded")
                _download_artifact(
                    name, url, expected, dest, self.verify, self.insecure,
                    progress=lambda n, name=name: event("bytes", name, n))
            except DownloadError as e:
                event("failed", name)
                self._out("failed\n")
                if e.status == 404 and (expired := self._evicted_meanwhile(build_id)):
                    self._report_expired(expired)
                    return EXIT_FAILED
                self._err(f"[client] ERROR: {e}\n")
                continue
            event("done", name)
            self._say(f"done ({format_size(expected['size'])}, sha256 verified)")
            done += 1

        self._say(f"[client] Downloaded {done} of {len(download_urls)} artifact(s) to {target_dir}")
        return EXIT_OK if done == len(download_urls) else EXIT_FAILED

    def _cancel(self, build_id: str, follower: LogFollower) -> int:
        """Ask the server to cancel after Ctrl+C and wait for it to take effect."""
        self._say(f"\n[client] Interrupted, requesting cancellation of {build_id}...")
        try:
            resp = requests.post(
                f"{self.server_url}/api/v1/builds/{build_id}/cancel",
                headers=self.headers,
                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT), verify=self.verify,
            )
            if resp.status_code >= 400 and resp.status_code != 409:
                _check_response(resp, "cancel request")
        except (requests.RequestException, RemoteError) as e:
            self._err(f"error: cancel request failed: {e}\n")
            return EXIT_INTERRUPTED
        self._say("[client] Cancellation requested; press Ctrl+C again to exit now")
        deadline = self.clock() + self.cancel_wait
        while self.clock() < deadline:
            try:
                status = self._get_build(build_id).get("status")
            except (_PollFailure, RemoteError):
                status = None
            if status in TERMINAL_STATES:
                self._drain(follower)
                self._say(f"[client] Build {build_id} finished with status: {status.upper()}")
                return EXIT_INTERRUPTED
            self.sleep(self.poll_interval)
        self._say(f"[client] Build {build_id} has not stopped yet; it will end on the server")
        return EXIT_INTERRUPTED


def build_remote(
    server_url: str,
    project: Optional[str] = None,
    spec_files: Optional[list[str]] = None,
    options: Optional[dict[str, Any]] = None,
    token: Optional[str] = None,
    is_release: bool = False,
    root_dir: Optional[str] = None,
) -> int:
    """Build the project remotely on seine-server and follow it, returning the exit code."""
    if not spec_files:
        sys.stderr.write("error: remote build expects a specification file\n")
        return EXIT_PROTOCOL
    return RemoteBuild(
        server_url, project, spec_files[0], options=options, token=token,
        is_release=is_release, root_dir=root_dir,
    ).run()
