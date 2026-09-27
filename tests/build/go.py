#!/usr/bin/env python3
# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import atexit
import avocado
import json
import os
import shutil
import sys
import tempfile

from unittest import mock

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

import seine.extends.go

from seine.extends import registry
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
                              cgo-cflags: "-DA=1 -DB=1"
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
        self.assertEqual(package.ext["go"].cgo_cflags, "-DA=1 -DB=1")
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
        self.assertEqual(package.ext["go"].cgo_cflags, "")
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
            "cgo": True, "cgo_cflags": "-DA=1",
            "cross_architectures": ["arm64"],
            "ldflags": "-s -w", "tags": "",
            "build_depends": ["libbtrfs-dev"], "runtime_depends": ["iptables"],
            "runtime_suggests": ["ca-certificates"],
            "goarch": sorted(seine.extends.go.GOARCH.items()),
            "license_scan": False,
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
        self.assertIn("export CGO_CFLAGS = -DA=1", rules)

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
            "cgo_cflags": "",
            "cross_architectures": [], "ldflags": "", "tags": "",
            "build_depends": [], "runtime_depends": [], "runtime_suggests": [],
            "goarch": sorted(seine.extends.go.GOARCH.items()),
            "license_scan": False,
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

UNIT = """[Unit]
Description=k3s

[Service]
ExecStart=/usr/bin/k3s server

[Install]
WantedBy=multi-user.target
"""

def with_unit(unit=None, commands=None):
    commands = commands or '[{package: "./cmd/k3s", binary: k3s}]'
    settings = f"""
                              toolchain: "1.23.0"
                              {TOOLCHAIN_SHA256_YAML}
                              commands: {commands}
"""
    if unit is not None:
        settings += "                              systemd-unit: |\n" + "".join(
            f"                                  {line}\n"
            for line in unit.splitlines())
    return parse_for("amd64", K3S % settings).image.packages[0]

class GoSystemdUnit(avocado.Test):
    def written(self, package):
        from seine.packages import Builder
        from seine.sbuild import BuilderImage
        distro = {"source": "debian", "release": "trixie",
                  "architecture": "amd64", "uri": "http://example.com/debian"}
        builder = Builder(distro, {}, BuilderImage(distro, {}))
        builder.packages = [package]
        # A tree with its modules already vendored has nothing to vendor.
        os.makedirs(os.path.join(self.workdir, "vendor"))
        with mock.patch("seine.extends.go.fetch_toolchain"):
            seine.extends.go.extend(builder, package, self.workdir, 946684800)
        return os.path.join(self.workdir, "debian")

    def test_it_is_optional(self):
        self.assertIsNone(with_unit().ext["go"].systemd_unit)
        debian = self.written(with_unit())
        self.assertEqual(sorted(os.listdir(debian)),
                         ["changelog", "control", "rules", "source"])

    def test_it_is_written_for_debhelper(self):
        debian = self.written(with_unit(UNIT))
        with open(os.path.join(debian, "k3s.service")) as f:
            self.assertEqual(f.read(), UNIT)

    def test_it_may_be_a_file(self):
        path = os.path.join(self.workdir, "k3s.service")
        with open(path, "w") as f:
            f.write(UNIT)
        package = parse_for("amd64", K3S % f"""
                              toolchain: "1.23.0"
                              {TOOLCHAIN_SHA256_YAML}
                              commands: [{{package: "./cmd/k3s", binary: k3s}}]
                              systemd-unit: file://{path}
        """).image.packages[0]
        self.assertIn(path, package.referenced_files())
        debian = self.written(package)
        with open(os.path.join(debian, "k3s.service")) as f:
            self.assertEqual(f.read(), UNIT)

    def test_it_must_not_be_empty(self):
        with self.assertRaises(ValueError) as refused:
            parse_for("amd64", K3S % ("""
                              toolchain: "1.23.0"
                              %s
                              commands: [{package: "./cmd/k3s", binary: k3s}]
                              systemd-unit: ""
            """ % TOOLCHAIN_SHA256_YAML))
        self.assertIn("'extends: go: systemd-unit'", str(refused.exception))

    def test_a_unit_alone_leaves_the_maintainer_scripts_to_debhelper(self):
        debian = self.written(with_unit(UNIT))
        self.assertFalse(os.path.exists(os.path.join(debian, "k3s.postinst")))

    def test_the_scripts_with_alternatives_still_let_debhelper_in(self):
        debian = self.written(with_unit(UNIT, '[{package: "./cmd/k3s", '
                                              'binary: k3s, alternatives: [kubectl]}]'))
        for script in ["k3s.postinst", "k3s.prerm"]:
            with open(os.path.join(debian, script)) as f:
                text = f.read()
            self.assertIn("update-alternatives", text)
            self.assertIn("#DEBHELPER#", text)

    def test_the_unit_is_part_of_the_stamp(self):
        self.assertNotEqual(
            stamp_of_package(with_unit(UNIT)),
            stamp_of_package(with_unit(UNIT.replace("k3s server", "k3s agent"))))
        self.assertNotEqual(stamp_of_package(with_unit()),
                            stamp_of_package(with_unit(UNIT)))

def stamp_of_package(package):
    from seine.packages import Builder
    from seine.sbuild import BuilderImage
    distro = {"source": "debian", "release": "trixie",
              "architecture": "amd64", "uri": "http://example.com/debian"}
    builder = Builder(distro, {}, BuilderImage(distro, {}))
    return os.path.basename(builder.stamp(package)).rsplit("_", 1)[1]

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

URL = "https://example.com/chart.tgz;sha256sum=" + "a" * 64

def with_files(files):
    settings = f"""
                              toolchain: "1.23.0"
                              {TOOLCHAIN_SHA256_YAML}
                              commands: [{{package: "./cmd/k3s", binary: k3s}}]
                              files: {files}
"""
    return parse_for("amd64", K3S % settings).image.packages[0]

class GoFiles(avocado.Test):
    def test_they_are_optional(self):
        self.assertEqual(with_files("[]").ext["go"].files, [])

    def test_a_path_and_a_download_are_kept(self):
        package = with_files(
            f"[{{from: manifests, to: pkg/embed}}, {{url: '{URL}', to: charts/c.tgz}}]")
        self.assertEqual(package.ext["go"].files, [
            {"from": "manifests", "to": "pkg/embed"},
            {"url": URL, "to": "charts/c.tgz"}])

    def test_a_source_is_needed(self):
        with self.assertRaises(ValueError):
            with_files("[{to: pkg/embed}]")

    def test_one_source_only(self):
        with self.assertRaises(ValueError):
            with_files(f"[{{from: a, url: '{URL}', to: b}}]")

    def test_a_destination_is_needed(self):
        with self.assertRaises(ValueError):
            with_files("[{from: a}]")

    def test_a_download_needs_its_hash(self):
        with self.assertRaises(ValueError):
            with_files("[{url: 'https://example.com/chart.tgz', to: b}]")

    def test_a_download_is_http(self):
        with self.assertRaises(ValueError):
            with_files("[{url: 'file:///etc/passwd;sha256sum=%s', to: b}]" % ("a" * 64))

    def test_paths_stay_inside_the_tree(self):
        for path in ("/etc/x", "../x", "a/../../x", "debian/x", "."):
            with self.assertRaises(ValueError, msg=path):
                with_files(f"[{{from: a, to: '{path}'}}]")

    def test_they_are_part_of_the_stamp(self):
        self.assertNotEqual(
            stamp_of_package(with_files("[{from: a, to: b}]")),
            stamp_of_package(with_files("[{from: a, to: c}]")))
        self.assertNotEqual(
            stamp_of_package(with_files("[]")),
            stamp_of_package(with_files("[{from: a, to: b}]")))

class GoFilesAreStaged(avocado.Test):
    def stage(self, files):
        package = with_files(files)
        with mock.patch("seine.extends.texts.fetch") as fetch:
            fetch.return_value = os.path.join(self.workdir, "downloaded")
            seine.extends.go._stage(None, package, os.path.join(self.workdir, "tree"))
        return package

    def setUp(self):
        tree = os.path.join(self.workdir, "tree")
        os.makedirs(os.path.join(tree, "manifests", "sub"))
        for name in ("manifests/a.yaml", "manifests/sub/b.yaml", "one.txt"):
            with open(os.path.join(tree, name), "w") as f:
                f.write(name)
        with open(os.path.join(self.workdir, "downloaded"), "w") as f:
            f.write("chart")

    def read(self, name):
        with open(os.path.join(self.workdir, "tree", name)) as f:
            return f.read()

    def test_a_directory_is_copied_into_another(self):
        self.stage("[{from: manifests, to: pkg/deploy/embed}]")
        self.assertEqual(self.read("pkg/deploy/embed/a.yaml"), "manifests/a.yaml")
        self.assertEqual(self.read("pkg/deploy/embed/sub/b.yaml"), "manifests/sub/b.yaml")

    def test_a_file_is_copied(self):
        self.stage("[{from: one.txt, to: pkg/two.txt}]")
        self.assertEqual(self.read("pkg/two.txt"), "one.txt")

    def test_a_download_is_copied(self):
        self.stage(f"[{{url: '{URL}', to: pkg/static/embed/charts/c.tgz}}]")
        self.assertEqual(self.read("pkg/static/embed/charts/c.tgz"), "chart")

    def test_it_can_be_done_twice(self):
        for _ in range(2):
            self.stage("[{from: manifests, to: pkg/embed}]")
        self.assertEqual(self.read("pkg/embed/a.yaml"), "manifests/a.yaml")

    def test_a_link_cannot_lead_out_of_the_tree(self):
        os.symlink(self.workdir, os.path.join(self.workdir, "tree", "out"))
        with self.assertRaises(ValueError):
            self.stage("[{from: one.txt, to: out/x}]")

def defaults_of(license_scan):
    settings = {"toolchain": "1.23.0",
               "toolchain-sha256": {HOST_ARCH: TOOLCHAIN_SHA256}}
    if license_scan is not None:
        settings["license-scan"] = license_scan
    return settings

class GoDefaultsCoverLicenseScan(avocado.Test):
    def test_it_is_required_alongside_the_toolchain(self):
        with self.assertRaises(ValueError):
            registry.check_defaults({"go": defaults_of(None)})

    def test_it_must_be_a_bool(self):
        with self.assertRaises(ValueError):
            registry.check_defaults({"go": defaults_of("yes")})

    def test_it_is_accepted(self):
        registry.check_defaults({"go": defaults_of(True)})

DEP5 = """Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: k3s

Files: *
Copyright: 2019-2024 Rancher Labs, Inc.
License: Apache-2.0
"""

MODULES_TXT = """# github.com/foo/bar v1.2.3
## explicit; go 1.21
github.com/foo/bar/baz
"""

SYFT_DOC = {
    "packages": [{
        "SPDXID": "SPDXRef-Package-go-module-foo-bar",
        "name": "github.com/foo/bar",
        "versionInfo": "v1.2.3",
        "licenseConcluded": "MIT",
        "copyrightText": "Copyright 2020 Foo Bar",
        "externalRefs": [{
            "referenceType": "purl",
            "referenceLocator": "pkg:golang/github.com/foo/bar@v1.2.3",
        }],
    }],
    "relationships": [],
}

def go_with_scan(copyright_text, license_scan="true", commands=None):
    commands = commands or '[{package: "./cmd/k3s", binary: k3s}]'
    settings = f"""
                              toolchain: "1.23.0"
                              {TOOLCHAIN_SHA256_YAML}
                              commands: {commands}
                              license-scan: {license_scan}
"""
    if copyright_text is not None:
        settings += "                              copyright: |\n" + "".join(
            f"                                  {line}\n"
            for line in copyright_text.splitlines())
    return parse_for("amd64", K3S % settings).image.packages[0]

class GoLicenseScan(avocado.Test):
    def written(self, package, syft_doc=SYFT_DOC, modules_txt=MODULES_TXT,
               go_mod=None):
        from seine.packages import Builder
        from seine.sbuild import BuilderImage
        distro = {"source": "debian", "release": "trixie",
                  "architecture": "amd64", "uri": "http://example.com/debian"}
        builder = Builder(distro, {}, BuilderImage(distro, {}))
        builder.packages = [package]
        os.makedirs(os.path.join(self.workdir, "vendor"), exist_ok=True)
        with open(os.path.join(self.workdir, "vendor", "modules.txt"), "w") as f:
            f.write(modules_txt)
        if go_mod is not None:
            with open(os.path.join(self.workdir, "go.mod"), "w") as f:
                f.write(go_mod)
        with mock.patch("seine.extends.go.fetch_toolchain"), \
             mock.patch("seine.extends.go_licenses.scan_tree",
                        return_value=syft_doc):
            seine.extends.go.extend(builder, package, self.workdir, 946684800)
        return os.path.join(self.workdir, "debian")

    def test_it_is_off_by_default(self):
        package = go_with("""[{package: "./cmd/k3s", binary: k3s}]""").image.packages[0]
        debian = self.written(package)
        self.assertFalse(
            os.path.exists(os.path.join(debian, "go-sbom.spdx.json")))

    def test_it_appends_a_stanza_per_vendored_module(self):
        debian = self.written(go_with_scan(DEP5))
        with open(os.path.join(debian, "copyright")) as f:
            text = f.read()
        self.assertIn("Format: https://www.debian.org", text)
        self.assertIn("Files: vendor/github.com/foo/bar/*", text)
        self.assertIn("License: MIT", text)
        self.assertIn("Copyright: Copyright 2020 Foo Bar", text)

    def test_it_writes_the_sbom_fragment(self):
        debian = self.written(go_with_scan(DEP5))
        with open(os.path.join(debian, "go-sbom.spdx.json")) as f:
            self.assertEqual(json.load(f), SYFT_DOC)

    def test_an_unclassified_module_is_flagged_for_review(self):
        debian = self.written(go_with_scan(DEP5),
                              syft_doc={"packages": [], "relationships": []})
        with open(os.path.join(debian, "copyright")) as f:
            text = f.read()
        self.assertIn("License: NOASSERTION", text)

    def test_copyright_must_already_be_dep5(self):
        package = go_with_scan("Just some free text, not DEP-5.\n")
        with self.assertRaises(ValueError) as refused:
            self.written(package)
        self.assertIn("license-scan", str(refused.exception))

    def test_it_generates_a_top_level_stanza_when_copyright_is_absent(self):
        syft_doc = {
            "packages": SYFT_DOC["packages"] + [{
                "SPDXID": "SPDXRef-Package-go-module-myapp",
                "name": "github.com/example/myapp",
                "licenseConcluded": "MIT",
                "copyrightText": "Copyright 2024 Example Corp",
                "externalRefs": [{
                    "referenceType": "purl",
                    "referenceLocator": "pkg:golang/github.com/example/myapp",
                }],
            }],
            "relationships": [],
        }
        debian = self.written(go_with_scan(None), syft_doc=syft_doc,
                              go_mod="module github.com/example/myapp\n\ngo 1.21\n")
        with open(os.path.join(debian, "copyright")) as f:
            text = f.read()
        self.assertIn("Format: https://www.debian.org", text)
        self.assertIn("Files: *\n", text)
        self.assertIn("Copyright: Copyright 2024 Example Corp", text)
        self.assertIn("Files: vendor/github.com/foo/bar/*", text)

    def test_calling_extend_twice_does_not_double_the_stanzas(self):
        package = go_with_scan(DEP5)
        first = self.written(package)
        with open(os.path.join(first, "copyright")) as f:
            once = f.read()
        twice = self.written(package)
        with open(os.path.join(twice, "copyright")) as f:
            self.assertEqual(f.read(), once)
