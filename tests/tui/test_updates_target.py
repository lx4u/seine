# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import asyncio
import contextlib
import os
import shutil
import sys
import tempfile
import types
from unittest import mock

import avocado

path_to_self = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.insert(0, path_to_sources)

PC_OSTREE_UPDATE_IMAGE = os.path.join(
    path_to_sources, "examples", "pc-ostree-update-image", "main.yaml"
)

@contextlib.contextmanager
def _tui_required(test):
    try:
        yield
    except ImportError as e:
        test.cancel(f"the 'tui' extra (textual) is not installed: {e}")

def _run(coro):
    asyncio.run(coro())

def _content(widget):
    renderable = widget.render()
    return renderable.text if hasattr(renderable, "text") else str(renderable)


class UpdatesTargetIntegration(avocado.Test):
    """
    :avocado: tags=tui
    """

    def setUp(self):
        with _tui_required(self):
            from seine.tui import target
            from seine.tui.app import SeineApp
            from seine.tui.updates_screen import UpdatesScreen, LineageTable
        self.target = target
        self.SeineApp = SeineApp
        self.UpdatesScreen = UpdatesScreen
        self.LineageTable = LineageTable

        self.tmp_dir = tempfile.mkdtemp(prefix="seine-test-updates-target-", dir="/var/tmp")
        self.deploy = os.path.join(self.tmp_dir, "deploy")
        os.makedirs(self.deploy, exist_ok=True)
        os.environ["SEINE_CACHE_DIR"] = self.tmp_dir
        os.environ["SEINE_DEPLOY_DIR"] = self.deploy
        os.environ["SEINE_HISTORY_FILE"] = os.path.join(self.tmp_dir, "history.json")

    def tearDown(self):
        os.environ.pop("SEINE_HISTORY_FILE", None)
        os.environ.pop("SEINE_CACHE_DIR", None)
        os.environ.pop("SEINE_DEPLOY_DIR", None)
        if hasattr(self, "tmp_dir") and os.path.isdir(self.tmp_dir):
            shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _setup_mock_lineage(self, deploy_rel, versions):
        payload_dir = os.path.join(deploy_rel, "pc-ostree-update-payload")
        uki_dir = os.path.join(payload_dir, "uki")
        repo_dir = os.path.join(payload_dir, "repo")
        heads_dir = os.path.join(repo_dir, "refs", "heads", "debian")
        os.makedirs(uki_dir, exist_ok=True)
        os.makedirs(heads_dir, exist_ok=True)

        for v in versions:
            commit_hash = f"{v}c9811e5f2847c1b5a329d91cb61d8a391e0a98c5d3129841f3e790a12b8cd12"
            with open(os.path.join(heads_dir, f"amd64.v{v}"), "w") as f:
                f.write(f"{commit_hash}\n")
            with open(os.path.join(uki_dir, f"debian-{v}.efi"), "wb") as f:
                f.write(b"uki" * 1024)

        tip_commit = f"{versions[-1]}c9811e5f2847c1b5a329d91cb61d8a391e0a98c5d3129841f3e790a12b8cd12"
        with open(os.path.join(heads_dir, "amd64"), "w") as f:
            f.write(f"{tip_commit}\n")

        with open(os.path.join(deploy_rel, "pc-ostree-update.ostree-commit"), "w") as f:
            f.write(f"{tip_commit}\n")
        with open(os.path.join(deploy_rel, "pc-ostree-update.ostree-ref"), "w") as f:
            f.write("debian/amd64\n")

        return payload_dir

    def test_parse_console_deployment_rollback_and_active(self):
        sample = (
            "root@target:~# cat /var/lib/seine-update/status\n"
            "time=1729094000\n"
            "result=failed-update\n"
            "version=5\n"
            "message=the update to 5 did not boot\n"
            "root@target:~# grep -q 'VERSION_ID=4' /etc/os-release && echo 'Target at v4'\n"
            "Target at v4\n"
        )
        res = self.target.parse_console_deployment(sample)
        self.assertEqual(res.get("failed_version"), "5")
        self.assertEqual(res.get("rolled_back_version"), "5")
        self.assertEqual(res.get("active_version"), "4")

    def test_parse_console_deployment_ok_active(self):
        sample = (
            "seine-update: pulling payload\n"
            "result=ok\n"
            "version=2\n"
            "IMAGE_VERSION=2\n"
        )
        res = self.target.parse_console_deployment(sample)
        self.assertEqual(res.get("active_version"), "2")

    def test_parse_console_deployment_ostree_admin_status(self):
        sample = (
            "* debian 4c9811e5f2847c1b5a329d91cb61d8a391e0a98c5d3129841f3e790a12b8cd12.0\n"
            "  debian 8f2b31c9a07123456789abcdef0123456789abcdef0123456789abcdef012345.0\n"
        )
        res = self.target.parse_console_deployment(sample)
        self.assertEqual(res.get("active_commit"), "4c9811e5f2847c1b5a329d91cb61d8a391e0a98c5d3129841f3e790a12b8cd12")

    def test_target_state_seed_and_status(self):
        state = self.target.TargetState()
        st = {
            "power": "ON",
            "uptime": 120,
            "storage": ("TARGET", False, 0),
            "usb": [],
            "active_version": "4",
            "rolled_back_version": "5",
            "active_commit": "4c9811e5f2",
            "lkg_version": "4",
        }
        state.seed(st)
        self.assertEqual(state.power, "ON")
        self.assertEqual(state.storage, "TARGET")
        self.assertEqual(state.active_version, "4")
        self.assertEqual(state.rolled_back_version, "5")
        self.assertEqual(state.active_commit, "4c9811e5f2")
        self.assertEqual(state.lkg_version, "4")

    def test_probe_deployment_with_console_dump(self):
        fake_client = types.SimpleNamespace(
            console_dump=lambda: "result=failed-update\nversion=5\nVERSION_ID=4\n",
            env_get=lambda key: None,
        )
        app = types.SimpleNamespace(_target_client=fake_client)
        dep = self.target.probe_deployment(app)
        self.assertEqual(dep.get("active_version"), "4")
        self.assertEqual(dep.get("rolled_back_version"), "5")

    def test_updates_screen_target_active_badge(self):
        deploy_rel = os.path.join(self.deploy, "trixie")
        os.makedirs(deploy_rel, exist_ok=True)
        self._setup_mock_lineage(deploy_rel, ["1", "2"])

        async def scenario():
            app = self.SeineApp(files=[PC_OSTREE_UPDATE_IMAGE])
            app.target_state.agent = "192.168.1.111:50051"
            app.target_state.active_version = "2"
            async with app.run_test() as pilot:
                app.show("updates")
                await pilot.pause()
                self.assertIsInstance(app.screen, self.UpdatesScreen)

                lineage_table = app.screen.query_one(self.LineageTable)
                row_v1 = lineage_table.get_row("1")
                row_v2 = lineage_table.get_row("2")

                self.assertIn("[LKG]", str(row_v1[4]))
                self.assertIn("● Target (Active)", str(row_v2[4]))

                app.screen._update_commit_detail("2")
                detail = str(_content(app.screen.query_one("#detail-content")))
                self.assertIn("Target:  ● Active hardware deployment", detail)

                app.screen._update_commit_detail("1")
                detail_v1 = str(_content(app.screen.query_one("#detail-content")))
                self.assertIn("Target:  Not deployed on target", detail_v1)

        _run(scenario)

    def test_updates_screen_target_rollback_and_detail(self):
        deploy_rel = os.path.join(self.deploy, "trixie")
        os.makedirs(deploy_rel, exist_ok=True)
        self._setup_mock_lineage(deploy_rel, ["1", "4", "5"])

        async def scenario():
            app = self.SeineApp(files=[PC_OSTREE_UPDATE_IMAGE])
            app.target_state.agent = "192.168.1.111:50051"
            app.target_state.active_version = "4"
            app.target_state.rolled_back_version = "5"
            async with app.run_test() as pilot:
                app.show("updates")
                await pilot.pause()
                self.assertIsInstance(app.screen, self.UpdatesScreen)

                lineage_table = app.screen.query_one(self.LineageTable)
                row_v1 = lineage_table.get_row("1")
                row_v4 = lineage_table.get_row("4")
                row_v5 = lineage_table.get_row("5")

                self.assertIn("[LKG]", str(row_v1[4]))
                self.assertIn("● Target (Active)", str(row_v4[4]))
                self.assertIn("[Rolled Back]", str(row_v5[4]))

                header = str(_content(app.screen.query_one("#updates-header")))
                self.assertIn("Active: v4", header)
                self.assertIn("Rolled Back: v5", header)

                app.screen._update_commit_detail("5")
                detail_v5 = str(_content(app.screen.query_one("#detail-content")))
                self.assertIn("Target:  ⚠ Rolled back on hardware", detail_v5)

        _run(scenario)

    def test_updates_screen_without_target_state(self):
        deploy_rel = os.path.join(self.deploy, "trixie")
        os.makedirs(deploy_rel, exist_ok=True)
        self._setup_mock_lineage(deploy_rel, ["1", "2"])

        async def scenario():
            app = self.SeineApp(files=[PC_OSTREE_UPDATE_IMAGE])
            async with app.run_test() as pilot:
                app.show("updates")
                await pilot.pause()
                self.assertIsInstance(app.screen, self.UpdatesScreen)

                lineage_table = app.screen.query_one(self.LineageTable)
                row_v1 = lineage_table.get_row("1")
                row_v2 = lineage_table.get_row("2")

                self.assertEqual(str(row_v1[4]), "[LKG]")
                self.assertEqual(str(row_v2[4]), "[Active]")

        _run(scenario)
