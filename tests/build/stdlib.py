#!/usr/bin/env python3

import avocado
import contextlib
import io
import os
import sys

path_to_self = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine import stdlib
from seine.build import BuildCmd
from seine.tui.context import Context


class TestSearchPaths(avocado.Test):
    def setUp(self):
        super().setUp()
        self._orig_env = os.environ.get("SEINE_STDLIB_DIR")

    def tearDown(self):
        super().tearDown()
        if self._orig_env is not None:
            os.environ["SEINE_STDLIB_DIR"] = self._orig_env
        else:
            os.environ.pop("SEINE_STDLIB_DIR", None)

    def test_default_hierarchy_contains_expected_locations(self):
        paths = stdlib.search_paths()
        repo_root = os.path.dirname(os.path.dirname(os.path.dirname(path_to_self)))
        expected_repo = os.path.abspath(os.path.join(repo_root, "stdlib"))
        self.assertIn(expected_repo, paths)
        self.assertIn(stdlib.SYSTEM_STDLIB_DIR, paths)

    def test_environment_override_is_prioritized(self):
        custom_dir = os.path.join(self.workdir, "custom-stdlib")
        os.makedirs(custom_dir, exist_ok=True)
        os.environ["SEINE_STDLIB_DIR"] = custom_dir

        paths = stdlib.search_paths()
        self.assertEqual(paths[0], custom_dir)

    def test_staged_worktree_takes_precedence_over_environment(self):
        custom_dir = os.path.join(self.workdir, "env-stdlib")
        os.makedirs(custom_dir, exist_ok=True)
        os.environ["SEINE_STDLIB_DIR"] = custom_dir

        project_dir = os.path.join(self.workdir, "project")
        staged_dir = os.path.join(project_dir, ".seine-stdlib")
        os.makedirs(staged_dir, exist_ok=True)

        paths = stdlib.search_paths(project_root=project_dir)
        self.assertEqual(paths[0], staged_dir)
        self.assertEqual(paths[1], custom_dir)


class TestResolve(avocado.Test):
    def setUp(self):
        super().setUp()
        self._orig_env = os.environ.get("SEINE_STDLIB_DIR")
        self.stdlib_dir = os.path.join(self.workdir, "stdlib")
        os.makedirs(self.stdlib_dir, exist_ok=True)
        os.environ["SEINE_STDLIB_DIR"] = self.stdlib_dir

    def tearDown(self):
        super().tearDown()
        if self._orig_env is not None:
            os.environ["SEINE_STDLIB_DIR"] = self._orig_env
        else:
            os.environ.pop("SEINE_STDLIB_DIR", None)

    def write_spec(self, directory, relpath, content):
        full = os.path.join(directory, relpath)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            f.write(content)
        return full

    def test_non_stdlib_reference_is_left_alone(self):
        self.assertEqual(stdlib.resolve("regular.yaml"), "regular.yaml")
        self.assertEqual(stdlib.resolve(123), 123)

    def test_explicit_yml_and_yaml_extensions(self):
        yml_path = self.write_spec(self.stdlib_dir, "debian/amd64.yml", "architecture: amd64\n")
        yaml_path = self.write_spec(self.stdlib_dir, "debian/arm64.yaml", "architecture: arm64\n")

        self.assertEqual(stdlib.resolve("stdlib:debian/amd64.yml"), yml_path)
        self.assertEqual(stdlib.resolve("stdlib:debian/arm64.yaml"), yaml_path)

    def test_extension_less_resolves_yml_first(self):
        yml_path = self.write_spec(self.stdlib_dir, "debian/base.yml", "release: trixie\n")
        self.write_spec(self.stdlib_dir, "debian/base.yaml", "release: bookworm\n")

        self.assertEqual(stdlib.resolve("stdlib:debian/base"), yml_path)

    def test_extension_less_falls_back_to_yaml(self):
        yaml_path = self.write_spec(self.stdlib_dir, "debian/feeds.yaml", "release: trixie\n")

        self.assertEqual(stdlib.resolve("stdlib:debian/feeds"), yaml_path)

    def test_empty_or_traversal_paths_raise_value_error(self):
        with self.assertRaises(ValueError):
            stdlib.resolve("stdlib:")
        with self.assertRaises(ValueError):
            stdlib.resolve("stdlib:../outside.yml")
        with self.assertRaises(ValueError):
            stdlib.resolve("stdlib:debian/../../outside.yml")

    def test_missing_specification_raises_file_not_found(self):
        with self.assertRaises(FileNotFoundError) as caught:
            stdlib.resolve("stdlib:missing/spec")
        err = str(caught.exception)
        self.assertIn("stdlib:missing/spec", err)
        self.assertIn("searched:", err)

    def test_staged_worktree_resolves_before_environment(self):
        staged_dir = os.path.join(self.workdir, "job", ".seine-stdlib")
        self.write_spec(self.stdlib_dir, "debian/amd64.yml", "origin: env\n")
        staged_file = self.write_spec(staged_dir, "debian/amd64.yml", "origin: staged\n")

        resolved = stdlib.resolve("stdlib:debian/amd64", project_root=os.path.join(self.workdir, "job"))
        self.assertEqual(resolved, staged_file)


class TestIsStdlibPath(avocado.Test):
    def setUp(self):
        super().setUp()
        self._orig_env = os.environ.get("SEINE_STDLIB_DIR")
        self.stdlib_dir = os.path.join(self.workdir, "stdlib")
        os.makedirs(self.stdlib_dir, exist_ok=True)
        os.environ["SEINE_STDLIB_DIR"] = self.stdlib_dir

    def tearDown(self):
        super().tearDown()
        if self._orig_env is not None:
            os.environ["SEINE_STDLIB_DIR"] = self._orig_env
        else:
            os.environ.pop("SEINE_STDLIB_DIR", None)

    def test_recognizes_stdlib_prefix_and_file_paths(self):
        inside_path = os.path.join(self.stdlib_dir, "debian", "amd64.yml")
        outside_path = os.path.join(self.workdir, "other", "amd64.yml")

        self.assertTrue(stdlib.is_stdlib_path("stdlib:debian/amd64.yml"))
        self.assertTrue(stdlib.is_stdlib_path(inside_path))
        self.assertFalse(stdlib.is_stdlib_path(outside_path))
        self.assertFalse(stdlib.is_stdlib_path(None))

    def test_stdlib_relpath_extracts_relative_path(self):
        inside_path = os.path.join(self.stdlib_dir, "debian", "amd64.yml")
        outside_path = os.path.join(self.workdir, "other", "amd64.yml")

        self.assertEqual(stdlib.stdlib_relpath("stdlib:debian/amd64.yml"), "debian/amd64.yml")
        self.assertEqual(stdlib.stdlib_relpath(inside_path), "debian/amd64.yml")
        self.assertIsNone(stdlib.stdlib_relpath(outside_path))


class TestSpecLoaderStdlib(avocado.Test):
    def setUp(self):
        super().setUp()
        self._orig_env = os.environ.get("SEINE_STDLIB_DIR")
        self.stdlib_dir = os.path.join(self.workdir, "stdlib")
        os.makedirs(self.stdlib_dir, exist_ok=True)
        os.environ["SEINE_STDLIB_DIR"] = self.stdlib_dir

    def tearDown(self):
        super().tearDown()
        if self._orig_env is not None:
            os.environ["SEINE_STDLIB_DIR"] = self._orig_env
        else:
            os.environ.pop("SEINE_STDLIB_DIR", None)

    def write_spec(self, directory, relpath, content):
        full = os.path.join(directory, relpath)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            f.write(content)
        return full

    def test_spec_requiring_stdlib_loads_cleanly(self):
        self.write_spec(self.stdlib_dir, "debian/amd64.yml",
                        "distribution:\n    architecture: amd64\n")
        main = self.write_spec(self.workdir, "main.yaml",
                               "requires:\n    - stdlib:debian/amd64\ndistribution:\n    release: trixie\n")

        build = BuildCmd()
        spec = build.load(main)
        self.assertEqual(spec["distribution"]["architecture"], "amd64")
        self.assertEqual(spec["distribution"]["release"], "trixie")

    def test_transitive_stdlib_requires(self):
        self.write_spec(self.stdlib_dir, "debian/base.yml",
                        "distribution:\n    install_recommends: false\n")
        self.write_spec(self.stdlib_dir, "debian/trixie.yml",
                        "requires:\n    - stdlib:debian/base\ndistribution:\n    release: trixie\n")
        main = self.write_spec(self.workdir, "main.yaml",
                               "requires:\n    - stdlib:debian/trixie\n")

        build = BuildCmd()
        spec = build.load(main)
        self.assertEqual(spec["distribution"]["release"], "trixie")
        self.assertFalse(spec["distribution"]["install_recommends"])

    def test_missing_stdlib_in_requires_reports_error(self):
        main = self.write_spec(self.workdir, "main.yaml",
                               "requires:\n    - stdlib:debian/nonexistent\n")

        build = BuildCmd()
        with self.assertRaises(FileNotFoundError) as caught:
            build.load(main)
        err = str(caught.exception)
        self.assertIn("stdlib:debian/nonexistent", err)
        self.assertIn(main, err)

    def test_load_direct_stdlib_spec(self):
        self.write_spec(self.stdlib_dir, "debian/amd64.yml",
                        "distribution:\n    architecture: amd64\n")

        build = BuildCmd()
        spec = build.load("stdlib:debian/amd64.yml")
        self.assertEqual(spec["distribution"]["architecture"], "amd64")

    def test_load_all_direct_stdlib_specs(self):
        self.write_spec(self.stdlib_dir, "debian/amd64.yml",
                        "distribution:\n    architecture: amd64\n")
        self.write_spec(self.stdlib_dir, "debian/trixie.yml",
                        "distribution:\n    release: trixie\n")

        build = BuildCmd()
        spec = build.load_all(["stdlib:debian/amd64", "stdlib:debian/trixie"])
        self.assertEqual(spec["distribution"]["architecture"], "amd64")
        self.assertEqual(spec["distribution"]["release"], "trixie")


class TestCliAndTuiStdlib(avocado.Test):
    def setUp(self):
        super().setUp()
        self._orig_env = os.environ.get("SEINE_STDLIB_DIR")
        self.stdlib_dir = os.path.join(self.workdir, "stdlib")
        os.makedirs(self.stdlib_dir, exist_ok=True)
        os.environ["SEINE_STDLIB_DIR"] = self.stdlib_dir

    def tearDown(self):
        super().tearDown()
        if self._orig_env is not None:
            os.environ["SEINE_STDLIB_DIR"] = self._orig_env
        else:
            os.environ.pop("SEINE_STDLIB_DIR", None)

    def write_spec(self, directory, relpath, content):
        full = os.path.join(directory, relpath)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            f.write(content)
        return full

    def test_cli_dump_with_stdlib_argument(self):
        self.write_spec(self.stdlib_dir, "debian/amd64.yml",
                        "distribution:\n    architecture: amd64\n")

        out = io.StringIO()
        with self.assertRaises(SystemExit) as caught, contextlib.redirect_stdout(out):
            BuildCmd().main(["--dump", "stdlib:debian/amd64"])
        self.assertEqual(caught.exception.code, 0)
        self.assertIn("amd64", out.getvalue())

    def test_cli_missing_stdlib_fails(self):
        with self.assertRaises(SystemExit) as caught:
            with contextlib.redirect_stderr(io.StringIO()) as err:
                BuildCmd().main(["stdlib:debian/missing"])
        self.assertEqual(caught.exception.code, 1)
        self.assertIn("stdlib:debian/missing", err.getvalue())

    def test_tui_context_use_and_side_load(self):
        base_path = self.write_spec(
            self.stdlib_dir, "debian/base.yml",
            "distribution:\n    architecture: amd64\n    release: trixie\n",
        )
        extra_path = self.write_spec(
            self.stdlib_dir, "debian/extra.yml",
            "playbook:\n    - name: p\n      tasks: []\n",
        )

        ctx = Context()
        ctx.use(["stdlib:debian/base"])
        self.assertTrue(ctx.active)
        self.assertEqual(ctx.groups[0], [base_path])

        ctx.side_load("stdlib:debian/extra")
        self.assertEqual(ctx.groups[0], [base_path, extra_path])

        ctx.side_unload("stdlib:debian/extra")
        self.assertEqual(ctx.groups[0], [base_path])


if __name__ == "__main__":
    avocado.main()
