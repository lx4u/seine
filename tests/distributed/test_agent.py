# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import argparse
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from unittest import mock

from avocado import Test

from seine.distributed.agent.detect import detect_capabilities, detect_native_arch, ARCH_MAP
from seine.distributed.agent.executor import SubprocessExecutor, child_env, find_seine_binary
from seine.distributed.common.models import JobManifest, JobS3
from seine.distributed.common.transport import (
    check_server_url,
    is_loopback,
    requests_verify,
    ws_ssl_context,
)


class DetectCapabilitiesTest(Test):
    """Tests for hardware and capability discovery."""

    def test_native_arch_matches_platform(self):
        arch = detect_native_arch()
        machine = platform.machine()
        expected = ARCH_MAP.get(machine, machine)
        self.assertEqual(arch, expected)

    def test_capabilities_native_arch_is_string(self):
        caps = detect_capabilities()
        self.assertIsInstance(caps.native_arch, str)
        self.assertGreater(len(caps.native_arch), 0)

    def test_capabilities_arch_scores_contains_native(self):
        caps = detect_capabilities()
        self.assertIn(caps.native_arch, caps.arch_scores)
        self.assertEqual(caps.arch_scores[caps.native_arch], 1.0)

    def test_capabilities_free_disk_positive(self):
        caps = detect_capabilities()
        self.assertGreater(caps.free_disk_gb, 0.0)

    def test_capabilities_work_dir_measured(self):
        tmp = tempfile.mkdtemp(prefix="seine-test-detect-")
        try:
            caps = detect_capabilities(work_dir=tmp)
            self.assertGreater(caps.free_disk_gb, 0.0)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_capabilities_tools_dict_present(self):
        caps = detect_capabilities()
        self.assertIsInstance(caps.tools, dict)
        # podman and kvm keys are always populated (True or False)
        self.assertIn("podman", caps.tools)
        self.assertIn("kvm", caps.tools)

    def test_capabilities_concurrency_slots_positive(self):
        caps = detect_capabilities()
        self.assertGreaterEqual(caps.concurrency_slots, 1)


class FindSeineBinaryTest(Test):
    """Tests for seine binary discovery in find_seine_binary."""

    def test_returns_string(self):
        result = find_seine_binary()
        self.assertIsInstance(result, str)
        self.assertGreater(len(result), 0)

    def test_venv_candidate_preferred(self):
        # Simulate a seine binary sitting next to the interpreter.
        fake_bin = os.path.join(os.path.dirname(sys.executable), "seine")
        with mock.patch("os.path.exists", side_effect=lambda p: p == fake_bin):
            result = find_seine_binary()
        self.assertEqual(result, fake_bin)

    def test_falls_back_to_path(self):
        with mock.patch("os.path.exists", return_value=False), \
             mock.patch("shutil.which", return_value="/usr/bin/seine"):
            result = find_seine_binary()
        self.assertEqual(result, "/usr/bin/seine")

    def test_falls_back_to_name_when_not_on_path(self):
        with mock.patch("os.path.exists", return_value=False), \
             mock.patch("shutil.which", return_value=None):
            result = find_seine_binary()
        self.assertEqual(result, "seine")

JOB_S3 = JobS3(
    endpoint="https://s3.example", region="garage", bucket="proj-dev",
    access_key="GKJOBKEY", secret_key="job-secret-value",
)


class SubprocessExecutorTest(Test):
    """Tests for SubprocessExecutor setup and environment construction."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="seine-test-exec-")

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_instantiation(self):
        ex = SubprocessExecutor(self.tmp_dir)
        self.assertEqual(ex.work_dir, os.path.abspath(self.tmp_dir))

    def test_build_dir_created(self):
        ex = SubprocessExecutor(self.tmp_dir)
        manifest = JobManifest(
            job_id="job-1",
            build_id="bld-test-001",
            project="test-proj",
            spec_file="nonexistent.yaml",
        )
        logs = []
        ret = ex.execute_job(manifest, on_log=lambda s, t: logs.append((s, t)))
        # No spec file → executor returns 1 and logs a message
        self.assertEqual(ret, 1)
        build_dir = os.path.join(self.tmp_dir, "jobs", "bld-test-001", "build")
        self.assertTrue(os.path.isdir(build_dir))

    def _execute(self, ex=None, spec_file="main.yaml", options=None, digest="dgst", s3=JOB_S3):
        """Run execute_job with a fake worktree pull and a mocked Popen."""
        ex = ex or SubprocessExecutor(self.tmp_dir)
        manifest = JobManifest(
            job_id="job-1", build_id="bld-1", project="proj",
            spec_file=spec_file, worktree_digest=digest, options=options or {}, s3=s3,
        )

        def pull(_manifest, job_dir):
            with open(os.path.join(job_dir, "main.yaml"), "w") as f:
                f.write("name: test\n")

        logs = []
        with mock.patch.object(ex, "_pull_worktree", side_effect=pull), \
                mock.patch.object(ex, "cleanup_containers"), \
                mock.patch("subprocess.Popen", side_effect=FileNotFoundError("no binary")) as popen:
            ret = ex.execute_job(manifest, on_log=lambda s, t: logs.append(t))
        return ret, popen, logs

    def test_child_gets_pythonunbuffered_and_build_vars(self):
        _, popen, _ = self._execute()
        env = popen.call_args.kwargs["env"]
        self.assertEqual(env["PYTHONUNBUFFERED"], "1")
        self.assertEqual(env["SEINE_BUILD_ID"], "bld-1")
        self.assertEqual(env["SEINE_BUILD_DIR"], os.path.join(self.tmp_dir, "jobs", "bld-1", "build"))

    def test_child_runs_the_relative_spec_from_the_job_dir(self):
        _, popen, _ = self._execute()
        self.assertEqual(popen.call_args.args[0][-2:], ["build", "main.yaml"])
        self.assertEqual(popen.call_args.kwargs["cwd"], os.path.join(self.tmp_dir, "jobs", "bld-1"))
        self.assertTrue(popen.call_args.kwargs["start_new_session"])

    def test_worktree_pull_failure_fails_the_job(self):
        ex = SubprocessExecutor(self.tmp_dir)
        manifest = JobManifest(job_id="j", build_id="bld-2", project="p", worktree_digest="dgst", s3=JOB_S3)
        logs = []
        with mock.patch.object(ex, "_pull_worktree", side_effect=RuntimeError("bucket gone")), \
                mock.patch("subprocess.Popen") as popen:
            ret = ex.execute_job(manifest, on_log=lambda s, t: logs.append(t))
        self.assertEqual(ret, 1)
        popen.assert_not_called()
        self.assertIn("bucket gone", "".join(logs))

    def test_job_without_s3_access_fails_before_any_pull(self):
        ex = SubprocessExecutor(self.tmp_dir)
        manifest = JobManifest(job_id="j", build_id="bld-3", project="p", worktree_digest="dgst")
        logs = []
        with mock.patch.object(ex, "_pull_worktree") as pull, mock.patch("subprocess.Popen") as popen:
            ret = ex.execute_job(manifest, on_log=lambda s, t: logs.append(t))
        self.assertEqual(ret, 1)
        pull.assert_not_called()
        popen.assert_not_called()
        self.assertIn("without S3 access", "".join(logs))

    def test_worktree_is_pulled_with_the_job_key_only(self):
        ex = SubprocessExecutor(self.tmp_dir)
        manifest = JobManifest(job_id="j", build_id="bld-4", project="p", worktree_digest="dgst", s3=JOB_S3)
        with mock.patch("seine.distributed.agent.executor.provider_from") as provider, \
                mock.patch("seine.storage.for_build") as ambient:
            ex._pull_worktree(manifest, self.tmp_dir)
        provider.assert_called_once_with(JOB_S3)
        provider.return_value.pull_worktree.assert_called_once_with("p", "dgst", dest_dir=self.tmp_dir)
        ambient.assert_not_called()

    def test_missing_worktree_fails_the_job_with_a_precise_error(self):
        ex = SubprocessExecutor(self.tmp_dir)
        manifest = JobManifest(job_id="j", build_id="bld-5", project="p", worktree_digest="dgst", s3=JOB_S3)
        logs = []
        with mock.patch("seine.distributed.agent.executor.provider_from") as provider, \
                mock.patch("subprocess.Popen") as popen:
            provider.return_value.pull_worktree.return_value = None
            ret = ex.execute_job(manifest, on_log=lambda s, t: logs.append(t))
        self.assertEqual(ret, 1)
        popen.assert_not_called()
        message = f"worktree dgst not found in bucket {JOB_S3.bucket}"
        self.assertIn(message, "".join(logs))
        self.assertIn(message, ex.failure_reason)
        self.assertNotIn("not found in the job directory", "".join(logs))

    def test_s3_cache_job_passes_scoped_flags_and_env_not_keys_on_the_command_line(self):
        _, popen, _ = self._execute(options={"s3_cache": True})
        cmd = popen.call_args.args[0]
        self.assertIn("--s3-cache", cmd)
        self.assertIn("--s3-endpoint=https://s3.example", cmd)
        self.assertIn("--s3-bucket=proj-dev", cmd)
        self.assertIn("--s3-region=garage", cmd)
        self.assertNotIn(JOB_S3.access_key, " ".join(cmd))
        self.assertNotIn(JOB_S3.secret_key, " ".join(cmd))
        env = popen.call_args.kwargs["env"]
        self.assertEqual(env["AWS_ACCESS_KEY_ID"], JOB_S3.access_key)
        self.assertEqual(env["AWS_SECRET_ACCESS_KEY"], JOB_S3.secret_key)

    def test_job_without_s3_cache_gets_no_s3_flags_or_keys(self):
        _, popen, _ = self._execute()
        self.assertFalse([a for a in popen.call_args.args[0] if a.startswith("--s3")])
        self.assertNotIn("AWS_ACCESS_KEY_ID", popen.call_args.kwargs["env"])

    def test_empty_worktree_digest_fails_the_job(self):
        ret, popen, logs = self._execute(digest="")
        self.assertEqual(ret, 1)
        popen.assert_not_called()
        self.assertIn("no worktree digest", "".join(logs))

    def test_spec_outside_the_job_dir_is_refused_without_a_process(self):
        os.makedirs(os.path.join(self.tmp_dir, "jobs", "bld-1"))
        outside = os.path.join(self.tmp_dir, "outside.yaml")
        with open(outside, "w") as f:
            f.write("name: evil\n")
        os.symlink(outside, os.path.join(self.tmp_dir, "jobs", "bld-1", "link.yaml"))
        for spec in (outside, "../../outside.yaml", "link.yaml", "missing.yaml", "."):
            ret, popen, logs = self._execute(spec_file=spec)
            self.assertEqual(ret, 1, spec)
            popen.assert_not_called()
            self.assertIn("Refusing job", "".join(logs))

    def test_spec_in_a_subdirectory_is_accepted(self):
        job_dir = os.path.join(self.tmp_dir, "jobs", "bld-1")
        os.makedirs(os.path.join(job_dir, "examples", "pc"))
        with open(os.path.join(job_dir, "examples", "pc", "main.yaml"), "w") as f:
            f.write("name: pc\n")
        _, popen, _ = self._execute(spec_file="examples/pc/../pc/main.yaml")
        self.assertEqual(popen.call_args.args[0][-1], os.path.join("examples", "pc", "main.yaml"))

    def test_upload_uses_the_job_key_and_fails_without_one(self):
        from seine.distributed.agent.executor import upload_artifacts
        job_dir = os.path.join(self.tmp_dir, "jobs", "bld-5")
        os.makedirs(os.path.join(job_dir, "build", "deploy"))
        with open(os.path.join(job_dir, "build", "deploy", "a.img"), "w") as f:
            f.write("x")
        with mock.patch("seine.distributed.agent.executor.provider_from") as provider:
            provider.return_value.push_artifact_info.return_value = {"key": "k"}
            self.assertEqual(upload_artifacts(job_dir, "p", "bld-5", s3=JOB_S3), [{"key": "k"}])
        provider.assert_called_once_with(JOB_S3)
        with self.assertRaises(ValueError):
            upload_artifacts(job_dir, "p", "bld-5")

    def test_distributed_code_never_builds_a_provider_from_ambient_credentials(self):
        root = os.path.join(os.path.dirname(__file__), "..", "..", "seine", "distributed")
        for folder, _, files in os.walk(root):
            for name in files:
                if name.endswith(".py"):
                    with open(os.path.join(folder, name), encoding="utf-8") as f:
                        self.assertNotIn("for_build(", f.read(), name)


class ChildEnvTest(Test):
    """Tests for the allowlisted environment builds run with."""

    AGENT_ENV = {
        "PATH": "/usr/bin", "HOME": "/home/agent", "LANG": "C.UTF-8", "LC_ALL": "C",
        "TZ": "UTC", "TERM": "xterm", "TMPDIR": "/var/tmp", "XDG_RUNTIME_DIR": "/run/user/9",
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/9/bus",
        "CONTAINERS_CONF": "/etc/c.conf", "http_proxy": "http://p", "NO_PROXY": "lan",
        "SEINE_CA_CERT": "/etc/ca.pem",
        "SEINE_ENROLLMENT_TOKEN": "enroll-secret", "SEINE_WORKER_TOKEN": "worker-secret",
        "SEINE_SIGN_KEY": "/keys/sign", "SEINE_VAULT_PASSWORD": "vault-secret",
        "AWS_ACCESS_KEY_ID": "AKIA", "AWS_SECRET_ACCESS_KEY": "aws-secret",
        "AWS_SESSION_TOKEN": "aws-session", "AWS_REGION": "garage",
        "SEINE_S3_ACCESS_KEY": "s3-ak", "SEINE_S3_SECRET_KEY": "s3-sk",
        "SEINE_S3_ENDPOINT": "https://s3", "SEINE_CREDENTIALS_FILE": "/home/agent/c.json",
        "SSH_AUTH_SOCK": "/tmp/agent.sock", "GITHUB_TOKEN": "gh-secret",
    }
    S3_VARS = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_REGION",
               "SEINE_S3_ACCESS_KEY", "SEINE_S3_SECRET_KEY", "SEINE_S3_ENDPOINT")

    def _env(self, s3=JOB_S3, **options):
        manifest = JobManifest(job_id="j", build_id="bld-9", project="p", options=options, s3=s3)
        return child_env(manifest, "/job/build", environ=self.AGENT_ENV)

    def test_allowlisted_variables_pass(self):
        env = self._env()
        for name in ("PATH", "HOME", "LANG", "LC_ALL", "TZ", "TERM", "TMPDIR", "XDG_RUNTIME_DIR",
                     "DBUS_SESSION_BUS_ADDRESS", "CONTAINERS_CONF", "http_proxy", "NO_PROXY",
                     "SEINE_CA_CERT"):
            self.assertEqual(env[name], self.AGENT_ENV[name], name)

    def test_tokens_and_unlisted_variables_never_pass(self):
        for options in ({}, {"s3_cache": True}):
            env = self._env(**options)
            for name in ("SEINE_ENROLLMENT_TOKEN", "SEINE_WORKER_TOKEN", "SEINE_SIGN_KEY",
                         "SEINE_VAULT_PASSWORD", "SSH_AUTH_SOCK", "GITHUB_TOKEN"):
                self.assertNotIn(name, env, name)
            self.assertNotIn("enroll-secret", env.values())

    def test_inherited_s3_variables_never_pass(self):
        for options in ({}, {"s3_cache": True}):
            env = self._env(s3=None, **options)
            for name in self.S3_VARS:
                self.assertNotIn(name, env, name)
            self.assertNotIn("SEINE_CREDENTIALS_FILE", env)

    def test_job_keys_only_with_s3_cache(self):
        env = self._env()
        self.assertNotIn("AWS_ACCESS_KEY_ID", env)
        env = self._env(s3_cache=True)
        self.assertEqual(env["AWS_ACCESS_KEY_ID"], JOB_S3.access_key)
        self.assertEqual(env["AWS_SECRET_ACCESS_KEY"], JOB_S3.secret_key)
        for name in ("AWS_SESSION_TOKEN", "AWS_REGION", "SEINE_S3_ACCESS_KEY", "SEINE_S3_ENDPOINT"):
            self.assertNotIn(name, env, name)
        self.assertNotIn("aws-secret", env.values())
        self.assertNotEqual(env["SEINE_CREDENTIALS_FILE"], "/home/agent/c.json")

    def test_executor_variables_are_set(self):
        env = self._env()
        self.assertEqual(env["SEINE_BUILD_DIR"], "/job/build")
        self.assertEqual(env["SEINE_BUILD_ID"], "bld-9")
        self.assertEqual(env["PYTHONUNBUFFERED"], "1")


class CancelExecutorTest(Test):
    """Tests for stopping a real build process group."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="seine-test-cancel-")
        self.ex = SubprocessExecutor(self.tmp_dir)
        self.ex.kill_grace = 0.5

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _script(self, body):
        path = os.path.join(self.tmp_dir, "fake-seine")
        with open(path, "w") as f:
            f.write(f"#!/bin/sh\n{body}\nsleep 300 &\necho $! > \"$SEINE_BUILD_DIR/sleep.pid\"\nwait\n")
        os.chmod(path, 0o755)
        return path

    def _cancel_running(self, script):
        manifest = JobManifest(job_id="j", build_id="bld-c", project="p", worktree_digest="d", s3=JOB_S3)
        pidfile = os.path.join(self.tmp_dir, "jobs", "bld-c", "build", "sleep.pid")
        result = []

        def pull(_manifest, job_dir):
            with open(os.path.join(job_dir, "spec.yaml"), "w") as f:
                f.write("name: x\n")

        with mock.patch.object(self.ex, "_pull_worktree", side_effect=pull), \
                mock.patch.object(self.ex, "cleanup_containers") as cleanup, \
                mock.patch("seine.distributed.agent.executor.find_seine_binary", return_value=script):
            thread = threading.Thread(
                target=lambda: result.append(self.ex.execute_job(manifest, on_log=lambda s, t: None)))
            thread.start()
            deadline = time.time() + 10
            while not os.path.exists(pidfile) and time.time() < deadline:
                time.sleep(0.02)
            time.sleep(0.1)
            sleep_pid = int(open(pidfile).read())
            self.ex.cancel()
            thread.join(15)
        self.assertFalse(thread.is_alive())
        cleanup.assert_called_once()
        return result[0], sleep_pid

    def _is_gone(self, pid):
        for _ in range(100):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            time.sleep(0.02)
        return False

    def test_cancel_terminates_the_process_group(self):
        code, sleep_pid = self._cancel_running(self._script(""))
        self.assertNotEqual(code, 0)
        self.assertTrue(self.ex.cancelled)
        self.assertTrue(self._is_gone(sleep_pid))

    def test_cancel_kills_a_build_that_ignores_sigterm(self):
        started = time.time()
        _, sleep_pid = self._cancel_running(self._script("trap '' TERM"))
        self.assertLess(time.time() - started, 10)
        self.assertTrue(self._is_gone(sleep_pid))


class ContainerCleanupTest(Test):
    """Tests for the podman commands that remove a build's containers."""

    def setUp(self):
        self.ex = SubprocessExecutor("/work")
        self.manifest = JobManifest(job_id="j", build_id="bld-l", project="p")
        self.env = {"PATH": "/usr/bin", "SEINE_BUILD_DIR": "/work/jobs/bld-l/build"}
        self.logs = []

    def _remove(self, run):
        with mock.patch("shutil.which", return_value="/usr/bin/podman"), \
                mock.patch("subprocess.run", side_effect=run) as sub:
            self.ex.cleanup_containers(self.manifest, self.env, lambda s, t: self.logs.append(t))
        return [c.args[0] for c in sub.call_args_list]

    def _done(self, out="", code=0, err=""):
        return subprocess.CompletedProcess([], code, out, err)

    def test_lists_by_label_then_removes_the_ids(self):
        def run(cmd, **kwargs):
            return self._done("c1\nc2\n" if "ps" in cmd else "")
        calls = self._remove(run)
        storage = ["--root", "/work/jobs/bld-l/build/containers",
                   "--runroot", "/work/jobs/bld-l/build/containers/run"]
        self.assertEqual(calls[0], ["podman"] + storage + ["ps", "-aq", "--filter", "label=seine.build_id=bld-l"])
        self.assertEqual(calls[1], ["podman"] + storage + ["rm", "-f", "c1", "c2"])

    def test_nothing_to_remove_runs_no_rm(self):
        calls = self._remove(lambda cmd, **kw: self._done(""))
        self.assertEqual(len(calls), 1)

    def test_commands_have_timeouts(self):
        timeouts = []

        def run(cmd, **kwargs):
            timeouts.append(kwargs.get("timeout"))
            return self._done("c1\n")
        self._remove(run)
        self.assertTrue(all(timeouts))

    def test_failures_are_logged(self):
        self._remove(lambda cmd, **kw: self._done(code=125, err="storage busy"))
        self.assertIn("storage busy", "".join(self.logs))
        self.logs.clear()

        def timeout(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, 1)
        self._remove(timeout)
        self.assertIn("ps failed", "".join(self.logs))

    def test_no_podman_runs_nothing(self):
        with mock.patch("shutil.which", return_value=None), \
                mock.patch("subprocess.run") as sub:
            self.ex.cleanup_containers(self.manifest, self.env, lambda s, t: None)
        sub.assert_not_called()


class WipeJobDirTest(Test):
    """Tests for removing a finished job's directory safely."""

    def setUp(self):
        self.tmp_dir = os.path.realpath(tempfile.mkdtemp(prefix="seine-test-wipe-"))
        self.ex = SubprocessExecutor(self.tmp_dir)
        self.ex.wipe_backoff = (0.0, 0.0)
        self.jobs = os.path.join(self.tmp_dir, "jobs")
        os.makedirs(os.path.join(self.jobs, "bld-1", "build", "sub"))
        os.makedirs(os.path.join(self.jobs, "bld-2"))
        self.logs = []

    def tearDown(self):
        for root, dirs, _ in os.walk(self.tmp_dir):
            for d in dirs:
                os.chmod(os.path.join(root, d), 0o700)
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _wipe(self, build_id):
        self.ex.wipe_job_dir(build_id, on_log=lambda s, t: self.logs.append(t))

    def test_wipes_only_the_job_dir(self):
        self._wipe("bld-1")
        self.assertFalse(os.path.exists(os.path.join(self.jobs, "bld-1")))
        self.assertTrue(os.path.isdir(os.path.join(self.jobs, "bld-2")))

    def test_wipes_read_only_content(self):
        inner = os.path.join(self.jobs, "bld-1", "build", "sub")
        with open(os.path.join(inner, "f"), "w") as f:
            f.write("x")
        os.chmod(inner, 0o500)
        os.chmod(os.path.dirname(inner), 0o500)
        self._wipe("bld-1")
        self.assertFalse(os.path.exists(os.path.join(self.jobs, "bld-1")))

    def test_unreadable_tree_is_removed_or_logged(self):
        locked = os.path.join(self.jobs, "bld-1", "build", "sub")
        with open(os.path.join(locked, "f"), "w") as f:
            f.write("x")
        os.chmod(locked, 0)
        self._wipe("bld-1")
        self.assertTrue(not os.path.exists(os.path.join(self.jobs, "bld-1")) or self.logs)

    def test_podman_runs_when_rmtree_leaves_the_dir(self):
        with mock.patch("shutil.rmtree"), \
                mock.patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")) as sub:
            self._wipe("bld-1")
        self.assertEqual(sub.call_count, 3)
        self.assertIn("Could not remove", "".join(self.logs))

    def test_wipe_never_raises(self):
        for exc in (ValueError("v"), TypeError("t"), RuntimeError("r")):
            self.logs.clear()
            with mock.patch("shutil.rmtree", side_effect=exc):
                self._wipe("bld-1")
            with mock.patch("shutil.rmtree"), mock.patch("subprocess.run", side_effect=exc):
                self._wipe("bld-1")
            self.assertTrue(self.logs)

    def test_missing_dir_is_fine(self):
        self._wipe("bld-gone")
        self.assertEqual(self.logs, [])

    def test_ids_leaving_the_jobs_dir_are_refused(self):
        for build_id in ("", "..", "../jobs", "../outside", "bld-1/build"):
            self._wipe(build_id)
            self.assertIn("Refusing", "".join(self.logs), build_id)
            self.logs.clear()
        self.assertTrue(os.path.isdir(os.path.join(self.jobs, "bld-1", "build", "sub")))
        self.assertTrue(os.path.isdir(self.jobs))

    def test_symlinked_job_dir_is_refused_and_target_kept(self):
        target = os.path.join(self.tmp_dir, "precious")
        os.makedirs(target)
        os.symlink(target, os.path.join(self.jobs, "bld-link"))
        self._wipe("bld-link")
        self.assertIn("Refusing", "".join(self.logs))
        self.assertTrue(os.path.isdir(target))

    def test_falls_back_to_podman_unshare(self):
        with mock.patch("shutil.rmtree"), \
                mock.patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")) as sub:
            self._wipe("bld-1")
        cmd = sub.call_args.args[0]
        self.assertEqual(cmd[:4], ["podman", "unshare", "bash", "-c"])
        self.assertEqual(cmd[-1], os.path.join(self.jobs, "bld-1"))
        self.assertIn("attempt 2", "".join(self.logs))

    def test_busy_mounts_are_unmounted_with_the_path_as_a_positional_argument(self):
        path = os.path.join(self.jobs, "bld-1")
        real_rmtree = shutil.rmtree
        runs = []

        def run(cmd, **kwargs):
            runs.append(cmd)
            if len(runs) == 1:
                return subprocess.CompletedProcess(cmd, 1, "", "rm: Device or resource busy")
            real_rmtree(path)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with mock.patch("subprocess.run", side_effect=run), mock.patch("shutil.rmtree"):
            self._wipe("bld-1")
        self.assertEqual(len(runs), 2)
        for cmd in runs:
            self.assertEqual(cmd[-2:], ["_", path])
            self.assertNotIn(path, cmd[4])
            self.assertIn("umount -l", cmd[4])
            self.assertIn("sort -r", cmd[4])
        self.assertIn("Device or resource busy", "".join(self.logs))
        self.assertFalse(os.path.exists(path))

    def test_wipe_gives_up_after_three_attempts(self):
        with mock.patch("shutil.rmtree"), \
                mock.patch("subprocess.run", return_value=subprocess.CompletedProcess([], 1, "", "busy")) as sub:
            self._wipe("bld-1")
        self.assertEqual(sub.call_count, 3)
        self.assertEqual(self.logs[-1], "[agent] Could not remove %s\n" % os.path.join(self.jobs, "bld-1"))

    def test_wipe_pauses_between_attempts(self):
        self.ex.wipe_backoff = (2.0, 5.0)
        with mock.patch("shutil.rmtree"), mock.patch("time.sleep") as sleep, \
                mock.patch("subprocess.run", return_value=subprocess.CompletedProcess([], 1, "", "")):
            self._wipe("bld-1")
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [2.0, 5.0])


class WipeJobDirPodmanTest(Test):
    """Wipe a job directory through a real `podman unshare`.

    :avocado: tags=container
    """

    def setUp(self):
        if not shutil.which("podman"):
            self.cancel("podman is not installed")
        if subprocess.run(["podman", "unshare", "true"], capture_output=True).returncode != 0:
            self.cancel("user namespaces are not available")
        self.tmp_dir = os.path.realpath(tempfile.mkdtemp(prefix="seine-test-wipe-"))
        self.ex = SubprocessExecutor(self.tmp_dir)
        self.ex.wipe_backoff = (0.0, 0.0)

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_podman_unshare_removes_a_job_dir(self):
        path = os.path.join(self.tmp_dir, "jobs", "bld-1", "build")
        os.makedirs(path)
        with open(os.path.join(path, "f"), "w") as f:
            f.write("x")
        with mock.patch("shutil.rmtree"):
            self.ex.wipe_job_dir("bld-1")
        self.assertFalse(os.path.exists(os.path.join(self.tmp_dir, "jobs", "bld-1")))


class WipeStaleJobsTest(Test):
    """Tests for cleaning what a crashed agent left behind."""

    def setUp(self):
        self.tmp_dir = os.path.realpath(tempfile.mkdtemp(prefix="seine-test-stale-"))
        self.ex = SubprocessExecutor(self.tmp_dir)
        self.ex.wipe_backoff = (0.0, 0.0)
        self.jobs = os.path.join(self.tmp_dir, "jobs")
        os.makedirs(os.path.join(self.jobs, "bld-aaaa1111", "build", "containers"))
        os.makedirs(os.path.join(self.jobs, "bld-bbbb2222"))
        os.makedirs(os.path.join(self.jobs, "keep-me"))
        with open(os.path.join(self.jobs, "bld-file"), "w") as f:
            f.write("x")
        self.logs = []

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _wipe(self):
        self.ex.wipe_stale_jobs(on_log=lambda s, t: self.logs.append(t))

    def test_job_dirs_are_removed_and_other_entries_kept(self):
        with mock.patch("shutil.which", return_value=None):
            self._wipe()
        self.assertEqual(sorted(os.listdir(self.jobs)), ["bld-file", "keep-me"])
        self.assertIn("bld-aaaa1111", "".join(self.logs))

    def test_symlinked_job_dir_is_left_alone(self):
        target = os.path.join(self.tmp_dir, "precious")
        os.makedirs(target)
        os.symlink(target, os.path.join(self.jobs, "bld-link"))
        self._wipe()
        self.assertTrue(os.path.isdir(target))
        self.assertTrue(os.path.islink(os.path.join(self.jobs, "bld-link")))

    def test_containers_are_removed_from_the_job_storage_before_the_wipe(self):
        root = os.path.join(self.jobs, "bld-aaaa1111", "build", "containers")
        seen = []

        def run(cmd, **kwargs):
            seen.append((cmd, os.path.isdir(root), kwargs.get("timeout")))
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with mock.patch("shutil.which", return_value="/usr/bin/podman"), \
                mock.patch("subprocess.run", side_effect=run):
            self._wipe()
        cmd, existed, timeout = seen[0]
        self.assertEqual(cmd, ["podman", "--root", root, "--runroot", os.path.join(root, "run"), "rm", "-af"])
        self.assertTrue(existed)
        self.assertTrue(timeout)
        self.assertEqual(len([c for c, _, _ in seen if "rm" in c and "-af" in c]), 1)

    def test_container_cleanup_failure_is_logged_and_the_wipe_continues(self):
        with mock.patch("shutil.which", return_value="/usr/bin/podman"), \
                mock.patch("subprocess.run", side_effect=OSError("boom")):
            self._wipe()
        self.assertIn("podman rm failed: boom", "".join(self.logs))
        self.assertFalse(os.path.exists(os.path.join(self.jobs, "bld-aaaa1111")))

    def test_never_raises(self):
        for target in ("os.listdir", "shutil.rmtree"):
            with mock.patch(target, side_effect=RuntimeError("r")):
                self._wipe()
        with mock.patch.object(self.ex, "_remove_stale_containers", side_effect=ValueError("v")), \
                mock.patch("shutil.rmtree"):
            self._wipe()

    def test_failure_reason_reaches_the_reported_status(self):
        from seine.distributed.agent.daemon import WorkerAgent
        with mock.patch("seine.distributed.agent.daemon.detect_capabilities"):
            agent = WorkerAgent("http://localhost:8000", "tok", self.tmp_dir, "w1")
        agent.executor.failure_reason = "worktree d not found in bucket b"
        manifest = JobManifest(job_id="j", build_id="bld-9", project="p")
        with mock.patch.object(agent.executor, "execute_job", return_value=1):
            status, err, _ = agent._execute(manifest, lambda s, t: None)
        self.assertEqual((status, err), ("failed", "worktree d not found in bucket b"))

    def test_missing_jobs_dir_is_fine(self):
        shutil.rmtree(self.jobs)
        self._wipe()
        self.assertEqual(self.logs, [])

    def test_agent_start_wipes_stale_jobs(self):
        from seine.distributed.agent.daemon import WorkerAgent
        with mock.patch("seine.distributed.agent.daemon.detect_capabilities"):
            agent = WorkerAgent("http://localhost:8000", "tok", self.tmp_dir, "w1")
        with mock.patch.object(agent, "register", side_effect=RuntimeError("stop")), \
                mock.patch.object(agent.executor, "wipe_stale_jobs") as wipe, \
                mock.patch("signal.signal"):
            with self.assertRaises(RuntimeError):
                agent.start()
        wipe.assert_called_once()


class WorkerAgentDaemonTest(Test):
    """Tests for WorkerAgent enrollment, heartbeat, and claim loops via mocking."""

    def _make_agent(self):
        from seine.distributed.agent.daemon import WorkerAgent
        return WorkerAgent(
            server_url="http://localhost:8000",
            enrollment_token="test-token",
            work_dir=self.tmp_dir,
            worker_id="worker-test-amd64",
            report_retry_total=0,
        )

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="seine-test-daemon-")

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_agent_instantiation(self):
        agent = self._make_agent()
        self.assertEqual(agent.server_url, "http://localhost:8000")
        self.assertEqual(agent.enrollment_token, "test-token")
        self.assertEqual(agent.worker_id, "worker-test-amd64")

    def test_register_sets_worker_token(self):
        agent = self._make_agent()
        fake_resp = mock.MagicMock()
        fake_resp.status_code = 200
        fake_resp.json.return_value = {"worker_id": "worker-test-amd64", "worker_token": "wt-abc123"}
        fake_resp.raise_for_status = mock.MagicMock()

        with mock.patch("requests.post", return_value=fake_resp):
            agent.register()

        self.assertEqual(agent.worker_token, "wt-abc123")

    def test_register_posts_to_correct_url(self):
        agent = self._make_agent()
        fake_resp = mock.MagicMock()
        fake_resp.json.return_value = {"worker_id": "worker-test-amd64", "worker_token": "tok"}
        fake_resp.raise_for_status = mock.MagicMock()

        with mock.patch("requests.post", return_value=fake_resp) as mock_post:
            agent.register()

        url = mock_post.call_args[0][0]
        self.assertIn("/api/v1/workers/register", url)

    def test_claim_job_returns_none_on_204(self):
        agent = self._make_agent()
        agent.worker_token = "wt-test"
        fake_resp = mock.MagicMock()
        fake_resp.status_code = 204

        with mock.patch("requests.post", return_value=fake_resp):
            result = agent.claim_job()

        self.assertIsNone(result)

    def test_claim_job_returns_manifest_on_200(self):
        agent = self._make_agent()
        agent.worker_token = "wt-test"
        fake_resp = mock.MagicMock()
        fake_resp.status_code = 200
        fake_resp.raise_for_status = mock.MagicMock()
        fake_resp.json.return_value = {
            "job_id": "job-xyz",
            "build_id": "bld-xyz",
            "project": "my-proj",
            "target_arch": "arm64",
        }

        with mock.patch("requests.post", return_value=fake_resp):
            result = agent.claim_job()

        self.assertIsNotNone(result)
        self.assertEqual(result.job_id, "job-xyz")
        self.assertEqual(result.target_arch, "arm64")

    def test_update_job_status_swallows_network_errors(self):
        agent = self._make_agent()
        agent.worker_token = "wt-test"

        with mock.patch("requests.post", side_effect=ConnectionError("refused")):
            # Should not raise.
            agent.update_job_status("job-1", "bld-1", "completed")

    def test_heartbeat_posts_worker_id(self):
        agent = self._make_agent()
        agent.worker_token = "wt-test"
        fake_resp = mock.MagicMock()
        fake_resp.raise_for_status = mock.MagicMock()

        with mock.patch("requests.post", return_value=fake_resp) as mock_post:
            agent._send_heartbeat()

        body = mock_post.call_args[1]["json"]
        self.assertEqual(body["worker_id"], "worker-test-amd64")
        self.assertIn("free_disk_gb", body)

    def test_stop_sets_running_false(self):
        agent = self._make_agent()
        agent._running = True
        agent.stop()
        self.assertFalse(agent._running)


class AgentCLITest(Test):
    """Tests for the seine-agent CLI entry point."""

    def test_cli_importable(self):
        from seine.distributed.agent import cli
        self.assertTrue(hasattr(cli, "main"))

    def test_cli_help_exits_zero(self):
        from seine.distributed.agent.cli import main
        with self.assertRaises(SystemExit) as ctx:
            with mock.patch("sys.argv", ["seine-agent", "--help"]):
                main()
        self.assertEqual(ctx.exception.code, 0)

    def test_cli_no_subcommand_prints_help_and_exits(self):
        from seine.distributed.agent.cli import main
        with self.assertRaises(SystemExit):
            with mock.patch("sys.argv", ["seine-agent"]):
                main()

    def test_cli_run_requires_server(self):
        from seine.distributed.agent.cli import main
        with self.assertRaises(SystemExit) as ctx:
            with mock.patch("sys.argv", ["seine-agent", "run"]):
                main()
        self.assertNotEqual(ctx.exception.code, 0)

    def test_cli_run_requires_enrollment_token(self):
        from seine.distributed.agent.cli import main
        env = {k: v for k, v in os.environ.items() if k != "SEINE_ENROLLMENT_TOKEN"}
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch("seine.distributed.agent.cli.WorkerAgent") as mock_cls, \
             mock.patch("sys.argv", ["seine-agent", "run", "--server", "http://localhost:8000"]):
            with self.assertRaises(SystemExit) as ctx:
                main()
        self.assertNotEqual(ctx.exception.code, 0)
        mock_cls.assert_not_called()

    def test_cli_run_launches_agent(self):
        from seine.distributed.agent.cli import main
        with mock.patch("seine.distributed.agent.cli.WorkerAgent") as mock_cls, \
             mock.patch("os.geteuid", return_value=1000), \
             mock.patch("signal.signal"):
            mock_agent = mock.MagicMock()
            mock_cls.return_value = mock_agent
            with mock.patch("sys.argv", [
                "seine-agent", "run",
                "--server", "http://localhost:8000",
                "--enrollment-token", "test-tok",
            ]):
                main()
        mock_cls.assert_called_once()
        mock_agent.start.assert_called_once()

    def _run_cli(self, *extra, euid=1000, env=None):
        from seine.distributed.agent.cli import main
        argv = ["seine-agent", "run", "--enrollment-token", "tok", *extra]
        if "--server" not in extra:
            argv += ["--server", "http://localhost:8000"]
        with mock.patch("seine.distributed.agent.cli.WorkerAgent") as agent_cls, \
                mock.patch("os.geteuid", return_value=euid), \
                mock.patch.dict(os.environ, env or {}), \
                mock.patch("sys.argv", argv):
            try:
                main()
                code = 0
            except SystemExit as e:
                code = e.code
        return code, agent_cls

    def test_cli_line_buffers_stdout_and_stderr(self):
        with mock.patch("sys.stdout") as out, mock.patch("sys.stderr") as err:
            self._run_cli()
        out.reconfigure.assert_called_once_with(line_buffering=True)
        err.reconfigure.assert_called_once_with(line_buffering=True)

    def test_cli_refuses_root(self):
        code, agent_cls = self._run_cli(euid=0)
        self.assertNotEqual(code, 0)
        agent_cls.assert_not_called()

    def test_cli_allow_root_runs_as_root(self):
        code, agent_cls = self._run_cli("--allow-root", euid=0)
        self.assertEqual(code, 0)
        agent_cls.return_value.start.assert_called_once()

    def test_cli_refuses_plain_http_to_a_remote_server(self):
        from seine.distributed.agent.cli import main
        with mock.patch("os.geteuid", return_value=1000), \
                mock.patch("sys.argv", ["seine-agent", "run", "--enrollment-token", "t",
                                        "--server", "http://build.example:8000"]), \
                mock.patch("seine.distributed.agent.daemon.detect_capabilities"):
            with self.assertRaises(SystemExit) as ctx:
                main()
        self.assertNotEqual(ctx.exception.code, 0)

    def test_cli_insecure_and_ca_cert_reach_the_agent(self):
        code, agent_cls = self._run_cli(
            "--server", "http://build.example:8000", "--insecure", "--ca-cert", "/etc/ca.pem")
        self.assertEqual(code, 0)
        kwargs = agent_cls.call_args.kwargs
        self.assertTrue(kwargs["insecure"])
        self.assertEqual(kwargs["ca_cert"], "/etc/ca.pem")

    def test_cli_ca_cert_defaults_to_the_environment(self):
        _, agent_cls = self._run_cli(env={"SEINE_CA_CERT": "/etc/env-ca.pem"})
        self.assertEqual(agent_cls.call_args.kwargs["ca_cert"], "/etc/env-ca.pem")


class TransportTest(Test):
    """Tests for the server URL and TLS helpers."""

    def test_loopback_hosts(self):
        for host in ("localhost", "127.0.0.1", "127.1.2.3", "::1"):
            self.assertTrue(is_loopback(host), host)
        for host in ("192.168.1.111", "build.example", "10.0.0.1"):
            self.assertFalse(is_loopback(host), host)

    def test_plain_http_only_to_loopback_unless_insecure(self):
        check_server_url("http://localhost:8000")
        check_server_url("http://[::1]:8000")
        check_server_url("https://build.example")
        check_server_url("http://192.168.1.111:8000", insecure=True)
        with self.assertRaises(ValueError):
            check_server_url("http://192.168.1.111:8000")

    def test_other_schemes_are_refused(self):
        for url in ("ftp://localhost", "localhost:8000", "ws://localhost"):
            with self.assertRaises(ValueError, msg=url):
                check_server_url(url, insecure=True)

    def test_agent_refuses_remote_http_at_construction(self):
        from seine.distributed.agent.daemon import WorkerAgent
        with mock.patch("seine.distributed.agent.daemon.detect_capabilities"):
            with self.assertRaises(ValueError):
                WorkerAgent("http://192.168.1.111:8000", "tok", "/tmp/w", "w1")
            WorkerAgent("http://192.168.1.111:8000", "tok", "/tmp/w", "w1", insecure=True)

    def test_requests_use_the_ca_cert(self):
        from seine.distributed.agent.daemon import WorkerAgent
        agent = WorkerAgent("https://srv:8443", "tok", "/tmp/w", "w1", ca_cert="/etc/ca.pem")
        agent.worker_token = "wt"
        resp = mock.MagicMock(status_code=204)
        resp.json.return_value = {"worker_id": "w1", "worker_token": "wt"}
        with mock.patch("requests.post", return_value=resp) as post:
            agent.register()
            agent.claim_job()
            agent.update_job_status("j", "b", "failed")
            agent._send_heartbeat()
        self.assertEqual(post.call_count, 4)
        for call in post.call_args_list:
            self.assertEqual(call.kwargs["verify"], "/etc/ca.pem")

    def test_requests_verify_by_default(self):
        self.assertIs(requests_verify(None), True)
        self.assertEqual(requests_verify("/x.pem"), "/x.pem")

    def test_ws_context_only_for_wss(self):
        self.assertIsNone(ws_ssl_context("ws://srv/x", "/etc/ca.pem"))
        with mock.patch("ssl.create_default_context") as create:
            ctx = ws_ssl_context("wss://srv/x", "/etc/ca.pem")
        create.assert_called_once_with(cafile="/etc/ca.pem")
        self.assertIs(ctx, create.return_value)


ART = {"name": "disk.img", "key": "artifacts/p/bld-1/disk.img", "sha256": "0" * 64, "size": 1}


class JobLifecycleTest(Test):
    """Tests for run_job: outcomes, the wipe after them, heartbeats and cancellation."""

    def setUp(self):
        self.tmp_dir = os.path.realpath(tempfile.mkdtemp(prefix="seine-test-life-"))
        self.agent = self._agent()
        self.manifest = JobManifest(job_id="job-1", build_id="bld-1", project="p")
        self.job_dir = os.path.join(self.tmp_dir, "jobs", "bld-1")
        os.makedirs(self.job_dir)
        self.statuses = []

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _agent(self, **kwargs):
        from seine.distributed.agent.daemon import WorkerAgent
        with mock.patch("seine.distributed.agent.daemon.detect_capabilities") as detect:
            detect.return_value = mock.MagicMock(native_arch="amd64", free_disk_gb=10.0)
            agent = WorkerAgent("http://localhost:8000", "enroll", self.tmp_dir, "w1", **kwargs)
        agent.worker_token = "wt"
        return agent

    def _run(self, execute_job, upload=None):
        def status(job_id, build_id, status, err=None, artifacts=None):
            self.statuses.append((status, err, artifacts))

        with mock.patch("seine.distributed.agent.daemon.LogStreamer"), \
                mock.patch.object(self.agent.executor, "execute_job", side_effect=execute_job), \
                mock.patch.object(self.agent.executor, "upload_artifacts",
                                  side_effect=upload or (lambda *a, **k: [ART])) as uploaded, \
                mock.patch.object(self.agent, "update_job_status", side_effect=status):
            self.agent.run_job(self.manifest)
        return uploaded

    def test_job_keys_are_registered_and_scrubbed_from_streamed_logs(self):
        from seine import vault
        self.manifest = JobManifest(job_id="job-1", build_id="bld-1", project="p", s3=JOB_S3)
        sent = []

        def execute(manifest, on_log):
            on_log("stderr", f"denied for {JOB_S3.access_key} / {JOB_S3.secret_key}\n")
            return 1

        with mock.patch("seine.distributed.agent.daemon.LogStreamer") as streamer:
            streamer.return_value.__enter__.return_value.send = lambda s, t: sent.append(t)
            with mock.patch.object(self.agent.executor, "execute_job", side_effect=execute), \
                    mock.patch.object(self.agent, "update_job_status"):
                self.agent.run_job(self.manifest)
        self.assertIn(JOB_S3.secret_key, vault.secrets())
        self.assertEqual(sent[0], "denied for <redacted> / <redacted>\n")

    def test_success_uploads_then_wipes(self):
        order = []
        self._run(lambda *a, **k: 0, upload=lambda *a, **k: order.append("upload") or [ART])
        self.assertEqual(order, ["upload"])
        self.assertEqual(self.statuses, [("completed", None, [ART])])
        self.assertFalse(os.path.exists(self.job_dir))

    def test_wipe_happens_after_the_upload(self):
        seen = []
        self._run(lambda *a, **k: 0,
                  upload=lambda *a, **k: seen.append(os.path.isdir(self.job_dir)) or [])
        self.assertEqual(seen, [True])
        self.assertFalse(os.path.exists(self.job_dir))

    def test_failure_wipes(self):
        uploaded = self._run(lambda *a, **k: 2)
        uploaded.assert_not_called()
        self.assertEqual(self.statuses, [("failed", "build exited with code 2", [])])
        self.assertFalse(os.path.exists(self.job_dir))

    def test_exception_fails_the_job_and_wipes(self):
        def boom(*a, **k):
            raise RuntimeError("kaboom")
        self._run(boom)
        self.assertEqual(self.statuses[0][:2], ("failed", "kaboom"))
        self.assertFalse(os.path.exists(self.job_dir))

    def test_upload_failure_fails_the_job_and_wipes(self):
        def boom(*a, **k):
            raise RuntimeError("s3 down")
        self._run(lambda *a, **k: 0, upload=boom)
        self.assertEqual(self.statuses[0][:2], ("failed", "artifact upload failed: s3 down"))
        self.assertFalse(os.path.exists(self.job_dir))

    def test_a_failing_wipe_does_not_change_the_outcome(self):
        with mock.patch.object(self.agent.executor, "wipe_job_dir", side_effect=TypeError("boom")):
            self._run(lambda *a, **k: 0)
        self.assertEqual(self.statuses, [("completed", None, [ART])])

    def test_cancel_reports_cancelled_and_wipes(self):
        def run(*a, **k):
            self.agent.executor.cancel()
            return -15
        uploaded = self._run(run)
        uploaded.assert_not_called()
        self.assertEqual(self.statuses, [("cancelled", None, [])])
        self.assertFalse(os.path.exists(self.job_dir))

    def test_wipe_leaves_other_jobs_alone(self):
        other = os.path.join(self.tmp_dir, "jobs", "bld-other")
        os.makedirs(other)
        self._run(lambda *a, **k: 1)
        self.assertTrue(os.path.isdir(other))

    def test_shutdown_cancels_the_job_and_reports_failed(self):
        def run(*a, **k):
            timer = threading.Timer(0.05, self.agent.shutdown)
            timer.start()
            deadline = time.time() + 10
            while not self.agent.executor.cancelled and time.time() < deadline:
                time.sleep(0.01)
            return -15
        self._run(run)
        self.assertEqual(self.statuses, [("failed", "agent shutting down", [])])
        self.assertFalse(self.agent._running)
        self.assertFalse(os.path.exists(self.job_dir))

    def test_claimed_job_after_shutdown_is_not_started(self):
        self.agent.shutdown()
        with mock.patch.object(self.agent.executor, "execute_job") as execute:
            self._run(execute)
        execute.assert_not_called()
        self.assertEqual(self.statuses[0][:2], ("failed", "agent shutting down"))

    def test_heartbeat_reports_the_running_job_and_returns_cancels(self):
        self.agent._current_job_id = "job-1"
        resp = mock.MagicMock()
        resp.json.return_value = {"status": "ok", "cancel": ["job-1", "job-9"]}
        with mock.patch("requests.post", return_value=resp) as post, \
                mock.patch("seine.distributed.agent.daemon.detect_capabilities") as detect:
            detect.return_value = mock.MagicMock(free_disk_gb=1.0)
            cancel = self.agent._send_heartbeat()
        self.assertEqual(cancel, ["job-1", "job-9"])
        self.assertEqual(post.call_args.kwargs["json"]["running_jobs"], ["job-1"])

    def test_heartbeat_failure_is_not_fatal(self):
        with mock.patch("requests.post", side_effect=ConnectionError("down")), \
                mock.patch("seine.distributed.agent.daemon.detect_capabilities"):
            self.assertEqual(self.agent._send_heartbeat(), [])

    def test_heartbeats_flow_while_a_job_runs_and_cancel_stops_it(self):
        self.agent = self._agent(heartbeat_interval=0.02)
        beats = []

        def post(url, **kwargs):
            beats.append(kwargs["json"]["running_jobs"])
            resp = mock.MagicMock()
            resp.json.return_value = {"status": "ok", "cancel": ["job-1"] if len(beats) > 3 else []}
            return resp

        def run(*a, **k):
            deadline = time.time() + 10
            while not self.agent.executor.cancelled and time.time() < deadline:
                time.sleep(0.01)
            return -15

        thread = threading.Thread(target=self.agent._heartbeat_loop, daemon=True)
        with mock.patch("requests.post", side_effect=post), \
                mock.patch("seine.distributed.agent.daemon.detect_capabilities") as detect:
            detect.return_value = mock.MagicMock(free_disk_gb=1.0)
            thread.start()
            self._run(run)
            self.agent._stop_heartbeat.set()
            thread.join(5)
        self.assertEqual(self.statuses, [("cancelled", None, [])])
        self.assertIn(["job-1"], beats)

    def test_cancel_for_another_job_is_ignored(self):
        self.agent._current_job_id = "job-1"
        with mock.patch.object(self.agent, "_send_heartbeat", return_value=["job-2"]):
            threading.Timer(0.05, self.agent._stop_heartbeat.set).start()
            self.agent._heartbeat_loop()
        self.assertFalse(self.agent.executor.cancelled)

    def test_signal_handler_shuts_the_agent_down(self):
        handlers = {}

        def claim():
            handlers[signal.SIGTERM](signal.SIGTERM, None)

        with mock.patch("signal.signal", side_effect=lambda n, h: handlers.update({n: h})), \
                mock.patch.object(self.agent, "register"), \
                mock.patch.object(self.agent, "claim_job", side_effect=claim), \
                mock.patch.object(self.agent, "_send_heartbeat", return_value=[]):
            self.agent.start()
        self.assertTrue(self.agent._shutdown.is_set())
        self.assertTrue(self.agent.executor.cancelled)
        self.assertEqual(set(handlers), {signal.SIGTERM, signal.SIGINT})


class PendingResultTest(Test):
    """Tests for final job results: persisted, resent with backoff, flushed after outages."""

    def setUp(self):
        self.tmp_dir = os.path.realpath(tempfile.mkdtemp(prefix="seine-test-pending-"))
        self.now = 0.0
        self.sleeps = []
        self.agent = self._agent()

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _sleep(self, delay):
        self.sleeps.append(delay)
        self.now += delay

    def _agent(self, **kwargs):
        from seine.distributed.agent.daemon import WorkerAgent
        with mock.patch("seine.distributed.agent.daemon.detect_capabilities") as detect:
            detect.return_value = mock.MagicMock(native_arch="amd64", free_disk_gb=10.0)
            agent = WorkerAgent("http://localhost:8000", "enroll", self.tmp_dir, "w1",
                                sleep=self._sleep, clock=lambda: self.now,
                                **{"report_retry_total": 20, **kwargs})
        agent.worker_token = "wt"
        return agent

    def _path(self, job_id="job-1"):
        return os.path.join(self.tmp_dir, "pending", f"{job_id}.json")

    def _post(self, *outcomes):
        """Patch requests.post: each outcome is a status code or an exception."""
        seen = []

        def post(url, **kwargs):
            outcome = outcomes[min(len(seen), len(outcomes) - 1)]
            seen.append((url, kwargs["json"]))
            if isinstance(outcome, Exception):
                raise outcome
            return mock.MagicMock(status_code=outcome)

        self.posts = seen
        return mock.patch("requests.post", side_effect=post)

    def _send_result(self, job_id="job-1", status="completed", **kwargs):
        self.agent.update_job_status(job_id, "bld-1", status, **kwargs)

    def test_result_is_persisted_before_the_first_attempt(self):
        seen = []
        with mock.patch("requests.post", side_effect=lambda *a, **k: seen.append(
                os.path.exists(self._path())) or mock.MagicMock(status_code=200)):
            self._send_result(artifacts=[{"name": "a", "key": "k", "sha256": "0" * 64, "size": 1}])
        self.assertEqual(seen, [True])

    def test_ack_deletes_the_file(self):
        with self._post(200):
            self._send_result()
        self.assertFalse(os.path.exists(self._path()))
        self.assertEqual(len(self.posts), 1)

    def test_file_content_and_permissions(self):
        art = {"name": "a", "key": "k", "sha256": "0" * 64, "size": 1, "secret_key": "s3cr3t"}
        with self._post(ConnectionError("down")):
            self._send_result(status="failed", err="boom", artifacts=[art])
        with open(self._path()) as f:
            rec = json.load(f)
        self.assertEqual(set(rec), {"job_id", "build_id", "status", "error", "artifacts", "written_at"})
        self.assertEqual(rec["artifacts"], [{k: art[k] for k in ("name", "key", "sha256", "size")}])
        self.assertEqual((rec["job_id"], rec["status"], rec["error"]), ("job-1", "failed", "boom"))
        self.assertEqual(os.stat(self._path()).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(os.path.dirname(self._path())).st_mode & 0o777, 0o700)

    def test_errors_are_redacted_in_the_file(self):
        from seine import vault
        vault.record_secret("hunter2-secret")
        with self._post(ConnectionError("down")):
            self._send_result(status="failed", err="denied for hunter2-secret")
        with open(self._path()) as f:
            self.assertEqual(json.load(f)["error"], "denied for <redacted>")

    def test_connection_errors_back_off_then_keep_the_file(self):
        with self._post(ConnectionError("down")):
            self._send_result()
        self.assertEqual(self.sleeps, [1, 2, 4, 8])
        self.assertEqual(len(self.posts), 5)
        self.assertTrue(os.path.exists(self._path()))

    def test_backoff_is_capped_at_30_seconds(self):
        agent = self._agent(report_retry_total=300)
        with self._post(ConnectionError("down")):
            agent.update_job_status("job-1", "bld-1", "failed")
        self.assertEqual(self.sleeps[:7], [1, 2, 4, 8, 16, 30, 30])
        self.assertEqual(set(self.sleeps[5:]), {30})

    def test_server_errors_are_retried_and_a_later_ack_deletes(self):
        with self._post(503, 429, 200):
            self._send_result()
        self.assertEqual(self.sleeps, [1, 2])
        self.assertFalse(os.path.exists(self._path()))

    def test_rejections_delete_without_retry(self):
        for code in (403, 404, 409, 410):
            with self.subTest(code=code):
                with self._post(code):
                    self._send_result()
                self.assertFalse(os.path.exists(self._path()))
                self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.sleeps, [])

    def test_running_status_is_a_single_best_effort_post(self):
        with self._post(ConnectionError("down")):
            self._send_result(status="running")
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.sleeps, [])
        self.assertFalse(os.path.exists(os.path.join(self.tmp_dir, "pending")))

    def test_atomic_write_leaves_no_partial_file(self):
        with mock.patch("os.replace", side_effect=OSError("disk")), self._post(200):
            self._send_result()
        self.assertEqual(os.listdir(os.path.join(self.tmp_dir, "pending")), [])

    def test_flush_sends_every_file_and_one_bad_file_does_not_block(self):
        with self._post(ConnectionError("down")):
            self._send_result("job-1")
            self._send_result("job-3")
        with open(self._path("job-2"), "w") as f:
            f.write("{not json")
        with self._post(200):
            self.agent.flush_pending()
        self.assertEqual(sorted(p[1]["job_id"] for p in self.posts), ["job-1", "job-3"])
        self.assertEqual(os.listdir(os.path.dirname(self._path())), ["job-2.json"])

    def test_flush_keeps_files_the_server_cannot_take_yet(self):
        with self._post(ConnectionError("down")):
            self._send_result()
            self.agent.flush_pending()
        self.assertTrue(os.path.exists(self._path()))

    def test_flush_runs_right_after_register_at_start(self):
        order = []
        self.agent._running = False
        with mock.patch("signal.signal"), \
                mock.patch.object(self.agent, "register", side_effect=lambda: order.append("register")), \
                mock.patch.object(self.agent, "flush_pending", side_effect=lambda: order.append("flush")), \
                mock.patch.object(self.agent, "claim_job", side_effect=lambda: order.append("claim")
                                  or self.agent.stop()), \
                mock.patch.object(self.agent, "_heartbeat_loop"):
            self.agent.start()
        self.assertEqual(order[:3], ["register", "flush", "claim"])

    def test_heartbeat_tick_flushes_after_a_successful_heartbeat(self):
        with self._post(ConnectionError("down")):
            self._send_result()
        beat = mock.MagicMock(status_code=200)
        beat.json.return_value = {}
        calls = []

        def post(url, **kwargs):
            calls.append(url)
            self.agent._stop_heartbeat.set()
            return mock.MagicMock(status_code=200) if "/status" in url else beat

        with mock.patch("requests.post", side_effect=post), \
                mock.patch("seine.distributed.agent.daemon.detect_capabilities") as detect:
            detect.return_value = mock.MagicMock(free_disk_gb=1.0)
            self.agent._heartbeat_loop()
        self.assertEqual([u.rsplit("/", 1)[-1] for u in calls], ["heartbeat", "status"])
        self.assertFalse(os.path.exists(self._path()))

    def test_heartbeat_tick_does_not_flush_after_a_failed_heartbeat(self):
        def beat():
            self.agent._last_heartbeat_ok = False
            self.agent._stop_heartbeat.set()
            return []

        with mock.patch.object(self.agent, "_send_heartbeat", side_effect=beat), \
                mock.patch.object(self.agent, "flush_pending") as flush:
            self.agent._heartbeat_loop()
        flush.assert_not_called()

    def test_heartbeat_lists_the_running_and_the_pending_jobs(self):
        with self._post(ConnectionError("down")):
            self._send_result("job-7")
            self._send_result("job-5")
        self.agent._current_job_id = "job-9"
        resp = mock.MagicMock(status_code=200)
        resp.json.return_value = {}
        with mock.patch("requests.post", return_value=resp) as post, \
                mock.patch("seine.distributed.agent.daemon.detect_capabilities") as detect:
            detect.return_value = mock.MagicMock(free_disk_gb=1.0)
            self.agent._send_heartbeat()
        self.assertEqual(post.call_args.kwargs["json"]["running_jobs"], ["job-5", "job-7", "job-9"])

    def test_restarted_agent_sends_the_pending_result(self):
        art = {"name": "a", "key": "k", "sha256": "0" * 64, "size": 1}
        with self._post(ConnectionError("down")):
            self._send_result(artifacts=[art])
        restarted = self._agent()
        with self._post(200):
            restarted.flush_pending()
        url, body = self.posts[0]
        self.assertTrue(url.endswith("/api/v1/workers/jobs/job-1/status"))
        self.assertEqual((body["status"], body["build_id"], body["artifacts"]), ("completed", "bld-1", [art]))
        self.assertEqual(body["artifact_urls"], ["k"])
        self.assertFalse(os.path.exists(self._path()))
