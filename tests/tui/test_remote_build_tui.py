# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import contextlib
import os
import sys
import types
from unittest import mock

import avocado

path_to_self = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.insert(0, path_to_sources)

@contextlib.contextmanager
def _tui_required(test):
    try:
        yield
    except ImportError as e:
        test.cancel("the 'tui' extra (textual) is not installed: %s" % e)


class RemoteBuildTuiTest(avocado.Test):
    """
    :avocado: tags=tui
    """

    def setUp(self):
        # Remote builds allocate their log directory under logs_root().
        os.environ["SEINE_LOG_DIR"] = self.workdir
        with _tui_required(self):
            from seine.tui import build, commands
            from seine.tui.remote_session import RemoteSession
        self.build = build
        self.commands = commands
        self.RemoteSession = RemoteSession

    def _app(self, connected):
        app = mock.Mock()
        app.build_state.running = False
        app.vendor_state.running = False
        app.context.active = True
        app.context.builds = [mock.Mock(spec=["spec", "options"])]
        app.context.builds[0].spec = {"image": {}}
        app.context.builds[0].options = {"files": ["/w/main.yaml"]}
        session = self.RemoteSession(app=app)
        session.connected = connected
        session.url = "http://127.0.0.1:8000"
        session.active_project = "demo"
        app.remote_session = session
        return app

    def _dispatch(self, app, line):
        with mock.patch.object(self.build, "start_build") as local, \
                mock.patch.object(self.build, "start_remote_build") as remote:
            self.commands.dispatch(app, line)
        return local, remote

    def test_build_routes_remote_when_connected(self):
        app = self._app(True)
        local, remote = self._dispatch(app, "/build")
        local.assert_not_called()
        self.assertEqual(remote.call_args.args[2], ["/w/main.yaml"])
        self.assertFalse(remote.call_args.kwargs["no_download"])
        app.show.assert_called_once_with("build")

    def test_side_loaded_fragments_are_sent_with_the_build(self):
        app = self._app(True)
        app.context.builds[0].options = {"files": ["/w/main.yaml", "/w/frag.yaml"]}
        _, remote = self._dispatch(app, "/build")
        self.assertEqual(remote.call_args.args[2], ["/w/main.yaml", "/w/frag.yaml"])

    def test_remote_build_gets_every_spec_file(self):
        app = mock.Mock()
        app.run_worker = lambda fn, **kw: fn() or mock.Mock()
        session = self.RemoteSession(app=app)
        session.url, session.token = "http://127.0.0.1:8000", "t"
        state = self.build.BuildState()
        build = self._build_cmd()
        build.options = {"files": ["/w/main.yaml", "/w/frag.yaml"]}
        with mock.patch("seine.distributed.client.remote.RemoteBuild") as rb, \
                mock.patch("seine.tui.credentials.tui_prompt"):
            rb.return_value.run.return_value = 0
            self.build.start_remote_build(
                app, state, build.options["files"], session, project="demo", build=build)
        self.assertEqual(rb.call_args.args[2], ["/w/main.yaml", "/w/frag.yaml"])

    def test_build_local_flag_and_disconnected_stay_local(self):
        for connected, line in ((True, "/build --local"), (False, "/build")):
            local, remote = self._dispatch(self._app(connected), line)
            remote.assert_not_called()
            local.assert_called_once()

    def test_no_download_is_passed_on(self):
        _, remote = self._dispatch(self._app(True), "/build --no-download")
        self.assertTrue(remote.call_args.kwargs["no_download"])

    def test_cancel_stops_the_remote_job_not_the_local_scheduler(self):
        app = self._app(True)
        app.build_state.running = True
        app.build_state.remote_job = mock.Mock()
        with mock.patch.object(self.commands.tasks, "interrupt") as interrupt:
            self.commands.dispatch(app, "/cancel")
        app.build_state.remote_job.stop_requested.set.assert_called_once_with()
        interrupt.assert_not_called()

    def test_state_title_and_log_dir(self):
        state = self.build.BuildState()
        state.reset_remote("10.0.0.1:8000", mock.Mock())
        self.assertIn("[BUILD: REMOTE @ 10.0.0.1:8000]", state.render())
        self.assertEqual(state.message, "[BUILD: REMOTE @ 10.0.0.1:8000]")
        self.assertIsNone(state.logs)
        state.reset(mock.Mock(image=mock.Mock(tasks=lambda: [], packages=[]), spec={}))
        self.assertIsNone(state.remote)

    def test_remote_state_reports_the_local_log_dir(self):
        state = self.build.BuildState()
        state.reset_remote("host", mock.Mock(), "/logs/x")
        self.assertEqual(state.logs, "/logs/x")

    def test_remote_render_is_a_header_over_the_task_rows(self):
        state = self.build.BuildState()
        with mock.patch("time.time", return_value=1000.0):
            state.reset_remote("10.0.0.1:8000", mock.Mock())
            self.assertEqual(state.render(),
                             "[BUILD: REMOTE @ 10.0.0.1:8000]\n● remote build  0s\n")

    def test_remote_build_row_stops_when_the_build_ends(self):
        for finish, expected in (
            (lambda state: state.finished_ok(), "done"),
            (lambda state: state.finished_failed("remote build failed (exit 1)"), "failed"),
        ):
            state = self.build.BuildState()
            with mock.patch("time.time", return_value=1000.0):
                state.reset_remote("10.0.0.1:8000", mock.Mock())
            with mock.patch("time.time", return_value=1065.0):
                finish(state)
            row = state.rows["remote build"]
            self.assertEqual(row["state"], expected)
            self.assertEqual(row["elapsed"], 65.0)
            with mock.patch("time.time", return_value=5000.0):
                self.assertEqual(state.render(), state.render())
                self.assertIn("1m05s", state.render())
            self.assertIsNone(state.current)

    def test_finishing_a_remote_build_twice_keeps_the_first_elapsed_time(self):
        state = self.build.BuildState()
        with mock.patch("time.time", return_value=1000.0):
            state.reset_remote("host", mock.Mock())
        with mock.patch("time.time", return_value=1010.0):
            state.finished_failed("boom")
        with mock.patch("time.time", return_value=2000.0):
            state.finished_ok()
        self.assertEqual(state.rows["remote build"]["elapsed"], 10.0)

    PLAN = {"type": "task_plan", "tasks": [
        {"name": "rootfs", "needs": ["chroot", "package:amd64:busybox"]},
        {"name": "chroot", "needs": []},
        {"name": "package:amd64:busybox", "needs": [], "cached": True},
        {"name": "disk", "needs": ["rootfs"]}]}

    def _planned(self):
        state = self.build.BuildState()
        state.reset_remote("host", mock.Mock())
        state.remote_event(self.PLAN)
        return state

    def test_plan_replaces_the_placeholder_row(self):
        state = self._planned()
        self.assertNotIn("remote build", state.rows)
        self.assertEqual(state.order,
                         ["chroot", "package:amd64:busybox", "rootfs", "disk"])
        self.assertEqual(state.rows["package:amd64:busybox"]["state"], "cached")
        self.assertEqual(state.rows["rootfs"]["needs"],
                         ["chroot", "package:amd64:busybox"])
        text = state.render()
        self.assertIn("[BUILD: REMOTE @ host]", text)
        self.assertIn("\U0001F680 package:amd64:busybox", text)
        self.assertIn("○ disk", text)

    def test_parallel_tasks_run_with_their_own_timers(self):
        state = self._planned()
        with mock.patch("time.time", return_value=1000.0):
            state.remote_event({"type": "task_started", "task": "chroot"})
        with mock.patch("time.time", return_value=1030.0):
            state.remote_event({"type": "task_started", "task": "disk"})
        with mock.patch("time.time", return_value=1065.0):
            text = state.render()
            state.remote_event({"type": "task_finished", "task": "chroot", "failed": False})
        self.assertIn("● chroot  1m05s", text)
        self.assertIn("● disk  35s", text)
        self.assertEqual(state.rows["chroot"]["state"], "done")
        self.assertEqual(state.rows["disk"]["state"], "running")

    def test_say_and_sampled_events_set_the_message(self):
        state = self._planned()
        state.remote_event({"type": "say", "text": "interrupted"})
        self.assertEqual(state.message, "interrupted")
        state.remote_event({"type": "sampled", "sample": {"load": 1.5, "cpu": 0.5}})
        self.assertEqual(state.message, "load 1.50, 50% busy")

    def test_remote_tasks_do_not_fire_the_local_screen_hooks(self):
        state = self._planned()
        state.on_task_started = mock.Mock()
        state.on_task_finished = mock.Mock()
        state.remote_event({"type": "task_started", "task": "chroot"})
        state.remote_event({"type": "task_finished", "task": "chroot"})
        state.on_task_started.assert_not_called()
        state.on_task_finished.assert_not_called()

    def test_running_tasks_end_with_the_build(self):
        for finish, expected in ((lambda s: s.finished_ok(), "done"),
                                 (lambda s: s.finished_failed("x"), "failed")):
            state = self._planned()
            state.remote_event({"type": "task_started", "task": "chroot"})
            finish(state)
            self.assertEqual(state.rows["chroot"]["state"], expected)
            self.assertEqual(state.rows["disk"]["state"], "pending")

    def test_running_remote_tasks_highlight_the_spec_tree(self):
        from seine.tui.spectree import highlight_active
        state = self._planned()
        state.remote_event({"type": "task_started", "task": "rootfs"})
        tree = mock.Mock()
        tree.active_keys.return_value = []
        self.assertEqual(highlight_active(tree, state), {"rootfs"})

    def test_start_remote_build_feeds_events_to_the_state(self):
        app = mock.Mock()
        app.run_worker = lambda fn, **kw: fn() or mock.Mock()
        app.call_from_thread = lambda fn, *a: fn(*a)
        session = self.RemoteSession(app=app)
        session.url, session.token = "http://127.0.0.1:8000", "t"
        state = self.build.BuildState()
        with mock.patch("seine.distributed.client.remote.RemoteBuild") as rb, \
                mock.patch("seine.tui.credentials.tui_prompt"):
            rb.return_value.run.return_value = 0
            self.build.start_remote_build(app, state, "main.yaml", session, project="demo")
        rb.call_args.kwargs["on_event"](self.PLAN)
        self.assertIn("disk", state.rows)

    def _build_cmd(self):
        return types.SimpleNamespace(
            options={"files": ["/w/main.yaml"]}, image=types.SimpleNamespace(packages=[]),
            spec={"distribution": {"release": "trixie", "architecture": "amd64"}})

    def _remote_with_build(self, code):
        app = mock.Mock()
        app.run_worker = lambda fn, **kw: fn() or mock.Mock()
        app.call_from_thread = lambda fn, *a: fn(*a)
        session = self.RemoteSession(app=app)
        session.url, session.token = "http://127.0.0.1:8000", "t"
        build = self._build_cmd()
        state = self.build.BuildState()
        with mock.patch("seine.distributed.client.remote.RemoteBuild") as rb, \
                mock.patch("seine.tui.credentials.tui_prompt"):
            rb.return_value.run.side_effect = (
                lambda: rb.call_args.kwargs["on_event"](self.PLAN) or code)
            self.build.start_remote_build(
                app, state, "/w/main.yaml", session, project="demo", build=build)
        return state, rb

    def test_remote_build_keeps_its_logs_locally(self):
        state, rb = self._remote_with_build(0)
        self.assertEqual(rb.call_args.kwargs["log_dir"], state.logs)
        self.assertTrue(os.path.isdir(state.logs))
        self.assertTrue(state.logs.startswith(self.workdir))

    def test_remote_build_is_listed_in_the_log_catalog(self):
        from seine import logindex
        for code, ok in ((0, True), (1, False)):
            state, _ = self._remote_with_build(code)
            entry = logindex.entries()[0]
            self.assertEqual((entry["release"], entry["arch"], entry["ok"]),
                             ("trixie", "amd64", ok))
            self.assertEqual(entry["dir"], os.path.relpath(state.logs, self.workdir))
            by_name = {t["name"]: t for t in entry["tasks"]}
            self.assertTrue(by_name["package:amd64:busybox"]["cached"])
            self.assertIsNone(by_name["package:amd64:busybox"]["log"])

    def test_remote_build_without_a_plan_is_not_cataloged(self):
        from seine import logindex
        app = mock.Mock()
        app.run_worker = lambda fn, **kw: fn() or mock.Mock()
        app.call_from_thread = lambda fn, *a: fn(*a)
        session = self.RemoteSession(app=app)
        session.url, session.token = "http://127.0.0.1:8000", "t"
        build = self._build_cmd()
        with mock.patch("seine.distributed.client.remote.RemoteBuild") as rb, \
                mock.patch("seine.tui.credentials.tui_prompt"):
            rb.return_value.run.return_value = 1
            self.build.start_remote_build(
                app, self.build.BuildState(), "/w/main.yaml", session, project="demo", build=build)
        self.assertEqual(logindex.entries(), [])

    def test_remote_build_asks_the_worker_for_a_verbose_build(self):
        _, rb = self._remote_with_build(0)
        self.assertTrue(rb.call_args.kwargs["options"]["verbose"])

    def test_remote_build_precomputes_the_package_paths(self):
        paths = {"package:amd64:busybox": ["packages", "busybox"]}
        with mock.patch.object(self.build, "_package_paths", return_value=paths):
            state, _ = self._remote_with_build(0)
        self.assertEqual(state.package_paths, paths)

    def _screen(self, state):
        screen = mock.Mock()
        screen.app.build_state = state
        screen._tail = self.build.Tail()
        screen._scan_ansible = lambda state, text: self.build.BuildScreen._scan_ansible(
            screen, state, text)
        return screen

    def _follow(self, screen):
        with mock.patch.object(self.build, "sanitize", side_effect=lambda t: t):
            self.build.BuildScreen._follow(screen)
        return screen.query_one.return_value.write

    def test_remote_output_pane_follows_the_running_task(self):
        state = self._planned()
        state.remote_logs = self.workdir
        for name, text in (("chroot", "chroot text\n"), ("rootfs", "rootfs text\n")):
            with open(os.path.join(self.workdir, "%s.log" % name), "w") as f:
                f.write(text)
        screen = self._screen(state)
        state.remote_event({"type": "task_started", "task": "chroot"})
        self.assertEqual(self._follow(screen).call_args.args, ("chroot text\n",))
        state.remote_event({"type": "task_finished", "task": "chroot"})
        state.remote_event({"type": "task_started", "task": "rootfs"})
        self.assertEqual(self._follow(screen).call_args.args, ("rootfs text\n",))

    def test_remote_output_pane_shows_the_worker_output_before_the_plan(self):
        state = self.build.BuildState()
        state.reset_remote("host", mock.Mock(), self.workdir)
        with open(os.path.join(self.workdir, "build.log"), "w") as f:
            f.write("[client] hello\n")
        write = self._follow(self._screen(state))
        self.assertEqual(write.call_args.args, ("[client] hello\n",))

    def test_remote_rootfs_log_drives_the_ansible_highlight(self):
        state = self._planned()
        state.remote_logs = self.workdir
        with open(os.path.join(self.workdir, "rootfs.log"), "w") as f:
            f.write("PLAY [main] ***\nTASK [Install] ***\n")
        state.remote_event({"type": "task_started", "task": "rootfs"})
        self._follow(self._screen(state))
        self.assertEqual((state.play, state.ansible_task), ("main", "Install"))

    def test_start_remote_build_reports_exit_code(self):
        app = mock.Mock()
        app.run_worker = lambda fn, **kw: fn() or mock.Mock()
        app.call_from_thread = lambda fn, *a: fn(*a)
        session = self.RemoteSession(app=app)
        session.url, session.token = "http://127.0.0.1:8000", "t"
        for code, failed in ((0, False), (1, True)):
            state = self.build.BuildState()
            with mock.patch("seine.distributed.client.remote.RemoteBuild") as rb, \
                    mock.patch("seine.tui.credentials.tui_prompt"):
                rb.return_value.run.return_value = code
                self.build.start_remote_build(
                    app, state, "main.yaml", session, project="demo")
            self.assertEqual(state.error, failed)
            self.assertTrue(state.done)
            self.assertEqual(
                state.rows["remote build"]["state"], "failed" if failed else "done")

    def test_start_remote_build_passes_the_tls_policy_of_the_session(self):
        app = mock.Mock()
        app.run_worker = lambda fn, **kw: fn() or mock.Mock()
        app.call_from_thread = lambda fn, *a: fn(*a)
        session = self.RemoteSession(app=app)
        session.url, session.token = "http://10.0.0.1:8000", "t"
        session.insecure, session.ca_cert = True, "/etc/seine/ca.pem"
        with mock.patch("seine.distributed.client.remote.RemoteBuild") as rb, \
                mock.patch("seine.tui.credentials.tui_prompt"):
            rb.return_value.run.return_value = 0
            self.build.start_remote_build(
                app, self.build.BuildState(), "main.yaml", session, project="demo")
        options = rb.call_args.kwargs["options"]
        self.assertTrue(options["insecure"])
        self.assertEqual(options["ca_cert"], "/etc/seine/ca.pem")

    def test_project_flag_is_for_this_build_only(self):
        app = self._app(True)
        _, remote = self._dispatch(app, "/build --project web")
        self.assertEqual(remote.call_args.kwargs["project"], "web")
        self.assertEqual(app.remote_session.active_project, "demo")

    def test_session_project_is_used_when_there_is_one(self):
        _, remote = self._dispatch(self._app(True), "/build")
        self.assertEqual(remote.call_args.kwargs["project"], "demo")

    def _undecided(self):
        app = self._app(True)
        session = app.remote_session
        session.active_project = None
        session.projects = {"core": "developer", "web": "releaser"}
        return app, session

    def test_build_asks_for_a_project_before_starting(self):
        from seine.tui.project_picker import ProjectPicker
        app, session = self._undecided()
        local, remote = self._dispatch(app, "/build")
        remote.assert_not_called()
        local.assert_not_called()
        picker, callback = app.push_screen.call_args[0]
        self.assertIsInstance(picker, ProjectPicker)

        with mock.patch.object(self.build, "start_remote_build") as remote:
            callback(("core", False))
        self.assertEqual(remote.call_args.kwargs["project"], "core")
        self.assertEqual(session.active_project, "core")
        app.show.assert_called_with("build")

    def test_build_is_not_started_when_the_picker_is_cancelled(self):
        app, session = self._undecided()
        self._dispatch(app, "/build")
        callback = app.push_screen.call_args[0][1]
        with mock.patch.object(self.build, "start_remote_build") as remote:
            callback(None)
        remote.assert_not_called()
        self.assertIsNone(session.active_project)
        app.say.assert_called_with("build: no project chosen, not started", warning=True)

    def test_choice_can_become_the_default_project(self):
        app, session = self._undecided()
        self._dispatch(app, "/build")
        callback = app.push_screen.call_args[0][1]
        with mock.patch.object(self.build, "start_remote_build"), \
                mock.patch.object(session, "set_default_project", return_value=None) as save:
            callback(("web", True))
        save.assert_called_once_with("web")

    def test_member_of_no_project_cannot_start_a_remote_build(self):
        app, session = self._undecided()
        session.projects = {}
        _, remote = self._dispatch(app, "/build")
        remote.assert_not_called()
        app.push_screen.assert_not_called()
        app.say.assert_called_with("build: you are not a member of any project", warning=True)

    def test_local_build_never_asks_for_a_project(self):
        app, _ = self._undecided()
        local, remote = self._dispatch(app, "/build --local")
        local.assert_called_once()
        app.push_screen.assert_not_called()

    def test_remote_build_download_feeds_the_download_state(self):
        from seine.tui.download import DownloadState
        app = mock.Mock()
        app.run_worker = lambda fn, **kw: mock.Mock()
        app.download_state = DownloadState()
        calls = []
        app.call_from_thread = lambda fn, *a: calls.append((fn, a))
        session = self.RemoteSession(app=app)
        session.url, session.token = "http://127.0.0.1:8000", "t"
        with mock.patch("seine.distributed.client.remote.RemoteBuild") as rb, \
                mock.patch("seine.tui.credentials.tui_prompt"):
            self.build.start_remote_build(
                app, self.build.BuildState(), "main.yaml", session, project="demo")
        on_download = rb.call_args.kwargs["on_download"]
        state = app.download_state

        on_download("queue", "bld-1", "disk.raw", 200)
        self.assertTrue(state.active)
        on_download("start", "bld-1", "disk.raw", 0)
        on_download("bytes", "bld-1", "disk.raw", 50)
        self.assertEqual(state.percent(), 25)
        on_download("done", "bld-1", "disk.raw", 0)
        self.assertFalse(state.active)
        on_download("queue", "bld-1", "rootfs.tar", 10)
        on_download("failed", "bld-1", "rootfs.tar", 0)
        self.assertEqual(state.snapshot()[("bld-1", "rootfs.tar")]["state"], "failed")
        self.assertTrue(calls)
        self.assertTrue(all(fn.__name__ == "redraw" for fn, _ in calls))

    def test_download_hook_refuses_once_quitting_cancelled_the_downloads(self):
        from seine.distributed.client.remote import DownloadError
        from seine.tui.download import DownloadState
        app = mock.Mock()
        app.run_worker = lambda fn, **kw: mock.Mock()
        app.download_state = DownloadState()
        app.call_from_thread = lambda fn, *a: None
        session = self.RemoteSession(app=app)
        session.url, session.token = "http://127.0.0.1:8000", "t"
        with mock.patch("seine.distributed.client.remote.RemoteBuild") as rb, \
                mock.patch("seine.tui.credentials.tui_prompt"):
            self.build.start_remote_build(
                app, self.build.BuildState(), "main.yaml", session, project="demo")
        on_download = rb.call_args.kwargs["on_download"]
        on_download("queue", "bld-1", "disk.raw", 100)
        on_download("start", "bld-1", "disk.raw", 0)
        on_download("bytes", "bld-1", "disk.raw", 10)
        app.download_state.cancel()
        for kind in ("start", "bytes"):
            with self.assertRaises(DownloadError):
                on_download(kind, "bld-1", "disk.raw", 10)
        on_download("failed", "bld-1", "disk.raw", 0)

    def test_start_remote_build_needs_a_project(self):
        app = mock.Mock()
        session = self.RemoteSession(app=app)
        session.url = "http://127.0.0.1:8000"
        with self.assertRaises(RuntimeError):
            self.build.start_remote_build(
                app, self.build.BuildState(), "main.yaml", session)
