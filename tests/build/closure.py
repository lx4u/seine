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

    def test_a_missing_role_and_a_dynamic_include_are_reported(self):
        build = self.parse("[{roles: [x]}, {ansible.builtin.include_tasks: \"{{ t }}\"}]")
        self.assertEqual(closure.unmodeled([build]), ["include_tasks", "roles"])

    def test_a_computed_lookup_is_reported(self):
        build = self.parse("[{debug: {msg: \"{{ lookup('file', d + 'a') }}\"}}]")
        self.assertEqual(closure.unmodeled([build]), ["lookup"])

    def test_a_fileglob_loop_is_reported(self):
        build = self.parse("[{copy: {src: '{{ item }}', dest: /x}, with_fileglob: [a/*]}]")
        self.assertEqual(closure.unmodeled([build]), ["with_fileglob"])


class StaticReferencesAreFollowed(avocado.Test):
    HEAD = ("distribution:\n    release: trixie\n    architecture: amd64\n"
            "playbook:\n")

    def write(self, name, content=""):
        path = os.path.join(self.workdir, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(content)
        return os.path.realpath(path)

    def build(self, playbook):
        self.write("main.yaml", self.HEAD + playbook)
        build = BuildCmd()
        build.load_all([os.path.join(self.workdir, "main.yaml")])
        build.parse()
        return build

    def files(self, playbook):
        build = self.build(playbook)
        return {os.path.realpath(p) for p in build.image.host_files()}

    def test_included_tasks_are_followed_and_cycles_stop(self):
        a = self.write("tasks/a.yaml", "- include_tasks: b.yaml\n")
        b = self.write("tasks/b.yaml", "- copy: {src: ../files/x, dest: /x}\n"
                       "- include_tasks: a.yaml\n")
        x = self.write("files/x")
        found = self.files("    - tasks:\n        - include_tasks: tasks/a.yaml\n")
        self.assertEqual(found, {a, b, x})

    def test_vars_files_include_vars_script_and_file_lookups(self):
        names = [self.write(n) for n in ("v/a.yaml", "v/b.yaml", "s/run.sh", "f/c")]
        found = self.files(
            "    - vars_files: [v/a.yaml]\n      tasks:\n"
            "        - include_vars: v/b.yaml\n"
            "        - script: s/run.sh --now\n"
            "        - debug: {msg: \"{{ lookup('file', 'f/c') }}\"}\n")
        self.assertEqual(found, set(names))

    def test_a_role_tree_is_taken_whole(self):
        names = [self.write(n) for n in ("roles/web/tasks/main.yaml",
                                         "roles/web/files/index.html")]
        found = self.files("    - roles: [web]\n")
        self.assertEqual(found, set(names))

    def test_template_includes_and_extends_are_followed(self):
        names = [self.write("t/a.j2", "{% extends 'base.j2' %}"),
                 self.write("t/base.j2", "{% include 'part.j2' %}"),
                 self.write("t/part.j2")]
        found = self.files("    - tasks:\n        - template: {src: t/a.j2, dest: /a}\n")
        self.assertEqual(found, set(names))

    def test_uses_lists_files_and_silences_the_dynamic_report(self):
        names = [self.write("c/a.json"), self.write("c/b.json"), self.write("d/e/f")]
        playbook = ("    - uses: ['c/*.json', d/]\n      tasks:\n"
                    "        - copy: {src: '{{ item }}', dest: /x}\n"
                    "          with_fileglob: ['c/*.json']\n")
        build = self.build(playbook)
        self.assertEqual({os.path.realpath(p) for p in build.image.host_files()},
                         set(names))
        self.assertEqual(closure.unmodeled([build]), [])

    def test_uses_without_a_match_is_an_error(self):
        build = self.build("    - uses: [nothing/*]\n      tasks: []\n")
        with self.assertRaises(ValueError):
            build.image.host_files()

    def test_a_path_leaving_the_project_is_an_error(self):
        build = self.build("    - tasks:\n        - copy: {src: .., dest: /x}\n")
        with self.assertRaises(ValueError):
            build.image.host_files()
