#!/usr/bin/env python3
# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import os
import shutil
import sys
import tempfile

import avocado

path_to_self = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine.build import BuildCmd
from seine.packages import Builder

COMMON = os.path.join(path_to_sources, "examples", "common")
RUNWAY_SPEC = os.path.join(COMMON, "runway.yaml")


class RunwayPackageSpec(avocado.Test):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="seine-test-runway-", dir="/var/tmp")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_runway_spec_loads_and_resolves_custom_packages(self):
        build = BuildCmd()
        spec = build.load(RUNWAY_SPEC)
        self.assertIn("packages", spec)
        self.assertEqual(
            spec.get("defaults", {}).get("sign-key"),
            "vault:custom-packages",
        )

    def test_runway_package_configuration(self):
        build = BuildCmd()
        build.load_all([
            os.path.join(COMMON, "bookworm.yaml"),
            os.path.join(COMMON, "amd64.yaml"),
            os.path.join(COMMON, "pc-image.yaml"),
            RUNWAY_SPEC,
        ])
        build.parse()
        packages = build.image.packages
        self.assertEqual(len(packages), 1)

        pkg = packages[0]
        self.assertEqual(pkg.name, "runway")
        self.assertEqual(pkg.scheme, "git")
        self.assertEqual(pkg.parameters.get("protocol"), "https")
        self.assertEqual(pkg.parameters.get("branch"), "main")
        self.assertEqual(
            pkg.parameters.get("rev"),
            "cab5beff5745a87dfbd3facc6fa576daac21c463",
        )
        self.assertEqual(pkg.scope, ["target"])
        self.assertEqual(pkg.profiles, ["nocheck"])
        self.assertEqual(len(pkg.patches), 0)

    def test_runway_is_built_for_the_target_architecture_only(self):
        build = BuildCmd()
        build.load_all([
            os.path.join(COMMON, "bookworm.yaml"),
            os.path.join(COMMON, "arm64.yaml"),
            os.path.join(COMMON, "pc-image.yaml"),
            RUNWAY_SPEC,
        ])
        build.parse()
        pkg = build.image.packages[0]
        builder = Builder(build.spec["distribution"], {}, None)
        self.assertEqual(builder.architectures(pkg), ["arm64"])

    def test_runway_tools_go_into_the_imager_tools_container(self):
        spec = BuildCmd().load(RUNWAY_SPEC)
        self.assertEqual(spec["imager"]["packages"], ["runway-tools"])
