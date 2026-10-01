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
    )

    url = f"{server_url}/api/v1/builds"
    print(f"[client] Submitting build to {url} (project: {project}, target_arch: {target_arch})...")
    resp = requests.post(url, json=req.model_dump(), timeout=10)
    resp.raise_for_status()
    sub_resp = BuildSubmitResponse(**resp.json())
    build_id = sub_resp.build_id
    print(f"[client] Build accepted as {build_id} (status: {sub_resp.status})")

    # Connect to WebSocket log stream
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

                # Check if build completed
                b_resp = requests.get(f"{server_url}/api/v1/builds/{build_id}", timeout=5)
                if b_resp.status_code == 200:
                    status = b_resp.json().get("status")
                    if status in ("completed", "failed", "cancelled"):
                        # Drain any remaining buffered logs briefly
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
                        return 0 if status == "completed" else 1

    except Exception as e:
        print(f"\n[client] Stream connection closed: {e}")

    # Final status check
    b_resp = requests.get(f"{server_url}/api/v1/builds/{build_id}", timeout=5)
    if b_resp.status_code == 200:
        status = b_resp.json().get("status")
        print("-" * 60 + f"\n[client] Final status: {status.upper()}")
        return 0 if status == "completed" else 1

    return 1
