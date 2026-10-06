#!/usr/bin/env python3

import avocado
import fcntl
import hashlib
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine.utils import version_key

SCRIPT = os.path.join(path_to_sources, "examples", "common", "ostree-update", "seine-update")
STUBS = os.path.join(os.path.dirname(path_to_self), "ostree_update_stubs")

# Every step of a run, in order, with what a test must set up to reach it.
FAILPOINTS = ["after-elect", "after-pull", "after-prune-ukis", "after-deploy",
              "after-link", "after-prune", "after-uki", "after-bind"]


def commit(name):
    return hashlib.sha256(name.encode()).hexdigest()


class World:
    """A device in a temporary directory: ESP, sysroot and the stubs' state."""

    def __init__(self):
        self.top = tempfile.mkdtemp(prefix="seine-update-test-", dir="/var/tmp")
        self.esp = self.mkdir("esp")
        self.sysroot = self.mkdir("sysroot")
        self.state = os.path.join(self.top, "state")
        self.stub = self.mkdir("stub")
        self.bin = self.install_stubs()
        self.deploys = self.mkdir("sysroot/ostree/deploy/debian/deploy")
        self.conf = os.path.join(self.top, "seine-update.conf")
        self.order = []
        self.write("stub/order", "")
        self.write("stub/calls", "")
        self.set_conf()

    # The stubs are not executable in the tree: avocado would run them as tests.
    def install_stubs(self):
        bin_dir = self.mkdir("bin")
        for name in os.listdir(STUBS):
            shutil.copy(os.path.join(STUBS, name), bin_dir)
            os.chmod(os.path.join(bin_dir, name), 0o755)
        return bin_dir

    def mkdir(self, rel):
        path = os.path.join(self.top, rel)
        os.makedirs(path, exist_ok=True)
        return path

    def write(self, rel, text):
        with open(os.path.join(self.top, rel), "w") as f:
            f.write(text)

    def set_conf(self, extra=""):
        self.write("seine-update.conf", "STATEROOT=debian\nREF=debian/amd64\n"
                   f"REMOTE=seine\nESP={self.esp}\n{extra}")

    def remove(self):
        shutil.rmtree(self.top, ignore_errors=True)

    # Deployments are given newest first.
    def deploy(self, name, serial=0, booted=False, pinned=False, link=True):
        full = f"{commit(name)}.{serial}"
        self.mkdir(f"sysroot/ostree/deploy/debian/deploy/{full}")
        self.write(f"sysroot/ostree/deploy/debian/deploy/{full}.origin", "")
        self.order.append(full)
        self.write("stub/order", "".join(f"{n}\n" for n in self.order))
        if booted:
            self.write("stub/booted", full)
        if pinned:
            self.mkdir("stub/pinned")
            self.write(f"stub/pinned/{full}", "")
        if link:
            os.symlink(f"deploy/debian/deploy/{full}",
                       os.path.join(self.sysroot, "ostree", f"debian-{commit(name)}"))
        return full

    def uki(self, file, name):
        self.write(f"esp/{file}", f"root=PARTUUID=1 rw ostree=/ostree/debian-{commit(name)}")

    def offer(self, version, name, commit_version=None, uki_of=None):
        self.write("stub/new-version", version)
        self.write("stub/commit", commit(name))
        self.write("stub/commit-version", commit_version or version)
        self.mkdir("stub/payload")
        self.write(f"stub/payload/{version}",
                   f"root=PARTUUID=1 rw ostree=/ostree/debian-{commit(uki_of or name)}")

    def factory(self, version="21"):
        self.deploy("a", booted=True)
        self.uki(f"debian-{version}.efi", "a")

    def run(self, *args, failpoint=None, action=None, **extra):
        env = dict(os.environ)
        env.update({
            "PATH": self.bin + ":" + env["PATH"],
            "STUB_DIR": self.stub,
            "SEINE_UPDATE_CONF": self.conf,
            "SEINE_UPDATE_ESP": self.esp,
            "SEINE_UPDATE_SYSROOT": self.sysroot,
            "SEINE_UPDATE_STATE": self.state,
            "SEINE_UPDATE_LOCK": os.path.join(self.top, "lock"),
            "SEINE_UPDATE_SYSUPDATE": os.path.join(self.bin, "systemd-sysupdate"),
            "SEINE_UPDATE_OSTREE": os.path.join(self.bin, "ostree"),
            "SEINE_UPDATE_BOOTCTL": os.path.join(self.bin, "bootctl"),
        })
        if failpoint:
            env["SEINE_UPDATE_FAILPOINT"] = failpoint
        if action:
            env["SEINE_UPDATE_FAILPOINT_ACTION"] = action
        env.update(extra)
        return subprocess.run([SCRIPT, *args], env=env, capture_output=True, text=True)

    def result(self):
        try:
            with open(os.path.join(self.state, "status")) as f:
                return dict(line.rstrip("\n").split("=", 1) for line in f)
        except FileNotFoundError:
            return {}

    def calls(self):
        with open(os.path.join(self.stub, "calls")) as f:
            return f.read().splitlines()

    def ukis(self):
        return sorted(n for n in os.listdir(self.esp))

    def deployments(self):
        return sorted(n for n in os.listdir(self.deploys) if not n.endswith(".origin"))

    def links(self):
        return sorted(n for n in os.listdir(os.path.join(self.sysroot, "ostree"))
                      if n.startswith("debian-"))

    def pinned(self):
        path = os.path.join(self.stub, "pinned")
        return sorted(os.listdir(path)) if os.path.isdir(path) else []

    def snapshot(self):
        return (self.ukis(), self.deployments(), self.links(), self.pinned())


class UpdateAgent(avocado.Test):
    def setUp(self):
        self.w = World()

    def tearDown(self):
        self.w.remove()

    def assert_ok(self, proc):
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def assert_check(self):
        proc = self.w.run("check")
        self.assertEqual(proc.returncode, 0, proc.stderr)

    # T1
    def test_nothing_new(self):
        self.w.factory()
        self.assert_ok(self.w.run())
        self.assertEqual(self.w.result()["result"], "nothing")
        self.assertFalse([c for c in self.w.calls() if c.startswith("ostree pull")])

    def test_new_version_is_installed(self):
        self.w.factory()
        self.w.offer("22", "b")
        self.assert_ok(self.w.run())
        res = self.w.result()
        self.assertEqual((res["result"], res["version"]), ("ok", "22"))
        self.assertIn("debian-22+3.efi", self.w.ukis())
        self.assertIn(f"debian-{commit('b')}", self.w.links())
        self.assertEqual(len(self.w.deployments()), 2)
        self.assert_check()

    def test_a_bad_list_fails_visibly(self):
        self.w.factory()
        self.w.offer("22", "b")
        self.w.write("stub/list-fail", "")
        proc = self.w.run()
        self.assertEqual(proc.returncode, 1)
        res = self.w.result()
        self.assertEqual(res["result"], "failed")
        self.assertIn("BAD signature", res["message"])

    def test_a_failed_pull_fails_visibly(self):
        self.w.factory()
        self.w.offer("22", "b")
        self.w.write("stub/pull-fail", "")
        self.assertEqual(self.w.run().returncode, 1)
        self.assertEqual(self.w.result()["result"], "failed")

    def test_a_bad_version_text_is_refused(self):
        self.w.factory()
        self.w.offer("22", "b")
        self.w.write("stub/new-version", "22/../x")
        self.assertEqual(self.w.run().returncode, 1)
        self.assertEqual(self.w.result()["result"], "failed")

    def test_a_bad_command_is_refused(self):
        self.w.factory()
        self.assertEqual(self.w.run("frobnicate").returncode, 2)

    # T2
    def test_commit_version_must_match(self):
        self.w.factory()
        self.w.offer("22", "b", commit_version="21")
        self.assertEqual(self.w.run().returncode, 1)
        self.assertIn("differs", self.w.result()["message"])
        self.assertEqual(self.w.ukis(), ["debian-21.efi"])

    def test_version_must_be_newer(self):
        self.w.factory("22")
        for v in ("22", "21", "9"):
            self.w.offer(v, "b")
            self.assertEqual(self.w.run().returncode, 1, v)
            self.assertIn("not newer", self.w.result()["message"])
        self.assertFalse([c for c in self.w.calls() if c.startswith("ostree pull")])

    def test_dotted_versions_compare_as_numbers(self):
        self.w.factory("1.9")
        self.w.offer("1.10", "b")
        self.assert_ok(self.w.run())
        self.assertEqual(self.w.result()["result"], "ok")

    # T3
    def test_a_second_instance_is_refused(self):
        self.w.factory()
        self.w.offer("22", "b")
        with open(os.path.join(self.w.top, "lock"), "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            self.assert_ok(self.w.run())
        self.assertEqual(self.w.result()["result"], "busy")
        self.assertEqual(self.w.ukis(), ["debian-21.efi"])

    def test_waits_for_the_good_boot(self):
        self.w.factory()
        self.w.deploy("b", booted=False)
        self.w.uki("debian-22+3.efi", "b")
        self.w.offer("23", "c")
        self.assert_ok(self.w.run())
        self.assertEqual(self.w.result()["result"], "waiting")
        self.assertFalse([c for c in self.w.calls() if c.startswith("ostree pull")])

    # T4
    def test_a_dead_link_is_repaired(self):
        self.w.factory()
        os.symlink("deploy/debian/deploy/gone", os.path.join(self.w.sysroot, "ostree", "debian-dead"))
        os.unlink(os.path.join(self.w.sysroot, "ostree", f"debian-{commit('a')}"))
        self.assertEqual(self.w.run("check").returncode, 1)
        self.assert_ok(self.w.run("reconcile"))
        self.assertEqual(self.w.links(), [f"debian-{commit('a')}"])
        self.assert_check()

    def test_a_uki_without_deployment_is_removed(self):
        self.w.factory()
        self.w.uki("debian-20.efi", "gone")
        self.assert_ok(self.w.run("reconcile"))
        self.assertEqual(self.w.ukis(), ["debian-21.efi"])

    def test_reconcile_is_idempotent(self):
        self.w.factory()
        self.w.uki("debian-20.efi", "gone")
        self.w.write("esp/.#sysupdate-x.efi", "")
        self.assert_ok(self.w.run("reconcile"))
        first = self.w.snapshot()
        self.assert_ok(self.w.run("reconcile"))
        self.assertEqual(self.w.snapshot(), first)

    def test_a_full_esp_refuses(self):
        self.w.factory()
        self.w.offer("22", "b")
        self.w.write("stub/esp-free", "1000")
        self.assertEqual(self.w.run().returncode, 1)
        self.assertIn("room", self.w.result()["message"])
        self.assertEqual(self.w.ukis(), ["debian-21.efi"])

    def test_nothing_is_deleted_when_bootctl_shows_nothing(self):
        self.w.factory()
        self.w.uki("debian-20.efi", "gone")
        self.w.write("stub/bootctl-empty", "")
        before = self.w.snapshot()
        self.assertEqual(self.w.run("reconcile").returncode, 1)
        self.assertEqual(self.w.run().returncode, 1)
        self.assertEqual(self.w.snapshot(), before)
        self.assertEqual(self.w.result()["result"], "failed")

    def test_nothing_is_deleted_when_ostree_shows_nothing(self):
        self.w.factory()
        self.w.deploy("b")
        self.w.uki("debian-19.efi", "b")
        self.w.offer("22", "c")
        broken = os.path.join(self.w.top, "ostree-broken")
        with open(broken, "w") as f:
            f.write("#!/bin/sh\n[ \"$1 $2\" = 'admin status' ] && exit 0\n"
                    f"exec {os.path.join(self.w.bin, 'ostree')} \"$@\"\n")
        os.chmod(broken, 0o755)
        before = self.w.snapshot()
        proc = self.w.run(SEINE_UPDATE_OSTREE=broken)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(self.w.snapshot(), before)

    def test_the_last_known_good_survives_without_image_version(self):
        # The script does not read os-release: retention comes from the ESP names.
        self.w.factory("os")
        self.w.offer("22", "b")
        self.w.run()
        self.assertIn("debian-os.efi", self.w.ukis())

    # T4b
    def test_the_last_known_good_is_chosen_among_states(self):
        w = self.w
        w.deploy("a", booted=True)
        w.deploy("b")
        w.deploy("c")
        w.deploy("d")
        w.uki("debian-20.efi", "a")           # blessed
        w.uki("debian-21.efi", "b")           # blessed, higher
        w.uki("debian-22+2-1.efi", "d")       # trial
        w.uki("debian-23+0-3.efi", "c")       # failed
        w.uki("debian-24.efi", "gone")        # blessed, deployment gone
        self.assert_ok(w.run("reconcile"))
        self.assertIn("debian-21.efi", w.ukis())
        self.assertNotIn("debian-24.efi", w.ukis())
        self.assertEqual(w.pinned(), [f"{commit('b')}.0"])

    def test_no_last_known_good_refuses(self):
        w = self.w
        w.deploy("a", booted=True)
        w.uki("debian-22+3.efi", "a")
        w.offer("23", "b")
        self.assertEqual(w.run("check").returncode, 1)
        self.assertEqual(w.run("reconcile").returncode, 1)

    def test_election_pins_exactly_the_last_known_good(self):
        w = self.w
        w.deploy("a", pinned=True)
        w.deploy("b", booted=True)
        w.uki("debian-20.efi", "a")
        w.uki("debian-21.efi", "b")
        self.assert_ok(w.run("reconcile"))
        self.assertEqual(w.pinned(), [f"{commit('b')}.0"])
        self.assert_ok(w.run("reconcile"))
        self.assertEqual(w.pinned(), [f"{commit('b')}.0"])

    def test_the_pin_moves_only_after_the_bless(self):
        w = self.w
        w.factory()
        w.offer("22", "b")
        self.assert_ok(w.run())
        self.assertEqual(w.pinned(), [f"{commit('a')}.0"])
        # Trial boot of 22: still not blessed, the pin stays.
        w.write("stub/booted", f"{commit('b')}.0")
        self.assert_ok(w.run("reconcile"))
        self.assertEqual(w.pinned(), [f"{commit('a')}.0"])
        # systemd-bless-boot renames the UKI.
        os.rename(os.path.join(w.esp, "debian-22+3.efi"), os.path.join(w.esp, "debian-22.efi"))
        self.assert_ok(w.run("reconcile"))
        self.assertEqual(w.pinned(), [f"{commit('b')}.0"])

    def test_a_third_deploy_drops_the_factory_deployment_and_uki(self):
        w = self.w
        w.factory()
        names = ["b", "c"]
        for version, name in zip(("22", "23"), names):
            w.offer(version, name)
            self.assert_ok(w.run())
            booted = f"{commit(name)}.0"
            w.write("stub/booted", booted)
            os.rename(os.path.join(w.esp, f"debian-{version}+3.efi"),
                      os.path.join(w.esp, f"debian-{version}.efi"))
            self.assert_ok(w.run("reconcile"))
            self.assert_check()
        self.assertEqual(w.ukis(), ["debian-22.efi", "debian-23.efi"])
        self.assertNotIn(f"{commit('a')}.0", w.deployments())
        self.assertNotIn(f"debian-{commit('a')}", w.links())

    def failed_world(self):
        w = self.w
        w.deploy("a", booted=True, pinned=True)
        w.deploy("b")
        w.uki("debian-21.efi", "a")
        w.uki("debian-22+0-3.efi", "b")
        return w

    def test_a_failed_update_is_denied_and_cleaned(self):
        w = self.failed_world()
        self.assert_ok(w.run("reconcile"))
        self.assertEqual(w.ukis(), ["debian-21.efi"])
        self.assertEqual(w.deployments(), [f"{commit('a')}.0"])
        self.assertEqual(w.links(), [f"debian-{commit('a')}"])
        with open(os.path.join(w.state, "failed")) as f:
            fields = f.read().split()
        self.assertEqual(fields[:2], ["22", f"{commit('b')}.0"])
        self.assertEqual(w.result()["result"], "failed-update")
        self.assertEqual(w.result()["version"], "22")
        self.assert_check()

    def test_a_denied_version_is_skipped(self):
        w = self.failed_world()
        w.offer("22", "b")
        self.assert_ok(w.run())
        res = w.result()
        self.assertEqual((res["result"], res["version"]), ("nothing", "22"))
        self.assertIn("denied", res["message"])
        self.assertFalse([c for c in w.calls() if c.startswith("ostree pull")])
        # A fixed rebuild has a higher version and goes through.
        w.offer("23", "c")
        self.assert_ok(w.run())
        self.assertEqual(w.result()["result"], "ok")

    def test_keep_failed_keeps_the_files_but_denies(self):
        w = self.failed_world()
        w.set_conf("KEEP_FAILED=1\n")
        self.assert_ok(w.run("reconcile"))
        self.assertEqual(w.ukis(), ["debian-21.efi", "debian-22+0-3.efi"])
        self.assertEqual(len(w.deployments()), 2)
        w.offer("22", "b")
        self.assert_ok(w.run())
        self.assertEqual(w.result()["result"], "nothing")

    def test_cleanup_never_undeploys_the_booted_deployment(self):
        w = self.w
        w.deploy("b", booted=True)
        w.deploy("a", pinned=True)
        w.uki("debian-21.efi", "a")
        w.uki("debian-22+0-3.efi", "b")
        self.assert_ok(w.run("reconcile"))
        self.assertIn(f"{commit('b')}.0", w.deployments())

    def random_world(self, rnd):
        """Four commits, some with a live deployment, each with a UKI in a random state."""
        w = World()
        names = "abcd"
        alive = [n for n in names if rnd.random() < 0.8] or ["a"]
        booted = rnd.choice(alive)
        for n in alive:
            w.deploy(n, booted=(n == booted), pinned=rnd.random() < 0.3)
        ukis = {}
        for i, n in enumerate(names):
            file = f"debian-{20 + i}{rnd.choice(['', '', '+2-1', '+0-3'])}.efi"
            w.uki(file, n)
            ukis[file] = n
        return w, ukis, alive, booted

    def test_retention_never_removes_the_last_known_good(self):
        rnd = random.Random(4)
        for _ in range(40):
            w, ukis, alive, booted = self.random_world(rnd)
            try:
                blessed = [f for f, n in ukis.items() if "+" not in f and n in alive]
                w.offer("30", "e")
                proc = w.run()
                if not blessed:
                    self.assertEqual(proc.returncode, 1, proc.stderr)
                    continue
                lkg = max(blessed, key=lambda f: version_key(f[len("debian-"):-len(".efi")]))
                self.assertIn(lkg, w.ukis(), (ukis, proc.stderr))
                self.assertIn(f"{commit(ukis[lkg])}.0", w.deployments(), (ukis, proc.stderr))
                self.assertIn(f"{commit(booted)}.0", w.deployments(), (ukis, proc.stderr))
            finally:
                w.remove()

    # T5
    def test_a_cut_at_every_failpoint_leaves_a_sane_device(self):
        for point in FAILPOINTS:
            w = World()
            try:
                w.factory()
                w.offer("22", "b")
                proc = w.run(failpoint=point)
                self.assertEqual(proc.returncode, -9, (point, proc.stderr))
                self.assertIn(f"FAILPOINT {point}", proc.stdout)
                self.assert_ok_in(w, w.run("reconcile"), point)
                self.assert_ok_in(w, w.run("check"), point)
                # The next run finishes the update or waits for the boot.
                self.assert_ok_in(w, w.run(), point)
                self.assertIn(w.result()["result"], ("ok", "waiting"), point)
                self.assert_ok_in(w, w.run("check"), point)
            finally:
                w.remove()

    def test_a_cut_during_the_cleanup_of_a_failed_update(self):
        for point in ("after-denylist", "after-undeploy"):
            w = World()
            try:
                w.deploy("a", booted=True, pinned=True)
                w.deploy("b")
                w.uki("debian-21.efi", "a")
                w.uki("debian-22+0-3.efi", "b")
                proc = w.run("reconcile", failpoint=point)
                self.assertEqual(proc.returncode, -9, (point, proc.stderr))
                self.assert_ok_in(w, w.run("reconcile"), point)
                self.assertEqual(w.ukis(), ["debian-21.efi"], point)
                self.assertEqual(w.deployments(), [f"{commit('a')}.0"], point)
                with open(os.path.join(w.state, "failed")) as f:
                    self.assertEqual(len(f.read().splitlines()), 1, point)
                self.assert_ok_in(w, w.run("check"), point)
            finally:
                w.remove()

    def assert_ok_in(self, w, proc, point):
        self.assertEqual(proc.returncode, 0, (point, proc.stderr))

    # T6
    def test_a_uki_of_another_commit_is_removed(self):
        self.w.factory()
        self.w.offer("22", "b", uki_of="other")
        self.assertEqual(self.w.run().returncode, 1)
        self.assertIn("does not name the commit", self.w.result()["message"])
        self.assertEqual(self.w.ukis(), ["debian-21.efi"])

    # Every deletion goes through the guard.
    def function_lines(self, text, name):
        lines, inside = [], False
        for line in text.splitlines():
            if line.startswith(f"{name}() {{"):
                inside = True
            elif inside and line == "}":
                inside = False
            elif inside:
                lines.append(line)
        return lines

    def test_an_incomplete_deployment_is_cleaned_and_redeployed(self):
        self.w.factory()
        self.w.offer("22", "b")
        # Incomplete deployment from an interrupted deploy (not in status_rows).
        partial = os.path.join(self.w.deploys, f"{commit('b')}.0")
        os.makedirs(partial, exist_ok=True)
        self.assert_ok(self.w.run("reconcile"))
        self.assertFalse(os.path.exists(partial))
        self.assert_ok(self.w.run("check"))
        self.assert_ok(self.w.run())
        self.assertIn("debian-22+3.efi", self.w.ukis())
        self.assert_check()

    def test_a_corrupt_trial_uki_is_removed_by_reconcile(self):
        self.w.factory()
        self.w.offer("22", "b")
        # Empty file simulates a corrupt PE file unparseable by bootctl.
        corrupt = os.path.join(self.w.esp, "debian-22+3-0.efi")
        with open(corrupt, "w") as f:
            f.write("")
        self.assert_ok(self.w.run("reconcile"))
        self.assertFalse(os.path.exists(corrupt))
        self.assert_check()
        self.assert_ok(self.w.run())
        self.assertIn("debian-22+3.efi", self.w.ukis())
        self.assert_check()

    def test_every_deletion_goes_through_the_guard(self):
        with open(SCRIPT) as f:
            text = f.read()
        code = [l for l in text.splitlines() if not l.lstrip().startswith("#")]
        removers = [l for l in code if re.search(r"(^|[\s;|&(])(rm|unlink|shred|rmdir)\s", l)]
        self.assertEqual([l.strip() for l in removers], ['rm -rf "${1:?}"'])
        self.assertIn(removers[0], self.function_lines(text, "drop_file"))
        undeploys = [l for l in code if re.search(r"\badmin undeploy\b", l)]
        self.assertEqual(len(undeploys), 1)
        self.assertIn(undeploys[0], self.function_lines(text, "drop_deployment"))

    def test_the_script_is_shellcheck_clean(self):
        if shutil.which("shellcheck") is None:
            self.cancel("shellcheck is not installed")
        proc = subprocess.run(["shellcheck", SCRIPT], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout)

    def test_the_stubs_are_shellcheck_clean(self):
        if shutil.which("shellcheck") is None:
            self.cancel("shellcheck is not installed")
        proc = subprocess.run(["shellcheck", "-s", "sh"] +
                              [os.path.join(STUBS, n) for n in os.listdir(STUBS)],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout)
