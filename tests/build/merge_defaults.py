#!/usr/bin/env python3

import avocado
import os
import sys

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine.build import BuildCmd

IMAGE = """
                image:
                    filename: t.img
                    partitions:
                        - label: rootfs
                          where: /
"""

class ADefaultNeedsNoSourceOfItsOwn(avocado.Test):
    def test(self):
        build = BuildCmd()
        # An architecture file, adding to a package it knows by name. It
        # has no opinion about where the source comes from -- that is
        # said by whoever asks for the build.
        build.loads("""
                defaults:
                    packages:
                        - name: nvidia-open
                          profiles:
                              - nocheck
        """ + IMAGE)
        build.loads("""
                packages:
                    - source: git://github.com/NVIDIA/open-gpu-kernel-modules.git;rev=deadbeef
                      name: nvidia-open
        """)
        spec = build.parse()
        self.assertEqual(len(spec["packages"]), 1)
        self.assertEqual(spec["packages"][0]["profiles"], ["nocheck"])

class ASourcelessDefaultNobodyBuildsIsDropped(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.loads("""
                defaults:
                    packages:
                        - name: nvidia-open
                          profiles:
                              - nocheck
        """ + IMAGE)
        build.parse()
        # It described a package nothing asked for, so it described
        # nothing -- and it did not conjure a build with no source.
        self.assertEqual(build.spec.get("packages"), None)

class ADescriptionIsStillChecked(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.loads("""
                defaults:
                    packages:
                        - name: nvidia-open
                          extends:
                              kernel:
                                  flavur: amd64
        """ + IMAGE)
        try:
            build.parse()
            self.fail("a misspelt setting under 'defaults' was accepted!")
        except ValueError:
            pass

class APackageStillNeedsASource(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.loads("""
                packages:
                    - name: nvidia-open
                      profiles:
                          - nocheck
        """ + IMAGE)
        try:
            build.parse()
            self.fail("an entry under 'packages' was built with no source!")
        except ValueError:
            pass

class AnEntryWithNeitherSourceNorNameIsRefused(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.loads("""
                defaults:
                    packages:
                        - profiles:
                              - nocheck
        """ + IMAGE)
        try:
            build.parse()
            self.fail("an entry saying which package it is about was accepted!")
        except ValueError:
            pass

MODULE = """
                packages:
                    - source: git://github.com/NVIDIA/open-gpu-kernel-modules.git;rev=deadbeef
                      name: nvidia-open
                      version: "580.95.05"
                      extends:
                          module:
                              amd64-kernels:
                                  - apt://linux-headers-amd64
"""

class KernelsAreAddedToRatherThanSettled(avocado.Test):
    def test(self):
        build = BuildCmd()
        # What asks for the modules, naming the distribution's kernel.
        build.loads(MODULE)
        # An architecture file, adding the kernel this build makes.
        build.loads("""
                packages:
                    - name: nvidia-open
                      extends:
                          module:
                              amd64-kernels:
                                  - linux
        """)
        kernels = build.spec["packages"][0]["extends"]["module"]["amd64-kernels"]
        # Both, in the order they were written: neither file is
        # describing the same thing twice, so settling between them
        # would drop a kernel somebody asked to have modules for.
        self.assertEqual(kernels, ["apt://linux-headers-amd64", "linux"])

class TheSameKernelIsNotAddedTwice(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.loads(MODULE)
        build.loads("""
                packages:
                    - name: nvidia-open
                      extends:
                          module:
                              amd64-kernels:
                                  - apt://linux-headers-amd64
                                  - linux
        """)
        kernels = build.spec["packages"][0]["extends"]["module"]["amd64-kernels"]
        self.assertEqual(kernels, ["apt://linux-headers-amd64", "linux"])

class KernelsOfDifferentArchitecturesStayApart(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.loads(MODULE)
        build.loads("""
                packages:
                    - name: nvidia-open
                      extends:
                          module:
                              arm64-kernels:
                                  - apt://linux-headers-arm64
        """)
        module = build.spec["packages"][0]["extends"]["module"]
        self.assertEqual(module["amd64-kernels"], ["apt://linux-headers-amd64"])
        self.assertEqual(module["arm64-kernels"], ["apt://linux-headers-arm64"])

# An architecture file deriving a generic flavour of its own, the way
# 'slim-amd64' is meant to be built on by a board file.
DERIVED = """
                packages:
                    - source: apt://linux
                      revision: fixed1
                      extends:
                          kernel:
                              derived-flavours:
                                  amd64:
                                      slim-amd64: []
"""

class DerivedFlavoursAreAddedToRatherThanSettled(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.loads(DERIVED)
        # A board file, deriving its own flavour from the one above --
        # not an original Debian flavour, so it only exists once both
        # files' 'derived-flavours' are on the same package.
        build.loads("""
                packages:
                    - source: apt://linux
                      extends:
                          kernel:
                              derived-flavours:
                                  slim-amd64:
                                      pc: []
        """)
        derived = build.spec["packages"][0]["extends"]["kernel"]["derived-flavours"]
        self.assertEqual(derived, {"amd64": {"slim-amd64": []},
                                   "slim-amd64": {"pc": []}})

class DerivedFlavoursOfTheSameBaseAreMerged(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.loads(DERIVED)
        # Another board deriving from the same base as the first file --
        # both flavours are wanted, so the second file's base is added to
        # rather than replacing what the first already said about it.
        build.loads("""
                packages:
                    - source: apt://linux
                      extends:
                          kernel:
                              derived-flavours:
                                  amd64:
                                      cloud-pc: []
        """)
        derived = build.spec["packages"][0]["extends"]["kernel"]["derived-flavours"]
        self.assertEqual(derived, {"amd64": {"slim-amd64": [], "cloud-pc": []}})

# Two files rebuilding the same kernel (matched by 'source', neither
# names it) each wanting a config group of their own -- the gap a real
# session hit (build/chats/20260820T080504625424.json): the second
# file's whole 'configs:' was silently dropped rather than merged.
CONFIGS = """
                packages:
                    - source: apt://linux
                      extends:
                          kernel:
                              configs:
                                  debug-page-ref:
                                      - CONFIG_DEBUG_PAGE_REF=y
"""

class KernelConfigGroupsAreAddedToRatherThanSettled(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.loads(CONFIGS)
        build.loads("""
                packages:
                    - source: apt://linux
                      extends:
                          kernel:
                              configs:
                                  magic-sysrq:
                                      - CONFIG_MAGIC_SYSRQ=n
        """)
        configs = build.spec["packages"][0]["extends"]["kernel"]["configs"]
        self.assertEqual(configs,
                         {"debug-page-ref": ["CONFIG_DEBUG_PAGE_REF=y"],
                          "magic-sysrq": ["CONFIG_MAGIC_SYSRQ=n"]})

class KernelConfigsOfTheSameGroupAreMerged(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.loads(CONFIGS)
        # Another file adding to the same group -- both lines are
        # wanted, so the second file's group adds to rather than
        # replacing what the first already said about it.
        build.loads("""
                packages:
                    - source: apt://linux
                      extends:
                          kernel:
                              configs:
                                  debug-page-ref:
                                      - CONFIG_DEBUG_PAGE_REF_TRACKING=y
        """)
        configs = build.spec["packages"][0]["extends"]["kernel"]["configs"]
        self.assertEqual(configs,
                         {"debug-page-ref": ["CONFIG_DEBUG_PAGE_REF=y",
                                             "CONFIG_DEBUG_PAGE_REF_TRACKING=y"]})

class KernelConfigLinesAreNotAddedTwice(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.loads(CONFIGS)
        build.loads("""
                packages:
                    - source: apt://linux
                      extends:
                          kernel:
                              configs:
                                  debug-page-ref:
                                      - CONFIG_DEBUG_PAGE_REF=y
        """)
        configs = build.spec["packages"][0]["extends"]["kernel"]["configs"]
        self.assertEqual(configs,
                         {"debug-page-ref": ["CONFIG_DEBUG_PAGE_REF=y"]})

class DefaultsAddTheirKernelsToo(avocado.Test):
    def test(self):
        build = BuildCmd()
        # An architecture file: modules for the kernel we build, if one
        # is built. Under 'defaults', so it asks for nothing itself.
        build.loads("""
                distribution:
                    release: trixie
                    architecture: amd64
                defaults:
                    packages:
                        - name: nvidia-open
                          extends:
                              module:
                                  amd64-kernels:
                                      - linux
        """ + IMAGE)
        build.loads(MODULE)
        build.loads("""
                packages:
                    - source: apt://linux
                      extends:
                          kernel:
                              flavour: amd64
        """)
        spec = build.parse()
        module = [p for p in spec["packages"]
                  if p.get("name") == "nvidia-open"][0]["extends"]["module"]
        self.assertEqual(module["amd64-kernels"],
                         ["apt://linux-headers-amd64", "linux"])

class ADefaultDropsAKernelNothingBuilds(avocado.Test):
    def test(self):
        build = BuildCmd()
        # The same architecture file, in a specification that builds no
        # kernel of its own -- which is most of them.
        build.loads("""
                distribution:
                    release: trixie
                    architecture: amd64
                defaults:
                    packages:
                        - name: nvidia-open
                          extends:
                              module:
                                  amd64-kernels:
                                      - linux
        """ + IMAGE)
        build.loads(MODULE)
        spec = build.parse()
        module = spec["packages"][0]["extends"]["module"]
        # The description described nothing, so it added nothing. The
        # modules are still built, against the distribution's kernel.
        self.assertEqual(module["amd64-kernels"], ["apt://linux-headers-amd64"])

class DefaultsAreFoldedIntoWhatIsBuilt(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.loads("""
                defaults:
                    packages:
                        - source: apt://linux
                          extends:
                              kernel:
                                  flavour: amd64
                image:
                    filename: t.img
                    partitions:
                        - label: rootfs
                          where: /
        """)
        build.loads("""
                packages:
                    - source: apt://linux=6.12.101-1
                      extends:
                          kernel:
                              upstream: https://kernel.org/linux-6.18.43.tar.xz
        """)
        build.parse()
        package = build.image.packages[0]
        self.assertEqual(package.kernel_flavour, "amd64")
        self.assertEqual(package.version, "6.12.101-1")

class ThePackageBeingBuiltBeatsTheDefault(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.loads("""
                defaults:
                    packages:
                        - source: apt://linux
                          extends:
                              kernel:
                                  flavour: amd64
                packages:
                    - source: apt://linux
                      extends:
                          kernel:
                              flavour: cloud-amd64
                image:
                    filename: t.img
                    partitions:
                        - label: rootfs
                          where: /
        """)
        build.parse()
        self.assertEqual(build.image.packages[0].kernel_flavour, "cloud-amd64")

class TheLastDefaultWins(avocado.Test):
    def test(self):
        build = BuildCmd()
        # The architecture file, then the board file that sits on it.
        build.loads("""
                defaults:
                    packages:
                        - source: apt://linux
                          extends:
                              kernel:
                                  flavour: amd64
                image:
                    filename: t.img
                    partitions:
                        - label: rootfs
                          where: /
        """)
        build.loads("""
                defaults:
                    packages:
                        - source: apt://linux
                          extends:
                              kernel:
                                  featureset: rt
        """)
        build.loads("""
                packages:
                    - source: apt://linux
        """)
        build.parse()
        package = build.image.packages[0]
        # The particular adds to the general rather than replacing it.
        self.assertEqual(package.kernel_flavour, "amd64")
        self.assertEqual(package.kernel_featureset, "rt")

class DefaultsAreCheckedWhereTheyAreWritten(avocado.Test):
    def test(self):
        build = BuildCmd()
        # A default nothing builds still has to make sense, or a typo in an
        # architecture file waits for the one image that rebuilds a kernel.
        build.loads("""
                defaults:
                    packages:
                        - source: apt://linux
                          extends:
                              kernel:
                                  flavours: amd64
                image:
                    filename: t.img
                    partitions:
                        - label: rootfs
                          where: /
        """)
        try:
            build.parse()
            self.fail("parsing succeeded for an unknown 'kernel' setting!")
        except ValueError:
            pass

class DefaultsHoldPackagesOnly(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.loads("""
                image:
                    filename: t.img
                    partitions:
                        - label: rootfs
                          where: /
        """)
        try:
            build.loads("""
                defaults:
                    playbook:
                        - name: nothing
            """)
            self.fail("parsing succeeded for a 'defaults' section that is "
                     "neither packages nor vault!")
        except ValueError:
            pass

# 'defaults: vault:' is fixed dev-only material so independent
# dev-vault instances agree instead of each generating their own --
# read back by Image._vault_defaults().
class DefaultsVaultReachesTheBuiltImage(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.loads("""
                defaults:
                    vault:
                        some-key:
                            key_pem: "-- key --"
                            cert_pem: "-- cert --"
                image:
                    filename: t.img
                    partitions:
                        - label: rootfs
                          where: /
        """)
        build.parse()
        self.assertEqual(build.image._vault_defaults(),
                         {"some-key": {"key_pem": "-- key --",
                                      "cert_pem": "-- cert --"}})

    def test_a_second_file_merges_by_name_last_wins(self):
        build = BuildCmd()
        build.loads("""
                defaults:
                    vault:
                        one: {key_pem: "1a", cert_pem: "1a"}
                image:
                    filename: t.img
                    partitions:
                        - label: rootfs
                          where: /
        """)
        build.loads("""
                defaults:
                    vault:
                        one: {key_pem: "1b", cert_pem: "1b"}
                        two: {key_pem: "2", cert_pem: "2"}
        """)
        build.parse()
        self.assertEqual(build.image._vault_defaults(), {
            "one": {"key_pem": "1b", "cert_pem": "1b"},
            "two": {"key_pem": "2", "cert_pem": "2"},
        })

    def test_not_a_mapping_is_refused(self):
        build = BuildCmd()
        build.loads("""
                image:
                    filename: t.img
                    partitions:
                        - label: rootfs
                          where: /
        """)
        try:
            build.loads("""
                defaults:
                    vault:
                        - not a mapping
            """)
            self.fail("a non-mapping 'defaults: vault' was accepted")
        except ValueError:
            pass

# 'defaults: sign-key' names the repository signing key when neither
# --sign-key nor SEINE_SIGN_KEY does -- read back by Image, weakest of
# the three.
class DefaultsSignKeyReachesTheBuiltImage(avocado.Test):
    def loaded(self, *texts):
        build = BuildCmd()
        for text in texts:
            build.loads(text)
        build.parse()
        return build.image._sign_key_default()

    def test(self):
        self.assertEqual(self.loaded("""
                defaults:
                    sign-key: vault:custom-packages
                image:
                    filename: t.img
                    partitions:
                        - label: rootfs
                          where: /
        """), "vault:custom-packages")

    def test_a_second_file_wins(self):
        self.assertEqual(self.loaded("""
                defaults:
                    sign-key: vault:first
                image:
                    filename: t.img
                    partitions:
                        - label: rootfs
                          where: /
        """, """
                defaults:
                    sign-key: vault:second
        """), "vault:second")

    def test_not_a_name_is_refused(self):
        build = BuildCmd()
        build.loads("""
                image:
                    filename: t.img
                    partitions:
                        - label: rootfs
                          where: /
        """)
        try:
            build.loads("""
                defaults:
                    sign-key: {not: a name}
            """)
            self.fail("a non-string 'defaults: sign-key' was accepted")
        except ValueError:
            pass

# 'overrides', unlike 'defaults', stands in for nothing else -- there
# is no CLI flag or external source it could ever lose to, so a spec
# using it is the one and only place the setting is made.
class OverridesLocalesReachesTheBuiltImage(avocado.Test):
    def loaded(self, *texts):
        build = BuildCmd()
        for text in texts:
            build.loads(text)
        build.parse()
        return build.image._locales_override()

    def test(self):
        self.assertEqual(self.loaded("""
                overrides:
                    locales: [en]
                image:
                    filename: t.img
                    partitions:
                        - label: rootfs
                          where: /
        """), ["en"])

    def test_a_second_file_wins(self):
        self.assertEqual(self.loaded("""
                overrides:
                    locales: [en]
                image:
                    filename: t.img
                    partitions:
                        - label: rootfs
                          where: /
        """, """
                overrides:
                    locales: [en, fr]
        """), ["en", "fr"])

    def test_unset_is_none_not_an_empty_list(self):
        self.assertIsNone(self.loaded("""
                image:
                    filename: t.img
                    partitions:
                        - label: rootfs
                          where: /
        """))

    def test_not_a_list_is_refused(self):
        build = BuildCmd()
        build.loads("""
                image:
                    filename: t.img
                    partitions:
                        - label: rootfs
                          where: /
        """)
        try:
            build.loads("""
                overrides:
                    locales: en
            """)
            self.fail("a non-list 'overrides: locales' was accepted")
        except ValueError:
            pass

    def test_an_unknown_setting_is_refused(self):
        build = BuildCmd()
        build.loads("""
                image:
                    filename: t.img
                    partitions:
                        - label: rootfs
                          where: /
        """)
        try:
            build.loads("""
                overrides:
                    typo: en
            """)
            self.fail("an unknown 'overrides' setting was accepted")
        except ValueError:
            pass

    def test_locales_under_defaults_is_refused(self):
        build = BuildCmd()
        build.loads("""
                image:
                    filename: t.img
                    partitions:
                        - label: rootfs
                          where: /
        """)
        try:
            build.loads("""
                defaults:
                    locales: [en]
            """)
            self.fail("'defaults: locales' was accepted -- it moved to 'overrides'")
        except ValueError:
            pass

    # The shared fragment carries the default, the example reaches it --
    # however it is built, composed or fragment-direct.
    def test_rebuild_busybox_signs_with_the_shared_key(self):
        for name in ("main.yaml", "busybox.yaml"):
            build = BuildCmd()
            build.load(os.path.join(path_to_sources, "examples",
                                    "rebuild-busybox", name))
            build.parse()
            self.assertEqual(build.image._sign_key_default(),
                             "vault:custom-packages")
