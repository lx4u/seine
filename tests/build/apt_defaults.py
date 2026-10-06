#!/usr/bin/env python3

import avocado
import os
import sys

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine.build import BuildCmd

SPEC = """
distribution:
    release: trixie
    architecture: amd64
    uri: http://example.com/debian
playbook:
    - name: install
      tasks:
          - apt:
                name: vim
"""

def apt_defaults(*texts):
    build = BuildCmd()
    for text in texts:
        build.loads(text)
    build.parse()
    return (build.spec.get("defaults") or {}).get("apt")

def defaults(value):
    return f"defaults:\n    apt:\n        install_recommends: {value}\n"

class DefaultsAptIsMergedAcrossFiles(avocado.Test):
    def test_false_is_kept(self):
        self.assertEqual(apt_defaults(SPEC, defaults("false")),
                         {"install_recommends": False})

    def test_a_later_true_overrides_an_earlier_false(self):
        self.assertIsNone(apt_defaults(SPEC, defaults("false"), defaults("true")))

    def test_a_later_false_overrides_an_earlier_true(self):
        self.assertEqual(apt_defaults(SPEC, defaults("true"), defaults("false")),
                         {"install_recommends": False})

    def test_true_is_dropped_with_its_section(self):
        self.assertIsNone(apt_defaults(SPEC, defaults("true")))

    def test_an_unknown_setting_is_refused(self):
        with self.assertRaisesRegex(ValueError, "install_recommends"):
            apt_defaults(SPEC, "defaults:\n    apt:\n        update_cache: true\n")

    def test_a_string_is_refused(self):
        with self.assertRaisesRegex(ValueError, "true or false"):
            apt_defaults(SPEC, defaults('"no"'))

    def test_a_non_dictionary_is_refused(self):
        with self.assertRaisesRegex(ValueError, "dictionary"):
            apt_defaults(SPEC, "defaults:\n    apt: [install_recommends]\n")

class OnlyFalseMovesTheRootfsDigest(avocado.Test):
    def setUp(self):
        os.environ["SEINE_CACHE_DIR"] = self.workdir
        os.environ["SEINE_BUILD_DIR"] = self.workdir

    def digest(self, *texts):
        build = BuildCmd()
        for text in texts:
            build.loads(text)
        build.parse()
        build.image._from = "base"
        return build.image._rootfs_digest(None)

    def test_true_leaves_it_alone(self):
        self.assertEqual(self.digest(SPEC), self.digest(SPEC, defaults("true")))

    def test_a_true_that_undoes_a_false_leaves_it_alone(self):
        self.assertEqual(self.digest(SPEC),
                         self.digest(SPEC, defaults("false"), defaults("true")))

    def test_false_moves_it(self):
        self.assertNotEqual(self.digest(SPEC), self.digest(SPEC, defaults("false")))

class ImageReadsTheDefaultBack(avocado.Test):
    def recommends(self, *texts):
        build = BuildCmd()
        for text in texts:
            build.loads(text)
        build.parse()
        return build.image._install_recommends()

    def test_true_without_a_setting(self):
        self.assertTrue(self.recommends(SPEC))

    def test_false_when_a_file_says_so(self):
        self.assertFalse(self.recommends(SPEC, defaults("false")))
