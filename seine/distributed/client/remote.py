# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0
"""Client module for dispatching remote builds and streaming logs."""

import json
import os
import sys
import time
from typing import Any, Optional

import requests
from websockets.sync.client import connect as ws_connect

from seine.distributed.common.models import BuildSubmitRequest, BuildSubmitResponse


def upload_worktree(
    server_url: str,
    project: str,
    archive_path: str,
    token: Optional[str] = None,
) -> dict[str, Any]:
    """Upload a worktree bundle (.tar.zst) to seine-server."""
    server_url = server_url.rstrip("/")
    url = f"{server_url}/api/v1/projects/{project}/worktrees"
    headers = {"Content-Type": "application/octet-stream"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    with open(archive_path, "rb") as f:
        resp = requests.post(url, data=f, headers=headers)
    resp.raise_for_status()
    return resp.json()


def submit_remote_build(
    server_url: str,
    project: str,
    target_arch: str = "amd64",
    worktree_digest: str = "poc-digest",
    spec_file: str = "spec.yaml",
    is_release: bool = False,
    options: Optional[dict[str, Any]] = None,
    token: Optional[str] = None,
    transient_secrets: Optional[dict[str, Any]] = None,
) -> int:
    """Submit a build to seine-server and stream terminal logs via WebSocket."""
    server_url = server_url.rstrip("/")
    req = BuildSubmitRequest(
        project=project,
        worktree_digest=worktree_digest,
        target_arch=target_arch,
        is_release=is_release,
        spec_file=spec_file,
        options=options or {},
        transient_secrets=transient_secrets or {},
    )

    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    url = f"{server_url}/api/v1/builds"
    print(f"[client] Submitting build to {url} (project: {project}, target_arch: {target_arch})...")
    resp = requests.post(url, json=req.model_dump(), headers=headers, timeout=10)
    resp.raise_for_status()
    sub_resp = BuildSubmitResponse(**resp.json())
    build_id = sub_resp.build_id
    print(f"[client] Build accepted as {build_id} (status: {sub_resp.status})")

    ws_url = server_url.replace("http://", "ws://").replace("https://", "wss://")
    stream_url = f"{ws_url}/api/v1/builds/{build_id}/stream"
    print(f"[client] Connecting to live log stream: {stream_url} ...\n" + "-" * 60)

    try:
        with ws_connect(stream_url) as ws:
            ws.send(json.dumps({"auth": token or ""}))
            while True:
                try:
                    message = ws.recv(timeout=1.0)
                    data = json.loads(message)
                    text = data.get("text", "")
                    sys.stdout.write(text)
                    sys.stdout.flush()
                except TimeoutError:
                    pass
                except Exception:
                    pass

                b_resp = requests.get(f"{server_url}/api/v1/builds/{build_id}", headers=headers, timeout=5)
                if b_resp.status_code == 200:
                    status = b_resp.json().get("status")
                    if status in ("completed", "failed", "cancelled"):
                        time.sleep(0.5)
                        try:
                            while True:
                                message = ws.recv(timeout=0.2)
                                data = json.loads(message)
                                sys.stdout.write(data.get("text", ""))
                                sys.stdout.flush()
                        except Exception:
                            pass
                        print("-" * 60 + f"\n[client] Build {build_id} finished with status: {status.upper()}")
                        artifacts = b_resp.json().get("artifact_urls") or []
                        if artifacts:
                            print("\nArtifacts:")
                            for artifact in artifacts:
                                print(f"  - {artifact}")
                        return 0 if status == "completed" else 1

    except Exception as e:
        print(f"\n[client] Stream connection closed: {e}")

    b_resp = requests.get(f"{server_url}/api/v1/builds/{build_id}", headers=headers, timeout=5)
    if b_resp.status_code == 200:
        status = b_resp.json().get("status")
        print("-" * 60 + f"\n[client] Final status: {status.upper()}")
        artifacts = b_resp.json().get("artifact_urls") or []
        if artifacts:
            print("\nArtifacts:")
            for artifact in artifacts:
                print(f"  - {artifact}")
        return 0 if status == "completed" else 1

    return 1


def dispatch_remote_build(
    server_url: str,
    project: str = "default",
    spec_files: Optional[list[str]] = None,
    options: Optional[dict[str, Any]] = None,
    token: Optional[str] = None,
    is_release: bool = False,
    root_dir: Optional[str] = None,
) -> int:
    """Pack local worktree, upload bundle to server, and dispatch build."""
    root_dir = os.path.abspath(root_dir or os.getcwd())
    spec_files = spec_files or []
    if not spec_files:
        sys.stderr.write("error: remote build expects a specification file\n")
        return 1

    spec_path = spec_files[0]
    rel_spec = os.path.relpath(spec_path, root_dir) if os.path.isabs(spec_path) else spec_path

    target_arch = (options or {}).get("target_arch")
    if not target_arch:
        full_spec = os.path.join(root_dir, rel_spec)
        if os.path.exists(full_spec):
            try:
                import yaml
                with open(full_spec, "r", encoding="utf-8") as f:
                    doc = yaml.safe_load(f)
                    if isinstance(doc, dict):
                        target_arch = doc.get("architecture")
                        if not target_arch:
                            for req in doc.get("requires", []):
                                if "arm64" in str(req) or "aarch64" in str(req):
                                    target_arch = "arm64"
                                    break
                                elif "amd64" in str(req) or "x86_64" in str(req):
                                    target_arch = "amd64"
                                    break
            except Exception:
                pass
    if not target_arch:
        import platform
        mach = platform.machine()
        target_arch = "arm64" if mach in ("aarch64", "arm64") else "amd64"

    from seine.distributed.client.worktree import pack_worktree
    print(f"[client] Packaging worktree at {root_dir}...")
    archive_path, digest = pack_worktree(root_dir)

    try:
        print(f"[client] Uploading worktree bundle ({digest})...")
        upload_resp = upload_worktree(server_url, project, archive_path, token=token)
        if isinstance(upload_resp, dict) and upload_resp.get("digest"):
            digest = upload_resp["digest"]
    finally:
        if os.path.exists(archive_path):
            try:
                os.remove(archive_path)
            except Exception:
                pass

    return submit_remote_build(
        server_url=server_url,
        project=project,
        target_arch=target_arch,
        worktree_digest=digest,
        spec_file=rel_spec,
        is_release=is_release,
        options=options,
        token=token,
    )
