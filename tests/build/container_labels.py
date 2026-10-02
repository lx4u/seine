# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import os
import sys
from unittest import mock

import avocado

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine.container import ContainerEngine


# seine-agent sets SEINE_BUILD_ID so the containers of a cancelled build
# can be found, by this label, and removed.
class ContainersCarryTheBuildId(avocado.Test):
    def command(self, cmd, build_id="bld-7"):
        env = {} if build_id is None else {"SEINE_BUILD_ID": build_id}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(ContainerEngine, "root", return_value="/r"), \
                mock.patch.object(ContainerEngine, "runroot", return_value="/r/run"):
            if build_id is None:
                os.environ.pop("SEINE_BUILD_ID", None)
            return ContainerEngine._podman_cmd(cmd)

    def label(self, *verb):
        return ["podman", "--root", "/r", "--runroot", "/r/run", *verb,
                "--label", "seine.build_id=bld-7"]

    def test_run_create_and_build_are_labelled(self):
        self.assertEqual(self.command(["run", "--rm", "img"]),
                         self.label("run") + ["--rm", "img"])
        self.assertEqual(self.command(["create", "--name", "c", "img"]),
                         self.label("create") + ["--name", "c", "img"])
        self.assertEqual(self.command(["build", "-t", "img", "."]),
                         self.label("build") + ["-t", "img", "."])

    def test_container_subcommands_are_labelled(self):
        self.assertEqual(self.command(["container", "create", "img"]),
                         self.label("container", "create") + ["img"])
        self.assertEqual(self.command(["container", "run", "img"]),
                         self.label("container", "run") + ["img"])

    def test_other_commands_are_left_alone(self):
        for cmd in (["container", "exec", "c", "ls"], ["ps", "-a"], ["rm", "-f", "c"],
                    ["image", "exists", "img"], ["container", "export", "c"]):
            self.assertEqual(self.command(list(cmd)),
                             ["podman", "--root", "/r", "--runroot", "/r/run"] + cmd)

    def test_nothing_is_added_without_a_build_id(self):
        self.assertEqual(self.command(["run", "img"], build_id=None),
                         ["podman", "--root", "/r", "--runroot", "/r/run", "run", "img"])
