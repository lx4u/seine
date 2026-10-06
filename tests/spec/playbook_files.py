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
from seine.build import playbook


# A fragment names its files next to itself, wherever the first spec is.
class PlaybookFilesAreRelativeToTheirFile(avocado.Test):
    HEAD = "distribution:\n    release: trixie\n    architecture: amd64\n"

    def write(self, name, content=""):
        path = os.path.join(self.workdir, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(content)
        return path

    def play(self, fragment, main=""):
        self.write("main.yaml", "requires:\n    - lib/frag\n" + self.HEAD + main)
        self.write("lib/frag.yaml", "playbook:\n" + fragment)
        build = BuildCmd()
        build.load_all([os.path.join(self.workdir, "main.yaml")])
        build.parse()
        return build.spec["playbook"]

    def test_src_is_found_next_to_the_fragment(self):
        self.write("lib/files/motd")
        play = self.play("    - copy:\n        src: files/motd\n        dest: /etc/motd\n")
        self.assertEqual(play[0]["copy"]["src"],
                         os.path.join(self.workdir, "lib", "files", "motd"))

    def test_other_file_references_are_found_too(self):
        for name in ("tasks.yaml", "vars.yaml", "run.sh", "t.j2"):
            self.write("lib/" + name)
        play = self.play("    - tasks:\n"
                         "        - include_tasks: tasks.yaml\n"
                         "        - template: src=t.j2 dest=/etc/t\n"
                         "        - script: run.sh --fast\n"
                         "        - include_vars: {file: vars.yaml}\n"
                         "      vars_files: [vars.yaml]\n")
        tasks = play[0]["tasks"]
        here = os.path.join(self.workdir, "lib") + os.sep
        self.assertEqual(tasks[0]["include_tasks"], here + "tasks.yaml")
        self.assertEqual(tasks[1]["template"], "src=%st.j2 dest=/etc/t" % here)
        self.assertEqual(tasks[2]["script"], here + "run.sh --fast")
        self.assertEqual(tasks[3]["include_vars"]["file"], here + "vars.yaml")
        self.assertEqual(play[0]["vars_files"], [here + "vars.yaml"])

    def test_a_name_that_is_not_there_stays_as_written(self):
        play = self.play("    - copy:\n        src: nowhere\n        dest: /x\n")
        self.assertEqual(play[0]["copy"]["src"], "nowhere")

    def test_a_computed_name_and_the_target_of_a_link_stay(self):
        self.write("lib/motd")
        play = self.play("    - copy: {src: '{{ item }}', dest: /x}\n"
                         "    - file: {src: motd, dest: /y, state: link}\n")
        self.assertEqual(play[0]["copy"]["src"], "{{ item }}")
        self.assertEqual(play[1]["file"]["src"], "motd")

    def test_the_first_spec_still_names_its_own_files(self):
        self.write("files/motd")
        self.write("main.yaml", self.HEAD +
                   "playbook:\n    - copy:\n        src: files/motd\n        dest: /x\n")
        build = BuildCmd()
        build.load_all([os.path.join(self.workdir, "main.yaml")])
        build.parse()
        self.assertEqual(build.spec["playbook"][0]["copy"]["src"],
                         os.path.join(self.workdir, "files", "motd"))

    def test_the_digest_follows_the_file_a_fragment_names(self):
        path = self.write("lib/files/motd", "one")
        play = "    - copy:\n        src: files/motd\n        dest: /x\n"
        self.play(play)
        build = BuildCmd()
        build.load_all([os.path.join(self.workdir, "main.yaml")])
        build.parse()
        self.assertEqual([os.path.realpath(p) for p in build.image.host_files()],
                         [os.path.realpath(path)])

    def test_resolve_leaves_its_input_alone(self):
        tasks = [{"copy": {"src": "a", "dest": "/a"}}]
        out = playbook.resolve(tasks, lambda name: "/abs/" + name)
        self.assertEqual(out[0]["copy"]["src"], "/abs/a")
        self.assertEqual(tasks[0]["copy"]["src"], "a")
