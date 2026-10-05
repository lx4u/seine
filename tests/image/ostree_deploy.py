#!/usr/bin/env python3

import avocado
import os
import sys

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine.imager import ostree

# Plain Python, no guestfs appliance involved -- records what is run.
class FakeGuestfs:
    def __init__(self):
        self.commands = []
        self.written = {}
        self.removed = []
        self.dirs = []

    def command(self, argv):
        self.commands.append(argv)
        return "abc123\n"

    def write(self, path, content):
        self.written[path] = content

    def mkdir_p(self, path):
        self.dirs.append(path)

    def chmod(self, mode, path):
        pass

    def rm_rf(self, path):
        self.removed.append(path)

    def ls(self, path):
        return ["abc.0", "abc.0.origin"]

    def set_e2attrs(self, path, attrs, clear=False):
        self.unlocked = getattr(self, "unlocked", []) + [(path, attrs, clear)]

class MountPaths(avocado.Test):
    def test_var_belongs_to_the_stateroot(self):
        self.assertEqual(ostree.target_path("/var/", "debian"),
                         "/sysroot/ostree/deploy/debian/var")
        self.assertEqual(ostree.target_path("/var/home/", "debian"),
                         "/sysroot/ostree/deploy/debian/var/home")

    def test_other_mounts_sit_under_the_sysroot(self):
        self.assertEqual(ostree.target_path("/efi/", "debian"), "/sysroot/efi")
        self.assertEqual(ostree.target_path("/boot/", "debian"), "/sysroot/boot")

    def test_a_name_starting_like_var_is_not_var(self):
        self.assertEqual(ostree.target_path("/variable/", "debian"),
                         "/sysroot/variable")

class Commands(avocado.Test):
    def test_sysroot_is_initialised_for_the_stateroot(self):
        g = FakeGuestfs()
        ostree.init_sysroot(g, "main")
        self.assertEqual(g.commands, [
            ["/usr/bin/ostree", "admin", "init-fs", "/sysroot"],
            ["/usr/bin/ostree", "admin", "os-init", "--sysroot=/sysroot", "main"],
        ])

    def test_commit_skips_the_mounts_and_pins_the_time(self):
        g = FakeGuestfs()
        checksum = ostree.commit(g, "debian/amd64", 1700000000)
        self.assertEqual(checksum, "abc123")
        argv = g.commands[0]
        self.assertIn("--tree=dir=/", argv)
        self.assertIn("--timestamp=@1700000000", argv)
        self.assertIn("--repo=/sysroot/ostree/repo", argv)
        skipped = g.written[ostree.SKIP_LIST].decode().split()
        for path in ("/sysroot", "/proc", "/dev", "/sys", "/run"):
            self.assertIn(path, skipped)

    def test_commit_restores_the_skipped_directories(self):
        g = FakeGuestfs()
        ostree.commit(g, "debian/amd64", 0)
        for name in ("sysroot", "dev", "proc", "sys", "run"):
            self.assertIn("%s/%s" % (ostree.SKELETON, name), g.dirs)
        self.assertIn("--tree=dir=%s" % ostree.SKELETON, g.commands[0])

    def test_commit_leaves_nothing_behind_on_the_sysroot(self):
        g = FakeGuestfs()
        ostree.commit(g, "debian/amd64", 0)
        self.assertEqual(sorted(g.removed),
                         sorted([ostree.SKIP_LIST, ostree.SKELETON]))

    def test_deploy_names_stateroot_and_ref(self):
        g = FakeGuestfs()
        ostree.deploy(g, "main", "main/amd64")
        self.assertEqual(g.commands, [
            ["/usr/bin/ostree", "admin", "deploy", "--sysroot=/sysroot",
             "--os=main", "main/amd64"]])

class Unlock(avocado.Test):
    def test_deployment_roots_lose_the_immutable_flag(self):
        g = FakeGuestfs()
        ostree.unlock_deployments(g, "main")
        self.assertEqual(g.unlocked, [
            ("/sysroot/ostree/deploy/main/deploy/abc.0", "i", True)])
