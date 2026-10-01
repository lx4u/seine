# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0
"""Tests for how the agent hands a job's feed credentials to its build."""

import io
import json
import os
import shutil
import stat
import tempfile
from unittest import mock

from avocado import Test

from seine import vault
from seine.distributed.agent.daemon import WorkerAgent
from seine.credentials import FEED_AUTH_ENV
from seine.distributed.agent.executor import (
    SubprocessExecutor,
    child_env,
    harvest_artifacts,
)
from seine.distributed.common.models import JobManifest, JobS3

URI = "https://repo.example/debian"
LOGIN, PASSWORD = "alice-feed-login", "hunter2-feed-password"
FEEDS = {"feeds": {URI: {"login": LOGIN, "password": PASSWORD}}}
S3 = JobS3(endpoint="https://s3.example", bucket="p-dev", access_key="GKAK", secret_key="s3-sk")


def manifest(secrets=FEEDS, build_id="bld-1"):
    return JobManifest(
        job_id="job-1", build_id=build_id, project="p", spec_file="main.yaml",
        worktree_digest="dgst", s3=S3, transient_secrets=secrets,
    )


class ExecutorTest(Test):
    def setUp(self):
        self.tmp_dir = os.path.realpath(tempfile.mkdtemp(prefix="seine-test-feedauth-"))
        self.ex = SubprocessExecutor(self.tmp_dir)
        self.job_dir = os.path.join(self.tmp_dir, "jobs", "bld-1")
        self.secrets_dir = os.path.join(self.tmp_dir, "secrets", "bld-1")
        self.seen = {}

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def run_job(self, m=None, wait=0, popen=None):
        """Run execute_job with a fake build; record what the child could see."""
        m = m or manifest()

        def pull(_m, job_dir):
            with open(os.path.join(job_dir, "main.yaml"), "w") as f:
                f.write("name: test\n")

        def start(cmd, **kwargs):
            env = kwargs["env"]
            path = env.get(FEED_AUTH_ENV)
            self.seen.update(cmd=cmd, env=env, path=path)
            if path:
                self.seen["file_mode"] = stat.S_IMODE(os.stat(path).st_mode)
                self.seen["dir_mode"] = stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode)
                with open(path) as f:
                    self.seen["content"] = json.load(f)
            proc = mock.MagicMock()
            proc.stdout, proc.stderr = io.StringIO(""), io.StringIO("")
            return proc

        with mock.patch.object(self.ex, "_pull_worktree", side_effect=pull), \
                mock.patch.object(self.ex, "cleanup_containers"), \
                mock.patch.object(self.ex, "_wait", side_effect=wait if callable(wait) else None,
                                  return_value=wait if not callable(wait) else None), \
                mock.patch("subprocess.Popen", side_effect=popen or start):
            return self.ex.execute_job(m, on_log=lambda s, t: None)

    def test_the_file_is_private_outside_the_job_dir_and_holds_the_feeds(self):
        self.run_job()
        path = self.seen["path"]
        self.assertEqual(path, os.path.join(self.secrets_dir, "feeds.json"))
        self.assertEqual(self.seen["file_mode"], 0o600)
        self.assertEqual(self.seen["dir_mode"], 0o700)
        self.assertEqual(self.seen["content"], FEEDS["feeds"])
        self.assertFalse(path.startswith(self.job_dir + os.sep))

    def test_secrets_are_neither_on_the_command_line_nor_in_the_environment_values(self):
        self.run_job()
        for text in (" ".join(self.seen["cmd"]), " ".join(self.seen["env"].values())):
            self.assertNotIn(LOGIN, text)
            self.assertNotIn(PASSWORD, text)

    def test_the_file_is_removed_after_success(self):
        self.assertEqual(self.run_job(wait=0), 0)
        self.assertFalse(os.path.exists(self.secrets_dir))

    def test_the_file_is_removed_after_failure(self):
        self.assertEqual(self.run_job(wait=3), 3)
        self.assertFalse(os.path.exists(self.secrets_dir))

    def test_the_file_is_removed_when_the_process_cannot_start(self):
        self.assertEqual(self.run_job(popen=FileNotFoundError("no binary")), 1)
        self.assertFalse(os.path.exists(self.secrets_dir))

    def test_the_file_is_removed_after_cancellation(self):
        self.ex.cancel()
        self.run_job(wait=-15)
        self.assertFalse(os.path.exists(self.secrets_dir))

    def test_the_file_is_removed_after_a_crash(self):
        def crash(*args):
            raise RuntimeError("kaboom")

        with self.assertRaises(RuntimeError):
            self.run_job(wait=crash)
        self.assertFalse(os.path.exists(self.secrets_dir))

    def test_a_job_without_feeds_gets_no_file_and_no_variable(self):
        self.run_job(manifest(secrets={}))
        self.assertNotIn(FEED_AUTH_ENV, self.seen["env"])
        self.assertFalse(os.path.exists(os.path.join(self.tmp_dir, "secrets")))

    def test_the_artifact_harvest_never_includes_the_file(self):
        self.ex.write_feed_secrets(manifest())
        os.makedirs(os.path.join(self.job_dir, "build", "deploy"))
        with open(os.path.join(self.job_dir, "build", "deploy", "a.img"), "w") as f:
            f.write("x")
        found = harvest_artifacts(self.job_dir)
        self.assertEqual([os.path.basename(p) for p in found], ["a.img"])
        self.assertFalse(any("secrets" in p or "feeds.json" in p for p in found))

    def test_a_build_id_cannot_reach_another_directory(self):
        keep = os.path.join(self.tmp_dir, "secrets", "other")
        os.makedirs(keep)
        self.ex.remove_feed_secrets("../secrets/other")
        self.assertTrue(os.path.isdir(keep))

    def test_removing_every_build_clears_leftovers(self):
        self.ex.write_feed_secrets(manifest(build_id="bld-old"))
        self.ex.write_feed_secrets(manifest(build_id="bld-older"))
        self.ex.remove_feed_secrets()
        self.assertFalse(os.path.exists(os.path.join(self.tmp_dir, "secrets")))


class ChildEnvTest(Test):
    def test_the_variable_name_is_not_taken_for_a_secret_by_the_filter(self):
        env = child_env(manifest(), "/job/build", environ={}, feedauth_file="/w/secrets/b/feeds.json")
        self.assertEqual(env[FEED_AUTH_ENV], "/w/secrets/b/feeds.json")

    def test_a_value_inherited_from_the_agent_never_passes(self):
        env = child_env(manifest(), "/job/build", environ={FEED_AUTH_ENV: "/etc/shadow"})
        self.assertNotIn(FEED_AUTH_ENV, env)


class DaemonTest(Test):
    def setUp(self):
        self.tmp_dir = os.path.realpath(tempfile.mkdtemp(prefix="seine-test-feedauth-agent-"))
        vault.clear_secrets()
        with mock.patch("seine.distributed.agent.daemon.detect_capabilities") as detect:
            detect.return_value = mock.MagicMock(native_arch="amd64", free_disk_gb=10.0)
            self.agent = WorkerAgent("http://localhost:8000", "enroll", self.tmp_dir, "w1")
        self.agent.worker_token = "wt"

    def tearDown(self):
        vault.clear_secrets()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_secrets_are_registered_before_the_first_log_line_can_be_streamed(self):
        sent, registered = [], []

        def connect(*args):
            registered.extend(vault.secrets())
            return mock.MagicMock(send=lambda s, t: sent.append(t))

        def execute(m, on_log):
            on_log("stderr", f"401 for {LOGIN}:{PASSWORD}\n")
            return 1

        with mock.patch("seine.distributed.agent.daemon.LogStreamer") as streamer:
            streamer.return_value.__enter__.side_effect = connect
            with mock.patch.object(self.agent.executor, "execute_job", side_effect=execute), \
                    mock.patch.object(self.agent, "update_job_status"):
                self.agent.run_job(manifest())
        self.assertIn(LOGIN, registered)
        self.assertIn(PASSWORD, registered)
        self.assertEqual(sent, ["401 for <redacted>:<redacted>\n"])

    def test_start_removes_secrets_a_crashed_agent_left(self):
        leftover = os.path.join(self.tmp_dir, "secrets", "bld-old")
        os.makedirs(leftover)
        with open(os.path.join(leftover, "feeds.json"), "w") as f:
            f.write("{}")
        with mock.patch.object(self.agent, "register", side_effect=RuntimeError("stop")), \
                mock.patch("signal.signal"):
            with self.assertRaises(RuntimeError):
                self.agent.start()
        self.assertFalse(os.path.exists(os.path.join(self.tmp_dir, "secrets")))
