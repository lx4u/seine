#!/usr/bin/env python3
# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import atexit
import avocado
import os
import shutil
import sys
import tempfile

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

import seine.extends.go

from seine.extends import templates

from seine.build import BuildCmd
from seine.utils import HOST_ARCH

os.environ["SEINE_CACHE_DIR"] = tempfile.mkdtemp(prefix="seine-tests-")
os.environ.pop("SEINE_SIGN_KEY", None)
atexit.register(shutil.rmtree, os.environ["SEINE_CACHE_DIR"], ignore_errors=True)

IMAGE = """
                image:
                    filename: packages-test.img
                    partitions:
                        - label: rootfs
                          where: /
"""

def parse_for(architecture, packages):
    build = BuildCmd()
    build.loads("""
                distribution:
                    release: trixie
                    architecture: %s
    """ % architecture + packages + IMAGE)
    build.parse()
    return build

K3S = """
                packages:
                    - source: git://github.com/k3s-io/k3s.git;rev=v1.30.4+k3s1
                      version: "1.30.4"
                      extends:
                          go:
%s
"""

TOOLCHAIN_SHA256 = "905a297f19ead44780548933e0ff1a1b86e8327bb459e92f9c0012569f76f5e3"

# The digest for the machine running the tests.
TOOLCHAIN_SHA256_YAML = ("toolchain-sha256: {%s: \"%s\"}"
                        % (HOST_ARCH, TOOLCHAIN_SHA256))

class GoExtension(avocado.Test):
    def test(self):
        build = parse_for("amd64", K3S % ("""
                              toolchain: "1.23.0"
                              %s
                              commands:
                                  - {package: "./cmd/k3s", binary: k3s}
                              ldflags: "-s -w"
                              build-depends: [libbtrfs-dev]
                              runtime-depends: [iptables]
                              runtime-suggests: [ca-certificates]
        """ % TOOLCHAIN_SHA256_YAML))
        package = build.image.packages[0]
        self.assertIn("go", package.ext)
        self.assertEqual(package.ext["go"].toolchain, "1.23.0")
        self.assertEqual(package.ext["go"].toolchain_sha256, {HOST_ARCH: TOOLCHAIN_SHA256})
        self.assertEqual(package.ext["go"].commands, [{
            "package": "./cmd/k3s", "binary": "k3s",
            "links": [], "alternatives": []}])
        self.assertEqual(package.ext["go"].ldflags, "-s -w")
        self.assertEqual(package.ext["go"].build_depends, ["libbtrfs-dev"])
        self.assertEqual(package.ext["go"].runtime_depends, ["iptables"])
        self.assertEqual(package.ext["go"].runtime_suggests, ["ca-certificates"])
        self.assertEqual(package.ext["go"].cgo, False)
        self.assertEqual(package.ext["go"].build, ".")

class GoToolchainSha256IsPerHostArchitecture(avocado.Test):
    def test(self):
        build = parse_for("amd64", K3S % """
                              toolchain: "1.23.0"
                              toolchain-sha256:
                                  amd64: 905a297f19ead44780548933e0ff1a1b86e8327bb459e92f9c0012569f76f5e3
                                  arm64: 62788056693009bcf7020eedc778cdd1781941c6145eab7688bd087bce0f8659
                              commands:
                                  - {package: "./cmd/k3s", binary: k3s}
        """)
        package = build.image.packages[0]
        self.assertEqual(package.ext["go"].toolchain_sha256, {
            "amd64": "905a297f19ead44780548933e0ff1a1b86e8327bb459e92f9c0012569f76f5e3",
            "arm64": "62788056693009bcf7020eedc778cdd1781941c6145eab7688bd087bce0f8659",
        })

class GoToolchainSha256RejectsAnUnknownArchitecture(avocado.Test):
    def test(self):
        with self.assertRaises(ValueError):
            parse_for("amd64", K3S % """
                              toolchain: "1.23.0"
                              toolchain-sha256:
                                  not-an-architecture: %s
                              commands:
                                  - {package: "./cmd/k3s", binary: k3s}
            """ % TOOLCHAIN_SHA256)

class FetchingWithNoDigestForThisMachineIsRefused(avocado.Test):
    def test(self):
        build = parse_for("amd64", K3S % """
                              toolchain: "1.23.0"
                              toolchain-sha256:
                                  riscv64: 62788056693009bcf7020eedc778cdd1781941c6145eab7688bd087bce0f8659
                              commands:
                                  - {package: "./cmd/k3s", binary: k3s}
        """)
        package = build.image.packages[0]
        # Only an error when a toolchain is fetched: another machine may build it.
        with self.assertRaises(ValueError):
            seine.extends.go._toolchain_sha256(package)

class CrossBuildEssentialIsNeededEvenForPureGo(avocado.Test):
    # dh_strip needs a cross binutils even when cgo is off.
    def test(self):
        from seine.packages import Builder
        from seine.sbuild import BuilderImage
        other = "arm64" if HOST_ARCH != "arm64" else "amd64"
        distro = {"source": "debian", "release": "trixie",
                  "architecture": other, "uri": "http://example.com/debian"}
        builder = Builder(distro, {}, BuilderImage(distro, {}))
        build = parse_for(other, K3S % ("""
                              toolchain: "1.23.0"
                              %s
                              commands:
                                  - {package: "./cmd/k3s", binary: k3s}
        """ % TOOLCHAIN_SHA256_YAML))
        package = build.image.packages[0]
        self.assertEqual(package.ext["go"].cgo, False)
        self.assertEqual(seine.extends.go._cross_architectures(builder, package), [other])

class GoDefaults(avocado.Test):
    def test(self):
        build = parse_for("amd64", K3S % ("""
                              toolchain: "1.23.0"
                              %s
                              commands:
                                  - {package: "./cmd/k3s", binary: k3s}
        """ % TOOLCHAIN_SHA256_YAML))
        package = build.image.packages[0]
        self.assertEqual(package.ext["go"].ldflags, "")
        self.assertEqual(package.ext["go"].tags, "")
        self.assertEqual(package.ext["go"].build_depends, [])
        self.assertEqual(package.ext["go"].runtime_depends, [])
        self.assertEqual(package.ext["go"].runtime_suggests, [])

class NonGoPackageHasNoGoSettings(avocado.Test):
    def test(self):
        build = parse_for("amd64", """
                packages:
                    - source: apt://busybox
        """ + IMAGE)
        self.assertNotIn("go", build.image.packages[0].ext)

class GoRequiresAToolchain(avocado.Test):
    def test(self):
        with self.assertRaises(ValueError):
            parse_for("amd64", K3S % ("""
                              %s
                              commands:
                                  - {package: "./cmd/k3s", binary: k3s}
            """ % TOOLCHAIN_SHA256_YAML))

class GoRequiresACheckedToolchain(avocado.Test):
    def test(self):
        with self.assertRaises(ValueError):
            parse_for("amd64", K3S % """
                              toolchain: "1.23.0"
                              toolchain-sha256: {%s: "not-a-digest"}
                              commands:
                                  - {package: "./cmd/k3s", binary: k3s}
            """ % HOST_ARCH)

class GoRequiresNonEmptyCommands(avocado.Test):
    def test(self):
        with self.assertRaises(ValueError):
            parse_for("amd64", K3S % ("""
                              toolchain: "1.23.0"
                              %s
                              commands: []
            """ % TOOLCHAIN_SHA256_YAML))

class GoRejectsAPathAsABinaryName(avocado.Test):
    def test(self):
        with self.assertRaises(ValueError):
            parse_for("amd64", K3S % ("""
                              toolchain: "1.23.0"
                              %s
                              commands:
                                  - {package: "./cmd/k3s", binary: "usr/bin/k3s"}
            """ % TOOLCHAIN_SHA256_YAML))

class GoRequiresVersion(avocado.Test):
    def test(self):
        with self.assertRaises(ValueError):
            parse_for("amd64", ("""
                packages:
                    - source: git://github.com/k3s-io/k3s.git;rev=v1.30.4+k3s1
                      extends:
                          go:
                              toolchain: "1.23.0"
                              %s
                              commands:
                                  - {package: "./cmd/k3s", binary: k3s}
            """ % TOOLCHAIN_SHA256_YAML) + IMAGE)

# Renders the templates with a made-up context: no network, no container.
class GoTemplatesRender(avocado.Test):
    def test(self):
        found, _ = seine.extends.go.go_packaging()
        context = {
            "name": "k3s", "version": "1.30.4", "note": "Packaged.",
            "source": "git://github.com/k3s-io/k3s.git;rev=v1.30.4+k3s1",
            "maintainer": "seine", "email": "seine@example.com",
            "date": "Mon, 01 Jan 2024 00:00:00 +0000",
            "build_dir": ".", "toolchain": "1.23.0", "host_arch": "amd64",
            "toolchain_name": "1.23.0-amd64-905a297f19ead447",
            "commands": [{"package": "./cmd/k3s", "binary": "k3s"}],
            "links": [], "alternatives": [],
            "cgo": False, "cross_architectures": ["arm64"],
            "ldflags": "-s -w", "tags": "",
            "build_depends": ["libbtrfs-dev"], "runtime_depends": ["iptables"],
            "runtime_suggests": ["ca-certificates"],
            "goarch": sorted(seine.extends.go.GOARCH.items()),
        }
        for name in templates.FILES:
            rendered = templates.TEMPLATE.from_string(found[name]).render(context)
            self.assertIn("k3s", rendered)
        control = templates.TEMPLATE.from_string(found["control"]).render(context)
        self.assertIn("crossbuild-essential-arm64:native <cross>", control)
        self.assertIn("libbtrfs-dev", control)
        self.assertIn("Suggests: ca-certificates", control)
        rules = templates.TEMPLATE.from_string(found["rules"]).render(context)
        self.assertIn("GOROOT = /goroot/1.23.0-amd64-905a297f19ead447/go", rules)
        self.assertIn("./cmd/k3s", rules)

def go_with(commands):
    return parse_for("amd64", K3S % ("""
                              toolchain: "1.23.0"
                              %s
                              commands: %s
    """ % (TOOLCHAIN_SHA256_YAML, commands)))

class GoLinks(avocado.Test):
    def test_names_and_absolute_paths(self):
        build = go_with("""
                                  - package: ./cmd/server
                                    binary: k3s
                                    links: [kubectl, /usr/sbin/ctr]""")
        command = build.image.packages[0].ext["go"].commands[0]
        self.assertEqual(command["links"], ["kubectl", "/usr/sbin/ctr"])
        self.assertEqual(command["alternatives"], [])

    def test_a_relative_path_is_refused(self):
        with self.assertRaises(ValueError):
            go_with("""
                                  - {package: ./cmd/server, binary: k3s, links: [bin/kubectl]}""")

    def test_usr_local_is_refused(self):
        with self.assertRaises(ValueError):
            go_with("""
                                  - {package: ./cmd/server, binary: k3s, links: [/usr/local/bin/kubectl]}""")

    def test_a_link_cannot_climb_out(self):
        with self.assertRaises(ValueError):
            go_with("""
                                  - {package: ./cmd/server, binary: k3s, links: ["/usr/../etc/x"]}""")

    def test_two_files_at_one_path_are_refused(self):
        with self.assertRaises(ValueError):
            go_with("""
                                  - {package: ./cmd/server, binary: k3s, links: [kubectl, /usr/bin/kubectl]}""")

    def test_a_link_cannot_be_the_binary(self):
        with self.assertRaises(ValueError):
            go_with("""
                                  - {package: ./cmd/server, binary: k3s, links: [k3s]}""")

class GoAlternatives(avocado.Test):
    def test_both_forms_and_a_default_priority(self):
        build = go_with("""
                                  - package: ./cmd/server
                                    binary: k3s
                                    links: [ctr]
                                    alternatives:
                                        - kubectl
                                        - {link: /usr/sbin/crictl, priority: 10}""")
        command = build.image.packages[0].ext["go"].commands[0]
        self.assertEqual(command["links"], ["ctr"])
        self.assertEqual(command["alternatives"], [
            {"link": "kubectl", "priority": 50},
            {"link": "/usr/sbin/crictl", "priority": 10}])

    def test_priority_is_a_number(self):
        with self.assertRaises(ValueError):
            go_with("""
                                  - {package: ./cmd/server, binary: k3s, alternatives: [{link: kubectl, priority: high}]}""")

    def test_a_link_is_needed(self):
        with self.assertRaises(ValueError):
            go_with("""
                                  - {package: ./cmd/server, binary: k3s, alternatives: [{priority: 10}]}""")

    def test_a_link_and_an_alternative_cannot_share_a_path(self):
        with self.assertRaises(ValueError):
            go_with("""
                                  - {package: ./cmd/server, binary: k3s, links: [kubectl], alternatives: [kubectl]}""")

class GoLinksAreRelative(avocado.Test):
    def test(self):
        plain, alternatives = seine.extends.go.links_of([
            {"binary": "k3s", "links": ["kubectl", "/usr/sbin/ctr"],
             "alternatives": []},
            {"binary": "tool", "links": [],
             "alternatives": [{"link": "t", "priority": 7}]}])
        self.assertEqual([(l["path"], l["target"]) for l in plain], [
            ("/usr/bin/kubectl", "k3s"),
            ("/usr/sbin/ctr", "../bin/k3s")])
        self.assertEqual([(l["name"], l["priority"]) for l in alternatives],
                         [("t", 7)])

class GoLinksAndAlternativesRender(avocado.Test):
    def test(self):
        found, _ = seine.extends.go.go_packaging()
        commands = [
            {"package": "./cmd/server", "binary": "k3s",
             "links": ["kubectl", "/usr/sbin/ctr"], "alternatives": []},
            {"package": "./cmd/x", "binary": "x", "links": [],
             "alternatives": [{"link": "y", "priority": 20}]}]
        plain, alternatives = seine.extends.go.links_of(commands)
        context = {
            "name": "k3s", "version": "1", "source": "s", "note": "n",
            "maintainer": "m",
            "email": "e", "date": "d", "build_dir": ".", "toolchain": "1.23.0",
            "toolchain_name": "1.23.0-amd64-x", "host_arch": "amd64", "commands": commands, "links": plain,
            "alternatives": alternatives, "cgo": False,
            "cross_architectures": [], "ldflags": "", "tags": "",
            "build_depends": [], "runtime_depends": [], "runtime_suggests": [],
            "goarch": sorted(seine.extends.go.GOARCH.items()),
        }
        render = lambda name: templates.TEMPLATE.from_string(
            found[name]).render(context)
        rules = render("rules")
        self.assertIn("ln -s k3s debian/k3s/usr/bin/kubectl", rules)
        self.assertIn("ln -s ../bin/k3s debian/k3s/usr/sbin/ctr", rules)
        self.assertNotIn("debian/k3s/usr/bin/y", rules)
        postinst = render("postinst")
        self.assertIn("update-alternatives --install /usr/bin/y y /usr/bin/x 20",
                      postinst)
        self.assertIn("#DEBHELPER#", postinst)
        self.assertIn("update-alternatives --remove y /usr/bin/x", render("prerm"))

def stamp_of(sha256_yaml, architecture="amd64"):
    from seine.packages import Builder
    from seine.sbuild import BuilderImage
    distro = {"source": "debian", "release": "trixie",
              "architecture": architecture, "uri": "http://example.com/debian"}
    builder = Builder(distro, {}, BuilderImage(distro, {}))
    build = parse_for(architecture, K3S % ("""
                              toolchain: "1.23.0"
                              toolchain-sha256: %s
                              commands:
                                  - {package: "./cmd/k3s", binary: k3s}
    """ % sha256_yaml))
    return builder.stamp(build.image.packages[0], architecture)

class GoStampUsesOnlyThisMachinesDigest(avocado.Test):
    def digests(self, mine, other):
        other_arch = "arm64" if HOST_ARCH != "arm64" else "amd64"
        return "{%s: '%s', %s: '%s'}" % (HOST_ARCH, mine * 64, other_arch, other * 64)

    def test_another_architectures_digest_is_ignored(self):
        self.assertEqual(stamp_of(self.digests("a", "b")),
                         stamp_of(self.digests("a", "c")))

    def test_this_machines_digest_is_used(self):
        self.assertNotEqual(stamp_of(self.digests("a", "b")),
                            stamp_of(self.digests("c", "b")))

class GoToolchainDirectoryFollowsTheDigest(avocado.Test):
    def name_for(self, digest):
        build = parse_for("amd64", K3S % ("""
                              toolchain: "1.23.0"
                              toolchain-sha256: {%s: '%s'}
                              commands:
                                  - {package: "./cmd/k3s", binary: k3s}
        """ % (HOST_ARCH, digest * 64)))
        return seine.extends.go.toolchain_name(build.image.packages[0])

    def test(self):
        self.assertEqual(self.name_for("a"), "1.23.0-%s-%s" % (HOST_ARCH, "a" * 16))
        self.assertNotEqual(self.name_for("a"), self.name_for("b"))
