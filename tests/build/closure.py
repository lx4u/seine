#!/usr/bin/env python3
# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import avocado
import os
import sys

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine.build import BuildCmd
from seine.build import closure

class ClosureNamesEveryLocalInput(avocado.Test):
    def write(self, name, content=""):
        path = os.path.join(self.workdir, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(content)
        return os.path.realpath(path)

    def parse(self, name):
        build = BuildCmd()
        build.load_all([os.path.join(self.workdir, name)])
        build.parse()
        return build

    def test_specs_locks_and_fragments(self):
        main = self.write("main.yaml", "requires:\n    - frag\n"
                          "distribution:\n    release: trixie\n"
                          "    architecture: amd64\nplaybook: []\n")
        frag = self.write("frag.yaml", "variables: {}\n")
        lock = self.write("main.lock.yaml", "variables: {}\n")
        found = closure.collect([self.parse("main.yaml")])
        self.assertTrue({main, frag, lock} <= found)

    def test_ansible_library_and_host_files(self):
        module = self.write("library/mod.py")
        src = self.write("files/motd")
        self.write("main.yaml", "distribution:\n    release: trixie\n"
                   "    architecture: amd64\n"
                   "playbook:\n    - copy:\n        src: files/motd\n")
        found = closure.collect([self.parse("main.yaml")])
        self.assertIn(src, found)
        self.assertIn(os.path.dirname(module), found)

    def test_package_patches_and_local_source_tree(self):
        patch = self.write("patches/0001.patch")
        tree = os.path.dirname(self.write("pkg/debian/control"))
        self.write("main.yaml", "distribution:\n    release: trixie\n"
                   "    architecture: amd64\npackages:\n"
                   "    - source: file://pkg\n      patches:\n"
                   "        - patches/0001.patch\n")
        found = closure.collect([self.parse("main.yaml")])
        self.assertIn(patch, found)
        self.assertIn(os.path.dirname(tree), found)

    def test_a_multiconfig_group_is_followed(self):
        other = self.write("other.yaml", "distribution:\n    release: trixie\n"
                           "    architecture: amd64\nplaybook: []\n")
        self.write("main.yaml", "distribution:\n    release: trixie\n"
                   "    architecture: amd64\nplaybook: []\n"
                   "multiconfig:\n    sub:\n        - other.yaml\n")
        found = closure.collect([self.parse("main.yaml")])
        self.assertIn(other, found)

    def test_a_container_archive_is_listed(self):
        archive = self.write("images/app.tar")
        self.write("main.yaml", "distribution:\n    release: trixie\n"
                   "    architecture: amd64\nplaybook: []\n"
                   "containers:\n    - file: images/app.tar\n")
        found = closure.collect([self.parse("main.yaml")])
        self.assertIn(archive, found)

    def test_a_symlinked_patch_keeps_its_name(self):
        target = self.write("shared/a.patch")
        link = os.path.join(self.workdir, "patches", "a.patch")
        os.makedirs(os.path.dirname(link))
        os.symlink(target, link)
        self.write("main.yaml", "distribution:\n    release: trixie\n"
                   "    architecture: amd64\npackages:\n"
                   "    - source: file://pkg\n      patches:\n"
                   "        - patches/a.patch\n")
        found = closure.collect([self.parse("main.yaml")])
        self.assertIn(os.path.abspath(link), found)


class UnmodeledKeysAreReported(avocado.Test):
    def parse(self, playbook):
        build = BuildCmd()
        build.loads("distribution:\n    release: trixie\n"
                    "    architecture: amd64\nplaybook: %s\n" % playbook)
        build.parse()
        return build

    def test_a_plain_playbook_has_none(self):
        build = self.parse("[{copy: {src: a, dest: /a}}]")
        self.assertEqual(closure.unmodeled([build]), [])

    def test_roles_and_includes_are_reported(self):
        build = self.parse("[{roles: [x]}, {ansible.builtin.include_tasks: t.yaml}]")
        self.assertEqual(closure.unmodeled([build]), ["include_tasks", "roles"])

    def test_a_lookup_is_reported(self):
        build = self.parse("[{debug: {msg: \"{{ lookup('file', 'a') }}\"}}]")
        self.assertEqual(closure.unmodeled([build]), ["lookup"])
