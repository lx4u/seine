#!/usr/bin/env python3
# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import avocado
import os
import sys

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine.extends import parsing

class Package:
    name = "foo"
    source = None
    source_name = None

    def _error(self, message):
        return ValueError(message)

def refused(test, function, *args, **kwargs):
    with test.assertRaises(ValueError) as error:
        function(Package(), "go", *args, **kwargs)
    return str(error.exception)

class Strings(avocado.Test):
    def test_a_required_string_must_be_there(self):
        self.assertIn("'extends: go: x' shall be a non-empty string",
                      refused(self, parsing.parse_string, {}, "x"))
        self.assertIn("non-empty", refused(self, parsing.parse_string,
                                           {"x": ""}, "x"))

    def test_a_default_makes_it_optional_and_allows_empty(self):
        self.assertEqual(
            parsing.parse_string(Package(), "go", {}, "x", default="."), ".")
        self.assertEqual(
            parsing.parse_string(Package(), "go", {"x": ""}, "x", default=""), "")

    def test_it_must_be_a_string(self):
        self.assertIn("shall be a string", refused(
            self, parsing.parse_string, {"x": 1}, "x", default=""))

    def test_the_hint_is_shown(self):
        self.assertIn("the Go version", refused(
            self, parsing.parse_string, {}, "x", hint="the Go version"))

    def test_shell_safety(self):
        for bad in ["a;b", "a`b", "a$(b)", "a&b", "a|b", "a\nb"]:
            self.assertIn("not allowed", refused(
                self, parsing.parse_string, {"x": bad}, "x", default="",
                shell_safe=True))
        self.assertEqual(parsing.parse_string(
            Package(), "go", {"x": "-s -w"}, "x", default="",
            shell_safe=True), "-s -w")

class Booleans(avocado.Test):
    def test_values(self):
        self.assertEqual(parsing.parse_bool(Package(), "go", {}, "x"), False)
        self.assertEqual(
            parsing.parse_bool(Package(), "go", {"x": True}, "x"), True)
        self.assertIn("true or false",
                      refused(self, parsing.parse_bool, {"x": "yes"}, "x"))

class Lists(avocado.Test):
    def test_a_list_of_strings(self):
        self.assertEqual(
            parsing.parse_string_list(Package(), "go", {}, "x"), [])
        self.assertEqual(parsing.parse_string_list(
            Package(), "go", {"x": ["a"]}, "x"), ["a"])
        for bad in ["a", [1], {"a": 1}]:
            self.assertIn("list of strings", refused(
                self, parsing.parse_string_list, {"x": bad}, "x"))

    def test_relationships_are_one_per_entry(self):
        self.assertEqual(parsing.parse_relationships(
            Package(), "go", {"x": ["a (>= 1)"]}, "x"), ["a (>= 1)"])
        self.assertIn("one per entry", refused(
            self, parsing.parse_relationships, {"x": ["a\nb"]}, "x"))

class VaultKeys(avocado.Test):
    def test_the_name_is_returned(self):
        self.assertEqual(parsing.parse_vault_key(
            Package(), "go", {"k": "vault:sign"}, "k"), "sign")

    def test_unset_is_none(self):
        self.assertIsNone(parsing.parse_vault_key(Package(), "go", {}, "k"))

    def test_bad_values(self):
        for bad in ["sign", "vault:", 1]:
            self.assertIn("'extends: go: k' shall be 'vault:<name>'", refused(
                self, parsing.parse_vault_key, {"k": bad}, "k"))

class GeneratedSource(avocado.Test):
    def test_a_source_is_refused(self):
        package = Package()
        package.source = "git://x;rev=1"
        with self.assertRaises(ValueError):
            parsing.require_generated_source(package, "uki")

    def test_the_name_is_the_source_name(self):
        package = Package()
        parsing.require_generated_source(package, "uki")
        self.assertEqual(package.source_name, "foo")

if __name__ == "__main__":
    avocado.main()
