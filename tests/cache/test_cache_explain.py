#!/usr/bin/env python3
# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import io
import os
import sys
from unittest import mock
import avocado

path_to_self = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.insert(0, path_to_sources)

from seine import utils
from seine.cache import CacheCmd


class RecipeDiffUtilsTest(avocado.Test):
    def test_diff_recipes_identical(self):
        r1 = [("revision", "1"), ("spec", "abc")]
        r2 = [("revision", "1"), ("spec", "abc")]
        self.assertEqual(utils.diff_recipes(r1, r2), [])

    def test_diff_recipes_changed_and_added_and_removed(self):
        old = [("revision", "1"), ("source", "https://pkg.org"), ("sha256", "111")]
        new = [("revision", "2"), ("source", "https://pkg.org"), ("distro_release", "trixie")]
        diff = utils.diff_recipes(old, new)
        self.assertIn("debian revision changed", diff)
        self.assertIn("distribution release is new", diff)
        self.assertIn("source sha256 checksum no longer applies", diff)

    def test_diff_recipes_files_and_dependencies(self):
        old = [("file:patch.diff", "hash1"), ("depends:libfoo", "dep1")]
        new = [("file:patch.diff", "hash2"), ("depends:libbar", "dep2")]
        diff = utils.diff_recipes(old, new)
        self.assertIn("file 'patch.diff' changed", diff)
        self.assertIn("new dependency 'libbar'", diff)
        self.assertIn("dependency 'libfoo' no longer applies", diff)

    def test_format_recipe_label(self):
        self.assertEqual(utils.format_recipe_label("revision"), "debian revision")
        self.assertEqual(utils.format_recipe_label("file:etc/hosts"), "file 'etc/hosts'")
        self.assertEqual(utils.format_recipe_label("depends:python3"), "dependency 'python3'")
        self.assertEqual(utils.format_recipe_label("unknown_custom_key"), "unknown_custom_key")


class CacheExplainCliTest(avocado.Test):
    def setUp(self):
        self.cmd = CacheCmd()

    def test_explain_two_recipe_files_identical(self):
        f1 = os.path.join(self.workdir, "a.recipe")
        f2 = os.path.join(self.workdir, "b.recipe")
        utils.write_recipe_file(f1, [("rev", "1"), ("spec", "hash")])
        utils.write_recipe_file(f2, [("rev", "1"), ("spec", "hash")])

        with mock.patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            rc = self.cmd.explain([f1, f2])
            self.assertEqual(rc, 0)
            self.assertIn("are identical", mock_out.getvalue())

    def test_explain_two_recipe_files_diff(self):
        f1 = os.path.join(self.workdir, "old.recipe")
        f2 = os.path.join(self.workdir, "new.recipe")
        utils.write_recipe_file(f1, [("revision", "1")])
        utils.write_recipe_file(f2, [("revision", "2"), ("file:test.patch", "h")])

        with mock.patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            rc = self.cmd.explain([f1, f2])
            self.assertEqual(rc, 0)
            out = mock_out.getvalue()
            self.assertIn("debian revision changed", out)
            self.assertIn("new file 'test.patch'", out)

    def test_explain_single_recipe_file(self):
        f = os.path.join(self.workdir, "single.recipe")
        utils.write_recipe_file(f, [("revision", "3"), ("architecture", "amd64")])

        with mock.patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            rc = self.cmd.explain([f])
            self.assertEqual(rc, 0)
            out = mock_out.getvalue()
            self.assertIn("debian revision", out)
            self.assertIn("target architecture", out)
            self.assertIn("amd64", out)

    def test_explain_diff_remote_vs_local(self):
        remote_recipe = [("revision", "1"), ("file:fix.patch", "h1")]
        local_recipe = [("revision", "2"), ("file:fix.patch", "h2")]

        with mock.patch("seine.storage.for_build") as mock_storage, \
             mock.patch("seine.storage.local.LocalStorageProvider.explain") as mock_local, \
             mock.patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            mock_storage.return_value.explain.return_value = remote_recipe
            mock_local.return_value = local_recipe

            rc = self.cmd.explain(["packages", "trixie/amd64/pkg"])
            self.assertEqual(rc, 0)
            out = mock_out.getvalue()
            self.assertIn("packages/trixie/amd64/pkg: not cached --", out)
            self.assertIn("debian revision changed", out)
            self.assertIn("file 'fix.patch' changed", out)

    def test_explain_cache_hit_reported(self):
        recipe = [("revision", "1"), ("spec", "same")]

        with mock.patch("seine.storage.for_build") as mock_storage, \
             mock.patch("seine.storage.local.LocalStorageProvider.explain") as mock_local, \
             mock.patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            mock_storage.return_value.explain.return_value = recipe
            mock_local.return_value = recipe

            rc = self.cmd.explain(["packages/trixie/amd64/pkg"])
            self.assertEqual(rc, 0)
            self.assertIn("packages/trixie/amd64/pkg: cached (matches remote recipe)", mock_out.getvalue())

    def test_explain_remote_only(self):
        remote_recipe = [("revision", "1"), ("source", "git://upstream")]

        with mock.patch("seine.storage.for_build") as mock_storage, \
             mock.patch("seine.storage.local.LocalStorageProvider.explain") as mock_local, \
             mock.patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            mock_storage.return_value.explain.return_value = remote_recipe
            mock_local.return_value = None

            rc = self.cmd.explain(["packages", "trixie/amd64/pkg"])
            self.assertEqual(rc, 0)
            out = mock_out.getvalue()
            self.assertIn("remote recipe for packages/trixie/amd64/pkg:", out)
            self.assertIn("debian revision", out)

    def test_explain_missing_recipe_exits_nonzero(self):
        with mock.patch("seine.storage.for_build") as mock_storage, \
             mock.patch("seine.storage.local.LocalStorageProvider.explain") as mock_local, \
             mock.patch("sys.stderr", new_callable=io.StringIO) as mock_err:
            mock_storage.return_value.explain.return_value = None
            mock_local.return_value = None

            rc = self.cmd.explain(["packages", "missing/item"])
            self.assertEqual(rc, 1)
            self.assertIn("no recipe found", mock_err.getvalue())
