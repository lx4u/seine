#!/usr/bin/env python3
# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import avocado
import jinja2
import os
import stat
import sys
import types

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine.extends import templates

class TemplatesUseSquareBrackets(avocado.Test):
    def test_delimiters(self):
        text = "[% if x %]\na [[ x ]] {{ y }}\n[% endif %]\n"
        self.assertEqual(
            templates.TEMPLATE.from_string(text).render({"x": 1}),
            "a 1 {{ y }}\n")

    def test_a_missing_value_is_an_error(self):
        with self.assertRaises(jinja2.UndefinedError):
            templates.TEMPLATE.from_string("[[ nope ]]").render({})

class TemplatesAreLoadedFromData(avocado.Test):
    def test_files_and_content_agree(self):
        found, content = templates.load_templates("module")
        self.assertEqual(sorted(found), sorted(templates.FILES))
        joined = b""
        for name in templates.FILES:
            with open(os.path.join(templates.DATA, "module", name), "rb") as f:
                joined += f.read()
        self.assertEqual(content, joined)

    def test_other_files_can_be_named(self):
        found, _ = templates.load_templates("uefi-keys", ("service",))
        self.assertEqual(list(found), ["service"])

class DebianIsReset(avocado.Test):
    def test_what_the_tree_came_with_is_replaced(self):
        old = os.path.join(self.workdir, "debian")
        os.makedirs(old)
        with open(os.path.join(old, "control"), "w") as f:
            f.write("old")
        debian = templates.reset_debian(self.workdir)
        self.assertEqual(debian, old)
        self.assertEqual(sorted(os.listdir(debian)), ["source"])
        with open(os.path.join(debian, "source", "format")) as f:
            self.assertEqual(f.read(), "3.0 (native)\n")

class FilesAreRendered(avocado.Test):
    def test_rules_is_executable_and_names_can_change(self):
        debian = templates.reset_debian(self.workdir)
        templates.render_files(
            debian, {"rules": "a [[ x ]]\n", "service": "b [[ x ]]\n"},
            {"x": 1}, names={"service": "foo.service"})
        with open(os.path.join(debian, "foo.service")) as f:
            self.assertEqual(f.read(), "b 1\n")
        mode = os.stat(os.path.join(debian, "rules")).st_mode
        self.assertTrue(mode & stat.S_IXUSR)
        self.assertFalse(os.path.exists(os.path.join(debian, "service")))

class ContextHasWhatEveryChangelogNeeds(avocado.Test):
    def test_fields(self):
        package = types.SimpleNamespace(name="foo", upstream_version="1.2")
        context = templates.base_context(package, 946684800)
        self.assertEqual(context["name"], "foo")
        self.assertEqual(context["version"], "1.2")
        self.assertEqual(context["date"], "Sat, 01 Jan 2000 00:00:00 +0000")

class ShellQuoting(avocado.Test):
    def test_a_quote_is_closed_and_reopened(self):
        self.assertEqual(templates.sh_quote("it's"), "'it'\\''s'")

if __name__ == "__main__":
    avocado.main()
