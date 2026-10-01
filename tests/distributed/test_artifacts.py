# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import hashlib
import os
import shutil
import tempfile
from unittest import mock

from avocado import Test

from seine.distributed.agent.daemon import WorkerAgent
from seine.distributed.agent.executor import (
    DELIVERABLE_PATTERNS,
    SubprocessExecutor,
    harvest_artifacts,
    upload_artifacts,
)
from seine.distributed.common.models import JobManifest
from seine.storage.base import StorageError, StorageNotFoundError, StorageOfflineError
from seine.storage.s3.client import S3Client, S3NotFoundError
from seine.storage.s3.provider import S3StorageProvider


class TestS3ArtifactStorage(Test):
    """Tests for S3StorageProvider artifact push and pull operations."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="seine-test-artifacts-")
        self.mock_client = mock.MagicMock(spec=S3Client)
        self.provider = S3StorageProvider(self.mock_client, "test-bucket")

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_push_artifact_success(self):
        file_path = os.path.join(self.tmp_dir, "pc-image.img")
        with open(file_path, "wb") as f:
            f.write(b"mock image content")

        key = self.provider.push_artifact("my-proj", "bld-001", file_path)
        expected_key = "artifacts/my-proj/bld-001/pc-image.img"
        self.assertEqual(key, expected_key)
        self.mock_client.upload_file.assert_called_once_with("test-bucket", expected_key, file_path)

    def test_push_artifact_info_reports_digest_and_size(self):
        file_path = os.path.join(self.tmp_dir, "pc-image.img")
        with open(file_path, "wb") as f:
            f.write(b"mock image content")
        self.mock_client.upload_file.return_value = "ab" * 32

        info = self.provider.push_artifact_info("my-proj", "bld-001", file_path)
        self.assertEqual(info, {
            "name": "pc-image.img",
            "key": "artifacts/my-proj/bld-001/pc-image.img",
            "sha256": "ab" * 32,
            "size": len(b"mock image content"),
        })

    def test_push_artifact_custom_name(self):
        file_path = os.path.join(self.tmp_dir, "source.raw")
        with open(file_path, "wb") as f:
            f.write(b"raw bytes")

        key = self.provider.push_artifact("my-proj", "bld-001", file_path, artifact_name="custom.raw")
        self.assertEqual(key, "artifacts/my-proj/bld-001/custom.raw")
        self.mock_client.upload_file.assert_called_once_with(
            "test-bucket", "artifacts/my-proj/bld-001/custom.raw", file_path
        )

    def test_push_artifact_missing_file_raises_storage_error(self):
        missing = os.path.join(self.tmp_dir, "nonexistent.img")
        with self.assertRaises(StorageError):
            self.provider.push_artifact("my-proj", "bld-001", missing)

    def test_push_artifact_offline_mode_strict(self):
        file_path = os.path.join(self.tmp_dir, "test.iso")
        with open(file_path, "wb") as f:
            f.write(b"iso bytes")

        strict_provider = S3StorageProvider(self.mock_client, "test-bucket", offline_mode="strict")
        self.mock_client.upload_file.side_effect = Exception("network down")
        with self.assertRaises(StorageOfflineError):
            strict_provider.push_artifact("my-proj", "bld-001", file_path)

    def _serve(self, content, sha256="auto"):
        """Make the mock client serve content, with its sha256 as metadata."""
        meta = {}
        if sha256 == "auto":
            sha256 = hashlib.sha256(content).hexdigest()
        if sha256:
            meta["x-amz-meta-sha256"] = sha256
        self.mock_client.head_object.return_value = meta or {"etag": "x"}

        def download(bucket, key, target):
            with open(target, "wb") as f:
                f.write(content)
        self.mock_client.download_file.side_effect = download

    def test_pull_artifact_success_file_dest(self):
        self._serve(b"image")
        dest_path = os.path.join(self.tmp_dir, "downloaded.img")
        res = self.provider.pull_artifact("my-proj", "bld-001", "pc-image.img", dest_path)
        self.assertEqual(res, dest_path)
        with open(dest_path, "rb") as f:
            self.assertEqual(f.read(), b"image")
        self.assertEqual(self.mock_client.download_file.call_args.args[:2],
                         ("test-bucket", "artifacts/my-proj/bld-001/pc-image.img"))
        self.assertEqual(os.listdir(self.tmp_dir), ["downloaded.img"])

    def test_pull_artifact_success_dir_dest(self):
        self._serve(b"image")
        res = self.provider.pull_artifact("my-proj", "bld-001", "pc-image.img", self.tmp_dir)
        expected = os.path.join(self.tmp_dir, "pc-image.img")
        self.assertEqual(res, expected)
        self.assertTrue(os.path.isfile(expected))

    def test_pull_artifact_sha256_mismatch_leaves_nothing(self):
        self._serve(b"image", sha256="0" * 64)
        dest = os.path.join(self.tmp_dir, "out.img")
        with self.assertRaises(StorageError):
            self.provider.pull_artifact("my-proj", "bld-001", "pc-image.img", dest)
        self.assertEqual(os.listdir(self.tmp_dir), [])

    def test_pull_artifact_missing_sha256_is_refused(self):
        self._serve(b"image", sha256=None)
        dest = os.path.join(self.tmp_dir, "out.img")
        with self.assertRaises(StorageError):
            self.provider.pull_artifact("my-proj", "bld-001", "pc-image.img", dest)
        self.assertEqual(os.listdir(self.tmp_dir), [])

    def test_pull_artifact_missing_object_raises(self):
        self.mock_client.head_object.return_value = None
        dest = os.path.join(self.tmp_dir, "out.img")
        with self.assertRaises(StorageNotFoundError):
            self.provider.pull_artifact("my-proj", "bld-001", "missing.img", dest)
        self.assertEqual(os.listdir(self.tmp_dir), [])

    def test_pull_artifact_not_found_raises(self):
        self.mock_client.download_file.side_effect = S3NotFoundError("not found")
        dest = os.path.join(self.tmp_dir, "out.img")
        with self.assertRaises(StorageNotFoundError):
            self.provider.pull_artifact("my-proj", "bld-001", "missing.img", dest)

    def test_pull_artifact_offline_mode_strict(self):
        strict_provider = S3StorageProvider(self.mock_client, "test-bucket", offline_mode="strict")
        self.mock_client.download_file.side_effect = Exception("connection reset")
        dest = os.path.join(self.tmp_dir, "out.img")
        with self.assertRaises(StorageOfflineError):
            strict_provider.pull_artifact("my-proj", "bld-001", "image.img", dest)


class TestArtifactHarvesting(Test):
    """Tests for deliverable and metadata file harvesting from build trees."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="seine-test-harvest-")

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_harvest_matching_and_ignored_patterns(self):
        matching = [
            "system.img",
            "disk.raw",
            "vm.qcow2",
            "install.iso",
            "base.rootfs.tar",
            "system.img.digest",
            "system.img.recipe",
            "system.boot-signers",
            "system.boot-signers.json",
            "system.sbom.json",
            "system.sbom.spdx",
        ]
        ignored = [
            "app.deb",
            "notes.txt",
            "archive.tar.gz",
            "system.img.partial",
        ]

        for name in matching + ignored:
            p = os.path.join(self.tmp_dir, name)
            with open(p, "wb") as f:
                f.write(b"data")

        harvested = harvest_artifacts(self.tmp_dir)
        basenames = sorted([os.path.basename(p) for p in harvested])
        self.assertEqual(basenames, sorted(matching))

    def test_harvest_from_nested_deploy_hierarchy(self):
        nested = os.path.join(self.tmp_dir, "build", "deploy", "trixie")
        os.makedirs(nested)
        img = os.path.join(nested, "prod.img")
        digest = os.path.join(nested, "prod.img.digest")
        with open(img, "wb") as f, open(digest, "wb") as f2:
            f.write(b"img")
            f2.write(b"digest")

        harvested = harvest_artifacts(self.tmp_dir)
        self.assertEqual(len(harvested), 2)
        self.assertIn(os.path.abspath(img), harvested)
        self.assertIn(os.path.abspath(digest), harvested)

    def test_harvest_empty_or_missing_directory(self):
        self.assertEqual(harvest_artifacts(self.tmp_dir), [])
        missing = os.path.join(self.tmp_dir, "nonexistent")
        self.assertEqual(harvest_artifacts(missing), [])

    def test_executor_harvest_artifacts(self):
        executor = SubprocessExecutor(self.tmp_dir)
        job_deploy = os.path.join(self.tmp_dir, "jobs", "bld-test-01", "build", "deploy", "bookworm")
        os.makedirs(job_deploy)
        qcow2 = os.path.join(job_deploy, "image.qcow2")
        with open(qcow2, "wb") as f:
            f.write(b"qcow2")

        harvested = executor.harvest_artifacts("bld-test-01")
        self.assertEqual(len(harvested), 1)
        self.assertEqual(harvested[0], os.path.abspath(qcow2))


class TestWorkerArtifactReporting(Test):
    """Tests for worker daemon harvesting and reporting artifact_urls to server."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="seine-test-reporting-")

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _make_agent(self):
        with mock.patch("seine.distributed.agent.daemon.detect_capabilities") as mock_detect:
            caps = mock.MagicMock()
            caps.native_arch = "amd64"
            mock_detect.return_value = caps
            agent = WorkerAgent(
                server_url="http://test-server:8000",
                enrollment_token="enroll-tok",
                work_dir=self.tmp_dir,
                worker_id="worker-test-amd64",
                insecure=True,
                report_retry_total=0,
            )
            agent.worker_token = "worker-tok-123"
            return agent

    def test_worker_agent_forwards_artifact_urls_on_completion(self):
        agent = self._make_agent()
        manifest = JobManifest(
            job_id="job-101",
            build_id="bld-101",
            project="test-project",
            target_arch="amd64",
        )

        expected_artifacts = [
            {"name": name, "key": f"artifacts/test-project/bld-101/{name}",
             "sha256": "1" * 64, "size": 7}
            for name in ("pc-image.img", "pc-image.img.digest")
        ]

        with mock.patch("seine.distributed.agent.daemon.LogStreamer"), \
             mock.patch.object(agent.executor, "execute_job", return_value=0), \
             mock.patch.object(agent.executor, "upload_artifacts", return_value=expected_artifacts), \
             mock.patch("requests.post", return_value=mock.MagicMock(status_code=200)) as mock_post:
            agent.run_job(manifest)

        status_calls = [
            call for call in mock_post.call_args_list
            if "/api/v1/workers/jobs/job-101/status" in call[0][0]
        ]
        self.assertEqual(len(status_calls), 1)
        payload = status_calls[0][1]["json"]
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["artifact_urls"], [a["key"] for a in expected_artifacts])
        self.assertEqual(payload["artifacts"], expected_artifacts)

    def test_worker_agent_does_not_upload_on_failure(self):
        agent = self._make_agent()
        manifest = JobManifest(
            job_id="job-102",
            build_id="bld-102",
            project="test-project",
            target_arch="amd64",
        )

        with mock.patch("seine.distributed.agent.daemon.LogStreamer"), \
             mock.patch.object(agent.executor, "execute_job", return_value=1), \
             mock.patch.object(agent.executor, "upload_artifacts") as mock_upload, \
             mock.patch("requests.post", return_value=mock.MagicMock(status_code=200)) as mock_post:
            agent.run_job(manifest)

        mock_upload.assert_not_called()
        status_calls = [
            call for call in mock_post.call_args_list
            if "/api/v1/workers/jobs/job-102/status" in call[0][0]
        ]
        self.assertEqual(len(status_calls), 1)
        payload = status_calls[0][1]["json"]
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["artifact_urls"], [])
        self.assertEqual(payload["artifacts"], [])

    def test_update_job_status_includes_artifact_urls(self):
        agent = self._make_agent()
        with mock.patch("requests.post", return_value=mock.MagicMock(status_code=200)) as mock_post:
            agent.update_job_status(
                "job-103",
                "bld-103",
                "completed",
                artifacts=[{"name": "out.iso", "key": "artifacts/proj/bld-103/out.iso",
                            "sha256": "2" * 64, "size": 3}],
            )

        payload = mock_post.call_args[1]["json"]
        self.assertEqual(payload["artifact_urls"], ["artifacts/proj/bld-103/out.iso"])
        self.assertEqual(payload["artifacts"][0]["sha256"], "2" * 64)

    def test_executor_upload_artifacts_pushes_all_files(self):
        executor = SubprocessExecutor(self.tmp_dir)
        job_deploy = os.path.join(self.tmp_dir, "jobs", "bld-104", "build", "deploy")
        os.makedirs(job_deploy)
        with open(os.path.join(job_deploy, "disk.img"), "wb") as f, \
             open(os.path.join(job_deploy, "disk.img.digest"), "wb") as f2:
            f.write(b"disk")
            f2.write(b"digest")

        mock_provider = mock.MagicMock()
        mock_provider.push_artifact_info.side_effect = lambda proj, bld, path, artifact_name=None: {
            "name": artifact_name, "key": f"artifacts/{proj}/{bld}/{artifact_name}",
            "sha256": "3" * 64, "size": os.path.getsize(path),
        }

        manifest = JobManifest(
            job_id="job-104",
            build_id="bld-104",
            project="demo-proj",
            target_arch="amd64",
        )

        uploaded = executor.upload_artifacts(manifest, provider=mock_provider)
        self.assertEqual(len(uploaded), 2)
        keys = [u["key"] for u in uploaded]
        self.assertIn("artifacts/demo-proj/bld-104/disk.img", keys)
        self.assertIn("artifacts/demo-proj/bld-104/disk.img.digest", keys)
        self.assertEqual(mock_provider.push_artifact_info.call_count, 2)

    def test_uploaded_artifacts_carry_the_files_sha256_and_size(self):
        deploy = os.path.join(self.tmp_dir, "deploy")
        os.makedirs(deploy)
        payload = os.urandom(300_000)
        with open(os.path.join(deploy, "disk.img"), "wb") as f:
            f.write(payload)
        client = S3Client("http://localhost:1", "ak", "sk")
        client._s3.upload_file = mock.MagicMock()
        provider = S3StorageProvider(client, "bucket")

        [info] = upload_artifacts(self.tmp_dir, "demo", "bld-107", provider=provider)

        self.assertEqual(info, {
            "name": "disk.img",
            "key": "artifacts/demo/bld-107/disk.img",
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size": len(payload),
        })
        sent = client._s3.upload_file.call_args.kwargs["ExtraArgs"]["Metadata"]
        self.assertEqual(sent["sha256"], info["sha256"])

    def test_upload_artifacts_stops_at_first_failure(self):
        deploy = os.path.join(self.tmp_dir, "deploy")
        os.makedirs(deploy)
        for name in ("a.img", "b.img"):
            with open(os.path.join(deploy, name), "wb") as f:
                f.write(b"x")
        provider = mock.MagicMock()
        provider.push_artifact_info.side_effect = StorageError("bucket gone")
        logs = []

        with self.assertRaises(StorageError):
            upload_artifacts(self.tmp_dir, "demo", "bld-105", provider=provider,
                             on_log=lambda src, line: logs.append(line))
        self.assertEqual(provider.push_artifact_info.call_count, 1)
        self.assertTrue(any("bucket gone" in line for line in logs))

    def test_worker_agent_fails_job_when_upload_fails(self):
        agent = self._make_agent()
        manifest = JobManifest(
            job_id="job-106",
            build_id="bld-106",
            project="test-project",
            target_arch="amd64",
        )

        with mock.patch("seine.distributed.agent.daemon.LogStreamer"), \
             mock.patch.object(agent.executor, "execute_job", return_value=0), \
             mock.patch.object(agent.executor, "upload_artifacts",
                               side_effect=StorageError("bucket gone")), \
             mock.patch("requests.post", return_value=mock.MagicMock(status_code=200)) as mock_post:
            agent.run_job(manifest)

        status_calls = [
            call for call in mock_post.call_args_list
            if "/api/v1/workers/jobs/job-106/status" in call[0][0]
        ]
        self.assertEqual(len(status_calls), 1)
        payload = status_calls[0][1]["json"]
        self.assertEqual(payload["status"], "failed")
        self.assertIn("bucket gone", payload["error_message"])
        self.assertEqual(payload["artifact_urls"], [])
