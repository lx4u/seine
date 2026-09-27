#!/usr/bin/env python3
# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import avocado
import os
import sys

from unittest import mock

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine.extends import go_licenses

class ScanTreeAsksSyftToEnrichGoLicenses(avocado.Test):
    def test(self):
        with mock.patch("seine.sbom.scan_directory") as scan_directory:
            go_licenses.scan_tree("/some/build/dir")
        scan_directory.assert_called_once_with(
            "/some/build/dir", extra_args=["--enrich", "golang"])

MODULES_TXT = """# github.com/foo/bar v1.2.3
## explicit; go 1.21
github.com/foo/bar/baz
# github.com/other/mod v0.4.0
"""

class ParsesModulesTxt(avocado.Test):
    def test_reads_the_module_lines_only(self):
        path = os.path.join(self.workdir, "vendor")
        os.makedirs(path)
        with open(os.path.join(path, "modules.txt"), "w") as f:
            f.write(MODULES_TXT)
        self.assertEqual(go_licenses.parse_modules_txt(path), [
            ("github.com/foo/bar", "v1.2.3"),
            ("github.com/other/mod", "v0.4.0"),
        ])

class ParsesModulePath(avocado.Test):
    def test_reads_the_module_line(self):
        with open(os.path.join(self.workdir, "go.mod"), "w") as f:
            f.write("module github.com/example/myapp\n\ngo 1.21\n")
        self.assertEqual(go_licenses.parse_module_path(self.workdir),
                         "github.com/example/myapp")

def package_for(purl, license=None, copyright=None):
    package = {"name": purl.split("/", 1)[-1],
               "externalRefs": [{"referenceType": "purl",
                                 "referenceLocator": purl}]}
    if license is not None:
        package["licenseConcluded"] = license
    if copyright is not None:
        package["copyrightText"] = copyright
    return package

MODULES = [("github.com/foo/bar", "v1.2.3")]

class LicenseStanzas(avocado.Test):
    def test_a_classified_module(self):
        syft_doc = {"packages": [package_for(
            "pkg:golang/github.com/foo/bar@v1.2.3",
            license="MIT", copyright="Copyright 2020 Foo Bar")]}
        text = go_licenses.license_stanzas(MODULES, syft_doc)
        self.assertIn("Files: vendor/github.com/foo/bar/*", text)
        self.assertIn("License: MIT", text)
        self.assertIn("Copyright: Copyright 2020 Foo Bar", text)

    def test_a_module_syft_found_nothing_for(self):
        text = go_licenses.license_stanzas(MODULES, {"packages": []})
        self.assertIn("Files: vendor/github.com/foo/bar/*", text)
        self.assertIn("License: NOASSERTION", text)

    def test_noassertion_from_syft_is_treated_as_unclassified(self):
        syft_doc = {"packages": [package_for(
            "pkg:golang/github.com/foo/bar@v1.2.3", license="NOASSERTION")]}
        text = go_licenses.license_stanzas(MODULES, syft_doc)
        self.assertIn("License: NOASSERTION", text)

    def test_matched_by_purl_path_ignoring_version(self):
        # A vendored module's exact version need not match syft's purl,
        # which may resolve to a different (pseudo-)version.
        syft_doc = {"packages": [package_for(
            "pkg:golang/github.com/foo/bar@v0.0.0-20200101000000-abcdef123456",
            license="Apache-2.0")]}
        text = go_licenses.license_stanzas(MODULES, syft_doc)
        self.assertIn("License: Apache-2.0", text)

class MainStanza(avocado.Test):
    def test_the_app_s_own_module_gets_a_files_star_stanza(self):
        syft_doc = {"packages": [package_for(
            "pkg:golang/github.com/example/myapp", license="MIT",
            copyright="Copyright 2024 Example Corp")]}
        text = go_licenses.main_stanza("github.com/example/myapp", syft_doc)
        self.assertIn("Files: *\n", text)
        self.assertIn("License: MIT", text)
        self.assertIn("Copyright: Copyright 2024 Example Corp", text)

    def test_unmatched_is_noassertion(self):
        text = go_licenses.main_stanza("github.com/example/myapp",
                                       {"packages": []})
        self.assertIn("Files: *\n", text)
        self.assertIn("License: NOASSERTION", text)
