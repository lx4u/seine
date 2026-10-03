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

class ImpureLookupsAreBlocked(avocado.Test):
    def parse(self, playbook):
        build = BuildCmd()
        build.loads("distribution:\n    release: trixie\n"
                    "    architecture: amd64\nplaybook: %s\n" % playbook)
        return build.parse()

    def test_pipe_env_and_url_are_refused(self):
        for plugin in ("pipe", "env", "url", "ansible.builtin.env"):
            with self.assertRaises(ValueError) as caught:
                self.parse("[{debug: {msg: \"{{ lookup('%s', 'x') }}\"}}]" % plugin)
            self.assertIn("impure lookups are blocked", str(caught.exception))

    def test_a_nested_list_value_is_checked(self):
        with self.assertRaises(ValueError):
            self.parse("[{tasks: [{debug: {msg: [\"{{ lookup(\\\"pipe\\\", 'id') }}\"]}}]}]")

    def test_a_file_lookup_is_allowed(self):
        self.parse("[{debug: {msg: \"{{ lookup('file', 'a') }}\"}}]")
