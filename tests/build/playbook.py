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


class PlaybookWaves(avocado.Test):
    def parse(self, playbook_yaml):
        build = BuildCmd()
        build.loads("distribution:\n    release: trixie\n"
                    "    architecture: amd64\nplaybook:\n" + playbook_yaml)
        build.parse()
        return build.spec["playbook"]

    def test_default_wave_keeps_old_order_without_priority(self):
        playbooks = self.parse("""
    - name: first
    - name: second
    - name: third
""")
        self.assertEqual([p["name"] for p in playbooks],
                         ["first", "second", "third"])

    def test_default_wave_ordered_by_priority(self):
        playbooks = self.parse("""
    - name: first
      priority: 900
    - name: second
      priority: 100
""")
        self.assertEqual([p["name"] for p in playbooks],
                         ["second", "first"])

    def test_priority_inside_wave(self):
        playbooks = self.parse("""
    - name: app high
      wave: app
      priority: 800
      after: [accounts]
    - name: accounts high
      wave: accounts
      priority: 800
    - name: app low
      wave: app
      priority: 100
    - name: accounts low
      wave: accounts
      priority: 100
""")
        self.assertEqual([p["name"] for p in playbooks],
                         ["accounts low", "accounts high", "app low", "app high"])

    def test_wave_order_by_after_and_before(self):
        playbooks = self.parse("""
    - name: middle
      wave: middle
      after: first
    - name: last
      wave: last
      after: [middle]
    - name: first
      wave: first
""")
        self.assertEqual([p["name"] for p in playbooks],
                         ["first", "middle", "last"])

        playbooks = self.parse("""
    - name: second
      wave: second
    - name: first
      wave: first
      before: [second]
""")
        self.assertEqual([p["name"] for p in playbooks],
                         ["first", "second"])

    def test_union_of_edges(self):
        playbooks = self.parse("""
    - name: app 1
      wave: app
      after: [accounts]
    - name: app 2
      wave: app
      after: [storage]
    - name: accounts
      wave: accounts
    - name: storage
      wave: storage
""")
        names = [p["name"] for p in playbooks]
        self.assertLess(names.index("accounts"), names.index("app 1"))
        self.assertLess(names.index("storage"), names.index("app 1"))

    def test_unknown_wave_is_an_error(self):
        for key in ("after", "before"):
            with self.assertRaises(ValueError) as caught:
                self.parse(f"""
    - name: my play
      {key}: [missing]
""")
            self.assertIn("my play", str(caught.exception))
            self.assertIn("which no wave in this specification defines", str(caught.exception))

    def test_self_edge_is_an_error(self):
        for key in ("after", "before"):
            with self.assertRaises(ValueError) as caught:
                self.parse(f"""
    - name: my play
      wave: accounts
      {key}: accounts
""")
            self.assertIn("my play", str(caught.exception))
            self.assertIn("names the wave itself", str(caught.exception))

    def test_circle_is_an_error(self):
        with self.assertRaises(ValueError) as caught:
            self.parse("""
    - name: play one
      wave: w1
      after: [w2]
    - name: play two
      wave: w2
      after: [w1]
""")
        self.assertIn("circle", str(caught.exception))
        self.assertIn("play one", str(caught.exception))
        self.assertIn("play two", str(caught.exception))

    def test_seine_only_keys_are_popped(self):
        playbooks = self.parse("""
    - name: play
      wave: accounts
      priority: 200
      after: [base]
    - name: base play
      wave: base
""")
        for play in playbooks:
            for key in ("wave", "after", "before", "priority"):
                self.assertNotIn(key, play)

    def test_file_order_breaks_ties(self):
        playbooks = self.parse("""
    - name: first play
      wave: test
    - name: second play
      wave: test
""")
        self.assertEqual([p["name"] for p in playbooks],
                         ["first play", "second play"])

        playbooks = self.parse("""
    - name: wave a play
      wave: wa
    - name: wave b play
      wave: wb
""")
        self.assertEqual([p["name"] for p in playbooks],
                         ["wave a play", "wave b play"])
