#!/usr/bin/env python3

import avocado
import os
import sys

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine.build import BuildCmd

class MergeNewPartitionWithoutPriorities(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.loads("""
            image:
                filename: simple-test.img
                partitions:
                    - label: rootfs
                      where: /
        """)
        build.loads("""
            image:
                partitions:
                    - label: data
                      where: /var
        """)
        spec = build.parse()
        parts = spec["image"]["partitions"]
        if len(parts) != 2 or parts[0]["label"] != "rootfs" or parts[1]["label"] != "data":
            self.fail("expected 2 partitions: 'rootfs' and 'data' (got %s)" % parts)

class MergeNewPartitionWithPriorities(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.loads("""
            image:
                filename: simple-test.img
                partitions:
                    - label: rootfs
                      where: /
        """)
        build.loads("""
            image:
                partitions:
                    - label: data
                      priority: 800
                      where: /var
                    - label: boot
                      priority: 100
                      where: /boot
        """)
        spec = build.parse()
        parts = spec["image"]["partitions"]
        if len(parts) != 3 or parts[0]["label"] != "boot" or parts[1]["label"] != "rootfs" or parts[2]["label"] != "data":
            self.fail("expected 3 partitions: 'boot', 'rootfs' and 'data' (got %s)" % parts)

class MergePartitionWithAdditionalAttributes(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.loads("""
            image:
                filename: simple-test.img
                partitions:
                    - label: boot
                      type: vfat
                      where: /boot
        """)
        build.loads("""
            image:
                partitions:
                    - label: boot
                      flags:
                          - boot
                          - primary
                      size: 256MiB
        """)
        spec = build.parse()
        parts = spec["image"]["partitions"]
        if len(parts) != 1 or parts[0]["label"] != "boot":
            self.fail("expected 1 partition: 'boot' (got %s)" % parts)
        part = parts[0]
        if len(part["flags"]) != 2:
            self.fail("expected 2 partition flags: got %s" % part["flags"])
        if part["size"] != 256 * 1024 * 1024:
            self.fail("expected size of 256MiB: got %s" % part["size"])
        if part["where"] != "/boot":
            self.fail("expected 'where' to be '/boot': got %s" % part["where"])

class MergePartitionFlags(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.loads("""
            image:
                filename: simple-test.img
                partitions:
                    - label: boot
                      where: /boot
                      type: vfat
                      flags:
                          - boot
        """)
        build.loads("""
            image:
                partitions:
                    - label: boot
                      flags:
                          - boot
                          - primary
        """)
        spec = build.parse()
        parts = spec["image"]["partitions"]
        if len(parts) != 1 or parts[0]["label"] != "boot":
            self.fail("expected 1 partition: 'boot' (got %s)" % parts)
        part = parts[0]
        if len(part["flags"]) != 2:
            self.fail("expected 2 partition flags: got %s" % part["flags"])

class MergePartitionFlagRemoved(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.loads("""
            image:
                filename: simple-test.img
                partitions:
                    - label: rootfs
                      where: /
                      flags:
                          - boot
                          - primary
        """)
        build.loads("""
            image:
                partitions:
                    - label: rootfs
                      flags:
                          - ~boot
        """)
        spec = build.parse()
        parts = spec["image"]["partitions"]
        if len(parts) != 1 or parts[0]["label"] != "rootfs":
            self.fail("expected 1 partition: 'rootfs' (got %s)" % parts)
        part = parts[0]
        if len(part["flags"]) != 1:
            self.fail("expected 1 partition flag: got %s" % part["flags"])

class MergeClearPartitionFlagsButNoneSet(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.loads("""
            image:
                filename: simple-test.img
                partitions:
                    - label: rootfs
                      where: /
        """)
        build.loads("""
            image:
                partitions:
                    - label: rootfs
                      flags:
                          - ~boot
                          - ~primary
        """)
        spec = build.parse()
        parts = spec["image"]["partitions"]
        if len(parts) != 1 or parts[0]["label"] != "rootfs":
            self.fail("expected 1 partition: 'rootfs' (got %s)" % parts)
        part = parts[0]
        if len(part["flags"]) != 0:
            self.fail("expected 0 partition flags: got %s" % part["flags"])

class MergeNewVolumeWithoutPriorities(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.loads("""
            image:
                filename: simple-test.img
                partitions:
                    - label: main
                      group: main
                      size: 1GiB
                      flags:
                          - lvm
                volumes:
                    - label: rootfs
                      group: main
                      where: /
        """)
        build.loads("""
            image:
                volumes:
                    - label: data
                      group: main
                      where: /var
        """)
        spec = build.parse()
        vols = spec["image"]["volumes"]
        if len(vols) != 2 or vols[0]["label"] != "rootfs" or vols[1]["label"] != "data":
            self.fail("expected 2 volumes: 'rootfs' and 'data' (got %s)" % vols)

class MergeNewVolumesWithPriorities(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.loads("""
            image:
                filename: simple-test.img
                partitions:
                    - label: main
                      group: main
                      size: 1GiB
                      flags:
                          - lvm
                volumes:
                    - label: rootfs
                      group: main
                      where: /
        """)
        build.loads("""
            image:
                volumes:
                    - label: data
                      group: main
                      priority: 800
                      where: /var
                    - label: boot
                      group: main
                      priority: 100
                      where: /boot
        """)
        spec = build.parse()
        vols = spec["image"]["volumes"]
        if len(vols) != 3 or vols[0]["label"] != "boot" or vols[1]["label"] != "rootfs" or vols[2]["label"] != "data":
            self.fail("expected 3 volumes: 'boot', 'rootfs' and 'data' (got %s)" % vols)

class MergeVolumeAttributes(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.loads("""
            image:
                filename: simple-test.img
                partitions:
                    - label: main
                      group: main
                      size: 1GiB
                      flags:
                          - lvm
                volumes:
                    - label: rootfs
                      group: main
                      size: 750MiB
                      where: /
        """)
        build.loads("""
            image:
                volumes:
                    - label: rootfs
                      size: 500MiB
        """)
        spec = build.parse()
        vols = spec["image"]["volumes"]
        if len(vols) != 1 or vols[0]["label"] != "rootfs":
            self.fail("expected 1 volume: 'rootfs' (got %s)" % vols)
        vol = vols[0]
        # Two peer files, so the second amends the first's size.
        if vol["size"] != 500 * 1024 * 1024:
            self.fail("expected size of 500MiB: got %s" % vol["size"])

class MergeNewVmFormatIsAdded(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.loads("""
            image:
                filename: simple-test.img
                partitions:
                    - label: rootfs
                      where: /
            vms:
                formats:
                    vmware:
                        type: vmdk
        """)
        build.loads("""
            vms:
                formats:
                    virtualbox:
                        type: vdi
        """)
        spec = build.parse()
        formats = spec["vms"]["formats"]
        self.assertEqual(set(formats), {"vmware", "virtualbox"})

class MergeVmFormatSettingIsAmended(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.loads("""
            image:
                filename: simple-test.img
                partitions:
                    - label: rootfs
                      where: /
            vms:
                formats:
                    vmware:
                        type: vmdk
        """)
        # Two peer files, so the second amends 'vmware' with a setting
        # the first never gave it.
        build.loads("""
            vms:
                formats:
                    vmware:
                        cpus: 8
        """)
        spec = build.parse()
        formats = spec["vms"]["formats"]
        self.assertEqual(len(formats), 1)
        self.assertEqual(formats["vmware"]["type"], "vmdk")
        self.assertEqual(formats["vmware"]["cpus"], 8)

class MergeVmDefaults(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.loads("""
            image:
                filename: simple-test.img
                partitions:
                    - label: rootfs
                      where: /
            vms:
                defaults:
                    cpus: 4
                formats:
                    vmware:
                        type: vmdk
        """)
        build.loads("""
            vms:
                defaults:
                    memory: 4096
        """)
        spec = build.parse()
        defaults = spec["vms"]["defaults"]
        self.assertEqual(defaults["cpus"], 4)
        self.assertEqual(defaults["memory"], 4096)

if __name__ == "__main__":
    avocado.main()

class FeedsAreAddedRatherThanReplaced(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.loads("""
                distribution:
                    release: bookworm
                    feeds:
                        - suite: bookworm
                          uri: http://snapshot.debian.org/archive/debian/20260101
                        - suite: bookworm-security
                          uri: http://snapshot.debian.org/archive/debian-security/20260101
                image:
                    filename: t.img
                    partitions:
                        - label: rootfs
                          where: /
        """)
        # A fragment adding one feed keeps the others, URIs and all: a
        # specification built from a snapshot has changed exactly those,
        # and restating them here is what it must not have to do.
        build.loads("""
                distribution:
                    feeds:
                        - suite: bookworm-backports
        """)
        feeds = build.spec["distribution"]["feeds"]
        self.assertEqual([f["suite"] for f in feeds],
                         ["bookworm", "bookworm-security", "bookworm-backports"])
        self.assertEqual(feeds[0]["uri"],
                         "http://snapshot.debian.org/archive/debian/20260101")

class FeedsAreOverriddenBySuite(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.loads("""
                distribution:
                    release: bookworm
                    feeds:
                        - suite: bookworm
                image:
                    filename: t.img
                    partitions:
                        - label: rootfs
                          where: /
        """)
        # Naming a suite that is already there settles it rather than
        # adding a second entry for it.
        build.loads("""
                distribution:
                    feeds:
                        - suite: bookworm
                          valid-until: false
        """)
        feeds = build.spec["distribution"]["feeds"]
        self.assertEqual(len(feeds), 1)
        self.assertEqual(feeds[0]["valid-until"], False)

class PackagesAreMergedByTheirSourcePackage(avocado.Test):
    def test(self):
        build = BuildCmd()
        # The suite pins the version and requires the kernel fragment,
        # which says which tree to graft on and has no opinion on the
        # version -- a real 'requires:' chain, not two peer files.
        with open(os.path.join(self.workdir, "kernel.yml"), "w") as f:
            f.write("""
                packages:
                    - source: apt://linux
                      extends:
                          kernel:
                              upstream: https://kernel.org/linux-6.18.43.tar.xz
                              flavour: amd64
                      profiles:
                          - noudeb
            """)
        suite = os.path.join(self.workdir, "suite.yml")
        with open(suite, "w") as f:
            f.write("""
                requires:
                    - kernel
                packages:
                    - source: apt://linux=6.12.95-1~bpo12+1
            """)
        build.load(suite)
        packages = build.spec["packages"]
        self.assertEqual(len(packages), 1)
        # The version the specification asked for survives being described
        # further, or a fragment could not be shared by two suites.
        self.assertEqual(packages[0]["source"], "apt://linux=6.12.95-1~bpo12+1")
        self.assertEqual(packages[0]["profiles"], ["noudeb"])
        self.assertEqual(packages[0]["extends"]["kernel"]["flavour"], "amd64")

class PackagesAreMergedInsideExtends(avocado.Test):
    def test(self):
        build = BuildCmd()
        # A board file requiring a generic kernel fragment -- a real
        # 'requires:' chain, not two peer files.
        with open(os.path.join(self.workdir, "kernel.yml"), "w") as f:
            f.write("""
                packages:
                    - source: apt://linux
                      extends:
                          kernel:
                              flavour: amd64
                              featureset: rt
            """)
        board = os.path.join(self.workdir, "board.yml")
        with open(board, "w") as f:
            f.write("""
                requires:
                    - kernel
                packages:
                    - source: apt://linux
                      extends:
                          kernel:
                              flavour: rpi
            """)
        build.load(board)
        kernel = build.spec["packages"][0]["extends"]["kernel"]
        # Settled one setting at a time rather than the 'kernel' entry
        # being replaced whole: what was said first stands, what is new
        # is added.
        self.assertEqual(kernel, {"flavour": "rpi", "featureset": "rt"})

class APeerFileAmendsAPackageByField(avocado.Test):
    # Two top-level files, as 'seine build a.yaml b.yaml' or a TUI
    # side-load would compose them -- the second amends the first field
    # by field rather than losing to it or replacing the entry whole.
    def test(self):
        build = BuildCmd()
        build.loads("""
                packages:
                    - source: apt://linux=6.12.95-1~bpo12+1
                      profiles:
                          - noudeb
        """)
        build.loads("""
                packages:
                    - source: apt://linux=6.12.101-1
        """)
        package = build.spec["packages"][0]
        self.assertEqual(package["source"], "apt://linux=6.12.101-1")
        self.assertEqual(package["profiles"], ["noudeb"])

class ARequiresFragmentDoesNotOverrideAPackageField(avocado.Test):
    # The 'requires:' counterpart of APeerFileAmendsAPackageByField --
    # a board still keeps its own pin over a fragment it requires.
    def test(self):
        build = BuildCmd()
        with open(os.path.join(self.workdir, "fragment.yml"), "w") as f:
            f.write("""
                packages:
                    - source: apt://linux=6.12.101-1
            """)
        board = os.path.join(self.workdir, "board.yml")
        with open(board, "w") as f:
            f.write("""
                requires:
                    - fragment
                packages:
                    - source: apt://linux=6.12.95-1~bpo12+1
                      profiles:
                          - noudeb
            """)
        build.load(board)
        package = build.spec["packages"][0]
        self.assertEqual(package["source"], "apt://linux=6.12.95-1~bpo12+1")
        self.assertEqual(package["profiles"], ["noudeb"])

class PackagesAreMergedByName(avocado.Test):
    def test(self):
        build = BuildCmd()
        # What asks for the build, naming the tree and what it is called.
        build.loads("""
                packages:
                    - source: git://github.com/NVIDIA/open-gpu-kernel-modules.git;rev=deadbeef
                      name: nvidia-open
                      version: 580.95.05
        """)
        # What adds to it, which knows the package by name and has no
        # opinion about the revision it is pinned to.
        build.loads("""
                packages:
                    - name: nvidia-open
                      profiles:
                          - nocheck
        """)
        packages = build.spec["packages"]
        self.assertEqual(len(packages), 1)
        self.assertEqual(packages[0]["version"], "580.95.05")
        self.assertEqual(packages[0]["profiles"], ["nocheck"])

class PackagesWithDifferentNamesAreNotMerged(avocado.Test):
    def test(self):
        build = BuildCmd()
        # One tree, two packages built from it: the name decides, so the
        # same 'source' twice is not one package when they are named
        # apart.
        build.loads("""
                packages:
                    - source: git://example.com/drivers.git;rev=deadbeef
                      name: driver-one
                    - source: git://example.com/drivers.git;rev=deadbeef
                      name: driver-two
        """)
        self.assertEqual([p["name"] for p in build.spec["packages"]],
                         ["driver-one", "driver-two"])

class ANamedPackageStillMergesBySource(avocado.Test):
    def test(self):
        build = BuildCmd()
        # A file that names no package still merges into one that does,
        # since the name it would have had is the one the URI gives.
        build.loads("""
                packages:
                    - source: apt://busybox
                      name: busybox
        """)
        build.loads("""
                packages:
                    - source: apt://busybox
                      profiles:
                          - nocheck
        """)
        self.assertEqual(len(build.spec["packages"]), 1)
        self.assertEqual(build.spec["packages"][0]["profiles"], ["nocheck"])

class DifferentPackagesAreLeftApart(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.loads("""
                packages:
                    - source: apt://busybox
        """)
        build.loads("""
                packages:
                    - source: git://example.com/linux.git;rev=deadbeef
                    - source: https://example.com/busybox_1.37.0-6.dsc
        """)
        packages = build.spec["packages"]
        # busybox is busybox wherever its source is fetched from; the
        # kernel is not busybox. Two peer files, so the second's source
        # amends the first's.
        self.assertEqual(len(packages), 2)
        self.assertEqual(packages[0]["source"],
                         "https://example.com/busybox_1.37.0-6.dsc")
        self.assertEqual(packages[1]["source"],
                         "git://example.com/linux.git;rev=deadbeef")

class VendorEntriesAreMergedByName(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.loads("""
                vendor:
                    - name: openssl
        """)
        build.loads("""
                vendor:
                    - name: openssl
                      arch: [armhf]
                    - name: zlib
        """)
        entries = build.spec["vendor"]
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["name"], "openssl")
        self.assertEqual(entries[0]["arch"], ["armhf"])
        self.assertEqual(entries[1]["name"], "zlib")

class VendorEntrySettingsAreFirstLoadedWins(avocado.Test):
    def test(self):
        build = BuildCmd()
        # A fragment naming the same entry again cannot override what the
        # file asking for it already pinned -- a real 'requires:' chain,
        # not two peer files.
        with open(os.path.join(self.workdir, "fragment.yml"), "w") as f:
            f.write("""
                vendor:
                    - name: openssl
                      version: ">=1.3"
            """)
        board = os.path.join(self.workdir, "board.yml")
        with open(board, "w") as f:
            f.write("""
                requires:
                    - fragment
                vendor:
                    - name: openssl
                      version: ">=1.2"
            """)
        build.load(board)
        self.assertEqual(build.spec["vendor"][0]["version"], ">=1.2")

class VendorExcludeIsAdditive(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.loads("vendor-exclude: [gcc-12]")
        build.loads("vendor-exclude: [gcc-12, texlive]")
        self.assertEqual(build.spec["vendor-exclude"], ["gcc-12", "texlive"])

# Unlike every other 'distribution:' setting (last-loaded wins),
# 'architectures:' is additive and deduplicated, the same as
# 'vendor-exclude:' above -- so a specification composing
# examples/vendor/amd64.yaml and .../arm64.yaml (each naming its own
# one) ends up with both, not whichever was required last.
# 'architecture' itself (singular) still overwrites.
class DistributionArchitecturesIsAdditive(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.loads("""
                distribution:
                    architecture: amd64
                    architectures:
                        - amd64
        """)
        build.loads("""
                distribution:
                    architecture: arm64
                    architectures:
                        - arm64
        """)
        self.assertEqual(build.spec["distribution"]["architecture"], "arm64")
        self.assertEqual(build.spec["distribution"]["architectures"],
                         ["amd64", "arm64"])

        # Naming the same architecture again does not duplicate it.
        build.loads("""
                distribution:
                    architectures:
                        - arm64
        """)
        self.assertEqual(build.spec["distribution"]["architectures"],
                         ["amd64", "arm64"])

    def test_vendor_example_architectures(self):
        vendor_dir = os.path.join(path_to_sources, "examples", "vendor")
        common_dir = os.path.join(path_to_sources, "examples", "common")

        # common/amd64.yaml and common/arm64.yaml only set singular architecture
        amd64 = BuildCmd().load(os.path.join(common_dir, "amd64.yaml"))
        self.assertEqual(amd64["distribution"]["architecture"], "amd64")
        self.assertNotIn("architectures", amd64["distribution"])

        arm64 = BuildCmd().load(os.path.join(common_dir, "arm64.yaml"))
        self.assertEqual(arm64["distribution"]["architecture"], "arm64")
        self.assertNotIn("architectures", arm64["distribution"])

        # vendor/amd64.yaml and vendor/arm64.yaml augment architectures
        vendor_build = BuildCmd()
        vendor_build.load(os.path.join(vendor_dir, "amd64.yaml"))
        vendor_build.load(os.path.join(vendor_dir, "arm64.yaml"))
        self.assertEqual(vendor_build.spec["distribution"]["architecture"], "arm64")
        self.assertEqual(vendor_build.spec["distribution"]["architectures"],
                         ["amd64", "arm64"])


class FilesAreResolvedAgainstTheFileThatListedThem(avocado.Test):
    def test(self):
        build = BuildCmd()
        # A package described by two files in two directories: each names
        # its own files, and merging must not leave one of them looking
        # for the other's.
        for dirname in ["boards/rpi", "kernels/6.18"]:
            os.makedirs(os.path.join(self.workdir, dirname), exist_ok=True)
        with open(os.path.join(self.workdir, "boards/rpi/board.yml"), "w") as f:
            f.write("packages:\n"
                    "    - source: apt://linux\n"
                    "      patches:\n"
                    "          - patches/0001-board.patch\n")
        with open(os.path.join(self.workdir, "kernels/6.18/kernel.yml"), "w") as f:
            f.write("packages:\n"
                    "    - source: apt://linux\n"
                    "      extends:\n"
                    "          kernel:\n"
                    "              fragments:\n"
                    "                  - configs/slim.fragment\n")
        build.load(os.path.join(self.workdir, "boards/rpi/board.yml"))
        build.load(os.path.join(self.workdir, "kernels/6.18/kernel.yml"))

        package = build.spec["packages"][0]
        self.assertEqual(package["patches"],
                         [os.path.join(self.workdir,
                                       "boards/rpi/patches/0001-board.patch")])
        self.assertEqual(package["extends"]["kernel"]["fragments"],
                         [os.path.join(self.workdir,
                                       "kernels/6.18/configs/slim.fragment")])

class DefaultsDescribeAPackageWithoutBuildingIt(avocado.Test):
    def test(self):
        build = BuildCmd()
        # An architecture file, saying which kernel of it is meant.
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
        build.parse()
        # Nothing asked for a kernel, so nothing is built.
        self.assertEqual(build.spec.get("packages"), None)
