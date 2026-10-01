# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import os
import shutil
import sys
import tempfile
from unittest import mock

from avocado import Test

from seine.distributed.client.remote import (
    dispatch_remote_build,
    submit_remote_build,
    upload_worktree,
)


class RemoteBuildClientTest(Test):
    """Unit tests for remote build upload, submission, and dispatch."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="seine-test-remote-")

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    @mock.patch("requests.post")
    def test_upload_worktree(self, mock_post):
        mock_resp = mock.MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"digest": "abc123", "status": "staged"}
        mock_post.return_value = mock_resp

        archive_path = os.path.join(self.tmp_dir, "bundle.tar.zst")
        with open(archive_path, "wb") as f:
            f.write(b"dummy archive data")

        res = upload_worktree("http://localhost:8000", "myproj", archive_path, token="tok-123")
        self.assertEqual(res["digest"], "abc123")
        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        self.assertEqual(args[0], "http://localhost:8000/api/v1/projects/myproj/worktrees")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer tok-123")
        self.assertEqual(kwargs["headers"]["Content-Type"], "application/octet-stream")
        self.assertNotIn("files", kwargs)
        self.assertIn("data", kwargs)

    @mock.patch("seine.distributed.client.remote.ws_connect")
    @mock.patch("requests.get")
    @mock.patch("requests.post")
    def test_submit_remote_build_success(self, mock_post, mock_get, mock_ws_connect):
        post_resp = mock.MagicMock()
        post_resp.status_code = 200
        post_resp.json.return_value = {
            "build_id": "bld-test1",
            "status": "queued",
            "project": "proj-a",
            "target_arch": "amd64",
        }
        mock_post.return_value = post_resp

        get_resp = mock.MagicMock()
        get_resp.status_code = 200
        get_resp.json.return_value = {
            "id": "bld-test1",
            "status": "completed",
            "artifact_urls": ["http://s3/output.img"],
        }
        mock_get.return_value = get_resp

        mock_ws = mock.MagicMock()
        mock_ws.recv.side_effect = TimeoutError()
        mock_ws_connect.return_value.__enter__.return_value = mock_ws

        ret = submit_remote_build(
            server_url="http://localhost:8000",
            project="proj-a",
            target_arch="amd64",
            worktree_digest="dig123",
            spec_file="spec.yaml",
            is_release=False,
            token="pat-test",
        )
        self.assertEqual(ret, 0)
        mock_post.assert_called_once()
        self.assertEqual(mock_post.call_args[1]["headers"]["Authorization"], "Bearer pat-test")
        # The viewer authenticates with the user token before reading anything.
        self.assertEqual(mock_ws.send.call_args_list[0], mock.call('{"auth": "pat-test"}'))

    @mock.patch("seine.distributed.client.remote.submit_remote_build")
    @mock.patch("seine.distributed.client.remote.upload_worktree")
    @mock.patch("seine.distributed.client.worktree.pack_worktree")
    def test_dispatch_remote_build(self, mock_pack, mock_upload, mock_submit):
        archive_file = os.path.join(self.tmp_dir, "fake.tar.zst")
        with open(archive_file, "w") as f:
            f.write("content")
        mock_pack.return_value = (archive_file, "digest-xyz")
        mock_upload.return_value = {"status": "staged"}
        mock_submit.return_value = 0

        spec_file = os.path.join(self.tmp_dir, "custom.yaml")
        with open(spec_file, "w") as f:
            f.write("architecture: arm64\n")

        ret = dispatch_remote_build(
            server_url="http://localhost:8000",
            project="proj-x",
            spec_files=[spec_file],
            token="token-abc",
            is_release=True,
            root_dir=self.tmp_dir,
        )
        self.assertEqual(ret, 0)
        mock_pack.assert_called_once_with(self.tmp_dir)
        mock_upload.assert_called_once_with(
            "http://localhost:8000", "proj-x", archive_file, token="token-abc"
        )
        mock_submit.assert_called_once()
        kwargs = mock_submit.call_args[1]
        self.assertEqual(kwargs["target_arch"], "arm64")
        self.assertEqual(kwargs["worktree_digest"], "digest-xyz")
        self.assertEqual(kwargs["is_release"], True)
        self.assertEqual(kwargs["token"], "token-abc")

    @mock.patch("seine.distributed.client.remote.dispatch_remote_build")
    def test_build_cmd_cli_remote_dispatch(self, mock_dispatch):
        from seine.build import BuildCmd

        mock_dispatch.return_value = 0
        cmd = BuildCmd()
        spec_path = os.path.join(self.tmp_dir, "main.yaml")
        with open(spec_path, "w") as f:
            f.write("distribution: debian\n")

        with self.assertRaises(SystemExit) as ctx:
            cmd.main([
                "--remote", "http://server:8000",
                "--project", "clicmd-proj",
                "--token", "secret-token",
                "--release",
                spec_path,
            ])
        self.assertEqual(ctx.exception.code, 0)
        mock_dispatch.assert_called_once()
        kwargs = mock_dispatch.call_args[1]
        self.assertEqual(kwargs["server_url"], "http://server:8000")
        self.assertEqual(kwargs["project"], "clicmd-proj")
        self.assertEqual(kwargs["token"], "secret-token")
        self.assertEqual(kwargs["is_release"], True)
