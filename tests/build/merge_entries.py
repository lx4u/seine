#!/usr/bin/env python3

import avocado
import os
import sys

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine.build import BuildCmd

class SettingsRememberWhichFileWroteThem(avocado.Test):
    def test(self):
        # An overlay of the shape the examples use: the suite file pins
        # the packaging, the kernel fragment says which tree to graft on,
        # the architecture file says which flavour under 'defaults'. The
        # file to open to change any one of them is a different file.
        build = BuildCmd()
        for name, text in [
                ("suite.yml", """
                    requires:
                        - kernel
                    packages:
                        - source: apt://linux=6.12.101-1
                """),
                ("kernel.yml", """
                    packages:
                        - source: apt://linux
                          extends:
                              kernel:
                                  upstream: https://kernel.org/linux-6.18.43.tar.xz
                """),
                ("arch.yml", """
                    defaults:
                        packages:
                            - source: apt://linux
                              extends:
                                  kernel:
                                      flavour: amd64
                """)]:
            path = os.path.join(self.workdir, name)
            with open(path, "w") as f:
                f.write(text)
        build.load(os.path.join(self.workdir, "suite.yml"))
        build.load(os.path.join(self.workdir, "arch.yml"))
        build.loads("""
                image:
                    filename: t.img
                    partitions:
                        - label: rootfs
                          where: /
        """)
        build.parse()

        package = build.image.packages[0]
        self.assertEqual(os.path.basename(package.origin_of("source")),
                         "suite.yml")
        self.assertEqual(
            os.path.basename(package.origin_of("extends.kernel.upstream")),
            "kernel.yml")
        self.assertEqual(
            os.path.basename(package.origin_of("extends.kernel.flavour")),
            "arch.yml")
        # Nothing wrote this one down.
        self.assertEqual(package.origin_of("extends.kernel.sha256"), None)

class TheSamePlaybookEntryReachedTwiceIsNotDuplicated(avocado.Test):
    # A fragment reached via two 'requires:' paths used to duplicate
    # its own playbook entry once per path.
    def test(self):
        build = BuildCmd()
        build.loads("""
                playbook:
                    - name: configure user accounts
                      priority: 900
                      tasks:
                          - name: set root password
                            user: name=root password=secret
        """)
        build.loads("""
                playbook:
                    - name: configure user accounts
                      priority: 900
                      tasks:
                          - name: set root password
                            user: name=root password=secret
        """)
        self.assertEqual(len(build.spec["playbook"]), 1)
        self.assertEqual(len(build.spec["playbook"][0]["tasks"]), 1)

class PlaybookEntryTasksAreAddedToRatherThanSettled(avocado.Test):
    # 'tasks:' stays one additive, order-preserving list -- not merged
    # task-by-task -- since ansible tasks run in sequence and folding
    # two same-named tasks into one would be a bigger behavior change
    # than it is for an independent test case.
    def test(self):
        build = BuildCmd()
        build.loads("""
                playbook:
                    - name: configure user accounts
                      tasks:
                          - name: set root password
                            user: name=root password=one
        """)
        build.loads("""
                playbook:
                    - name: configure user accounts
                      tasks:
                          - name: add a second user
                            user: name=alice
        """)
        tasks = build.spec["playbook"][0]["tasks"]
        self.assertEqual([t["name"] for t in tasks],
                         ["set root password", "add a second user"])

class TheSameTestEntryReachedTwiceIsNotDuplicated(avocado.Test):
    # The bug this whole block guards against: a shared fragment's own
    # 'test:' entry, reached via two 'requires:' paths (so loaded
    # twice), used to show up as two -- or, with a third path, three --
    # duplicate entries instead of one.
    def test(self):
        build = BuildCmd()
        build.loads("""
                test:
                    - name: shared login
                      keywords:
                          - name: Log In
                            steps: ["Log Message  hi"]
        """)
        build.loads("""
                test:
                    - name: shared login
                      keywords:
                          - name: Log In
                            steps: ["Log Message  hi"]
        """)
        self.assertEqual(len(build.spec["test"]), 1)

class TestEntriesAreAmendedByName(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.loads("""
                test:
                    - name: boot
                      keywords:
                          - name: Log In
                            steps: ["Log Message  hi"]
        """)
        build.loads("""
                test:
                    - name: boot
                      tags: [smoke]
                      setup:
                          connect_target: {}
        """)
        entry = build.spec["test"][0]
        self.assertEqual(entry["tags"], ["smoke"])
        self.assertEqual(entry["setup"], {"connect_target": {}})

class ATestEntrysExistingFieldWins(avocado.Test):
    # A board's own 'boot' test entry stands even if a fragment it
    # requires also names an entry called 'boot' -- a real 'requires:'
    # chain, not two peer files.
    def test(self):
        build = BuildCmd()
        with open(os.path.join(self.workdir, "fragment.yml"), "w") as f:
            f.write("""
                test:
                    - name: boot
                      setup:
                          connect_target: {label: secondary}
            """)
        board = os.path.join(self.workdir, "board.yml")
        with open(board, "w") as f:
            f.write("""
                requires:
                    - fragment
                test:
                    - name: boot
                      setup:
                          connect_target: {label: primary}
            """)
        build.load(board)
        self.assertEqual(build.spec["test"][0]["setup"],
                         {"connect_target": {"label": "primary"}})

class ConflictingKeywordsOfTheSameNameAreRefused(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.loads("""
                test:
                    - name: boot
                      keywords:
                          - name: Log In
                            steps: ["Log Message  one"]
        """)
        self.assertRaises(ValueError, build.loads, """
                test:
                    - name: boot
                      keywords:
                          - name: Log In
                            steps: ["Log Message  two"]
        """)

class TestCasesAreAmendedByName(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.loads("""
                test:
                    - name: boot
                      tests:
                          - name: boots up
                            steps: ["Log Message  hi"]
        """)
        build.loads("""
                test:
                    - name: boot
                      tests:
                          - name: boots up
                            tags: [smoke]
        """)
        cases = build.spec["test"][0]["tests"]
        self.assertEqual(len(cases), 1)
        self.assertEqual(cases[0]["tags"], ["smoke"])
        self.assertEqual(cases[0]["steps"], ["Log Message  hi"])

class ConflictingTestCaseStepsAreRefused(avocado.Test):
    # Unlike an entry's own scalar settings (first-loaded wins), a case
    # name colliding with genuinely different 'steps:' is more likely an
    # authoring accident than deliberate composition, so it is refused
    # rather than silently keeping the first-loaded steps.
    def test(self):
        build = BuildCmd()
        build.loads("""
                test:
                    - name: boot
                      tests:
                          - name: boots up
                            steps: ["Log Message  one"]
        """)
        self.assertRaises(ValueError, build.loads, """
                test:
                    - name: boot
                      tests:
                          - name: boots up
                            steps: ["Log Message  two"]
        """)

class TestEntryLibraryAndTagsAreAddedToRatherThanSettled(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.loads("""
                test:
                    - name: boot
                      tags: [smoke]
                      library: [my.pkg.LibraryOne]
        """)
        build.loads("""
                test:
                    - name: boot
                      tags: [regression]
                      library: [my.pkg.LibraryTwo]
        """)
        entry = build.spec["test"][0]
        self.assertEqual(entry["tags"], ["smoke", "regression"])
        self.assertEqual(entry["library"],
                         ["my.pkg.LibraryOne", "my.pkg.LibraryTwo"])

class TestEntryVariablesAreAddedToRatherThanSettled(avocado.Test):
    # A regression test for a real pitfall found while designing this:
    # 'variables' is a flat name -> scalar dict, not nested like
    # 'derived-flavours', and must not be routed through _added()'s
    # dict-merge branch, which assumes a nested dict-of-dicts shape and
    # would raise on a flat one.
    def test(self):
        build = BuildCmd()
        build.loads("""
                test:
                    - name: boot
                      variables: {TIMEOUT: "30s"}
        """)
        build.loads("""
                test:
                    - name: boot
                      variables: {TIMEOUT: "60s", RETRIES: "3"}
        """)
        self.assertEqual(build.spec["test"][0]["variables"],
                         {"TIMEOUT": "60s", "RETRIES": "3"})

class TestEntriesWithDifferentNamesAreNotMerged(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.loads("""
                test:
                    - name: boot
                      tags: [smoke]
        """)
        build.loads("""
                test:
                    - name: rebuild-busybox
                      tags: [smoke]
        """)
        self.assertEqual([t["name"] for t in build.spec["test"]],
                         ["boot", "rebuild-busybox"])
