#!/usr/bin/env python3

import os
import sys

path_to_self = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

import avocado
from seine import analyze
from seine.build import BuildCmd


# A peer file changes only 'version', keeping earlier settings.
class PeerFileChangesOnlyVersion(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.loads("""
            image:
                filename: disk.img
                ostree:
                    mode: standard
                    stateroot: debian
                    ref: debian/amd64
                    gpg-key: vault:ostree-commits
                    manifest-key: vault:update-manifest
                    version: "1"
        """)
        build.loads("""
            image:
                ostree:
                    version: "2"
        """)
        ostree = build.spec["image"]["ostree"]
        self.assertEqual(ostree["mode"], "standard")
        self.assertEqual(ostree["stateroot"], "debian")
        self.assertEqual(ostree["ref"], "debian/amd64")
        self.assertEqual(ostree["gpg-key"], "vault:ostree-commits")
        self.assertEqual(ostree["manifest-key"], "vault:update-manifest")
        self.assertEqual(ostree["version"], "2")


# A fragment required by a spec adds 'gpg-key' to the block.
class FragmentAddsGpgKey(avocado.Test):
    def test(self):
        frag_path = os.path.join(self.workdir, "frag.yaml")
        with open(frag_path, "w") as f:
            f.write("""
image:
    ostree:
        gpg-key: vault:ostree-commits
""")
        main_path = os.path.join(self.workdir, "main.yaml")
        with open(main_path, "w") as f:
            f.write("""
requires:
    - frag
image:
    ostree:
        mode: standard
        stateroot: debian
        ref: debian/amd64
        version: "1"
        manifest-key: vault:update-manifest
""")
        build = BuildCmd()
        build.load(main_path)
        ostree = build.spec["image"]["ostree"]
        self.assertEqual(ostree["mode"], "standard")
        self.assertEqual(ostree["stateroot"], "debian")
        self.assertEqual(ostree["ref"], "debian/amd64")
        self.assertEqual(ostree["version"], "1")
        self.assertEqual(ostree["manifest-key"], "vault:update-manifest")
        self.assertEqual(ostree["gpg-key"], "vault:ostree-commits")


# 'sources:' entries merge by group name, then key by key.
class SourcesMergedByGroup(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.loads("""
            image:
                ostree:
                    mode: standard
                    sources:
                        main:
                            mode: standard
                            stateroot: debian
                        recovery:
                            mode: disabled
        """)
        build.loads("""
            image:
                ostree:
                    sources:
                        main:
                            ref: debian/amd64
                        extra:
                            mode: standard
        """)
        sources = build.spec["image"]["ostree"]["sources"]
        self.assertEqual(sources["main"], {
            "mode": "standard",
            "stateroot": "debian",
            "ref": "debian/amd64",
        })
        self.assertEqual(sources["recovery"], {"mode": "disabled"})
        self.assertEqual(sources["extra"], {"mode": "standard"})


# Merged block yields the same digest as a block written in one file.
class MergedBlockDigestEqualsSingleFile(avocado.Test):
    def test(self):
        single = BuildCmd()
        single.loads("""
            distribution:
                release: trixie
                architecture: amd64
            image:
                filename: disk.img
                partitions:
                    - label: esp
                      type: vfat
                      where: /efi
                    - label: root
                      type: ext4
                      where: /
                    - label: var
                      type: ext4
                      where: /var
                ostree:
                    mode: standard
                    stateroot: debian
                    ref: debian/amd64
                    gpg-key: vault:ostree-commits
                    manifest-key: vault:update-manifest
                    version: "2"
                    payload:
                        path: payload-dir
                        deltas-from: ["1"]
        """)

        merged = BuildCmd()
        merged.loads("""
            distribution:
                release: trixie
                architecture: amd64
            image:
                filename: disk.img
                partitions:
                    - label: esp
                      type: vfat
                      where: /efi
                    - label: root
                      type: ext4
                      where: /
                    - label: var
                      type: ext4
                      where: /var
                ostree:
                    mode: standard
                    stateroot: debian
                    ref: debian/amd64
                    gpg-key: vault:ostree-commits
                    manifest-key: vault:update-manifest
        """)
        merged.loads("""
            image:
                ostree:
                    version: "2"
                    payload:
                        path: payload-dir
                        deltas-from: ["1"]
        """)

        self.assertEqual(single.spec["image"]["ostree"],
                         merged.spec["image"]["ostree"])
        self.assertEqual(analyze.spec_digest(single.parse()),
                         analyze.spec_digest(merged.parse()))


# An existing replace case now merges key by key.
class ExistingReplaceCaseNowMerges(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.loads("""
            image:
                ostree:
                    mode: standard
                    stateroot: debian
                    ref: debian/stable
        """)
        build.loads("""
            image:
                ostree:
                    ref: debian/testing
        """)
        ostree = build.spec["image"]["ostree"]
        self.assertEqual(ostree["mode"], "standard")
        self.assertEqual(ostree["stateroot"], "debian")
        self.assertEqual(ostree["ref"], "debian/testing")


# A setting, source group, or the ostree block is removed with null.
class RemovingKeysWithNull(avocado.Test):
    def test_remove_scalar_key(self):
        build = BuildCmd()
        build.loads("""
            image:
                ostree:
                    mode: standard
                    version: "1"
        """)
        build.loads("""
            image:
                ostree:
                    version: null
        """)
        self.assertNotIn("version", build.spec["image"]["ostree"])
        self.assertEqual(build.spec["image"]["ostree"]["mode"], "standard")

    def test_remove_source_group_and_setting(self):
        build = BuildCmd()
        build.loads("""
            image:
                ostree:
                    sources:
                        main:
                            stateroot: debian
                            ref: debian/amd64
                        recovery:
                            mode: disabled
        """)
        build.loads("""
            image:
                ostree:
                    sources:
                        main:
                            ref: null
                        recovery: null
        """)
        sources = build.spec["image"]["ostree"]["sources"]
        self.assertNotIn("recovery", sources)
        self.assertEqual(sources["main"], {"stateroot": "debian"})

    def test_remove_whole_ostree_block(self):
        build = BuildCmd()
        build.loads("""
            image:
                filename: disk.img
                ostree:
                    mode: standard
        """)
        build.loads("""
            image:
                ostree: null
        """)
        self.assertNotIn("ostree", build.spec["image"])


# A mode change in a later file keeps other settings.
class ModeChangeKeepsOtherSettings(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.loads("""
            image:
                ostree:
                    mode: standard
                    stateroot: debian
                    ref: debian/amd64
        """)
        build.loads("""
            image:
                ostree:
                    mode: disabled
        """)
        ostree = build.spec["image"]["ostree"]
        self.assertEqual(ostree["mode"], "disabled")
        self.assertEqual(ostree["stateroot"], "debian")
        self.assertEqual(ostree["ref"], "debian/amd64")
