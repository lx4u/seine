# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# 'extends: go:' builds a Go program from source into a Debian package,
# with packaging seine writes for it, as 'extends: module:' does.
# It uses a pinned upstream Go toolchain, not the one in the distribution.

import json
import os
import re
import types

from seine.container import ContainerEngine
from seine.extends   import parsing
from seine.extends   import templates
from seine.utils     import HOST_ARCH
from seine.utils     import WORKDIR

# Bump when this code changes what a Go build is built into.
REVISION = 1

# Given together, so 'defaults: extends: go:' can set them for every package.
DEFAULTS = ["toolchain", "toolchain-sha256"]

SETTINGS = ["build", "build-depends", "cgo", "commands", "ldflags",
            "runtime-depends", "runtime-suggests", "tags", "toolchain",
            "toolchain-sha256"]

# Debian architecture -> Go names. The generated rules pick from this
# with $(DEB_HOST_ARCH), as the target may not be the machine's own.
GOARCH = {
    "amd64":   {"GOARCH": "amd64"},
    "arm64":   {"GOARCH": "arm64"},
    "armel":   {"GOARCH": "arm", "GOARM": "5"},
    "armhf":   {"GOARCH": "arm", "GOARM": "7"},
    "i386":    {"GOARCH": "386"},
    "ppc64el": {"GOARCH": "ppc64le"},
    "riscv64": {"GOARCH": "riscv64"},
    "s390x":   {"GOARCH": "s390x"},
}

SHA256 = re.compile(r"^[0-9a-f]{64}$")

TOOLCHAIN_URL = "https://go.dev/dl/go%s.linux-%s.tar.gz"

# Written once a toolchain is fully unpacked, so a half-done unpack
# is never reused.
FETCHED = ".seine-fetched"

# Toolchains are kept here. The same directory is mounted in the sbuild
# chroot, whose build phase has no network to fetch one.
def toolchain_root():
    return ContainerEngine.cache("go-toolchains")

# The digest is in the name, so a changed digest never reuses a copy
# that was not checked against it.
def toolchain_name(package):
    return "%s-%s-%s" % (package.ext["go"].toolchain, HOST_ARCH,
                         _toolchain_sha256(package)[:16])

def _toolchain_dir(package):
    return os.path.join(toolchain_root(), toolchain_name(package))

def parse(package, extends):
    if "go" not in extends:
        return None
    settings = extends["go"]
    toolchain = parsing.parse_string(
        package, "go", settings, "toolchain",
        hint="the Go version to build with, for example the one in go.mod, "
        "or the one 'defaults: extends: go:' sets")
    return types.SimpleNamespace(
        toolchain=toolchain,
        toolchain_sha256=_parse_toolchain_sha256(package, settings, toolchain),
        build=parsing.parse_string(package, "go", settings, "build", "."),
        commands=_parse_commands(package, settings),
        cgo=parsing.parse_bool(package, "go", settings, "cgo"),
        ldflags=parsing.parse_string(
            package, "go", settings, "ldflags", "", shell_safe=True),
        tags=parsing.parse_string(
            package, "go", settings, "tags", "", shell_safe=True),
        build_depends=parsing.parse_relationships(
            package, "go", settings, "build-depends"),
        runtime_depends=parsing.parse_relationships(
            package, "go", settings, "runtime-depends"),
        runtime_suggests=parsing.parse_relationships(
            package, "go", settings, "runtime-suggests"))

# One digest per Debian architecture, as the toolchain is downloaded
# for the machine that builds (HOST_ARCH), not for the target.
def _parse_toolchain_sha256(package, settings, toolchain):
    value = settings.get("toolchain-sha256")
    if type(value) != type({}) or len(value) == 0:
        raise package._error(
            "'extends: go: toolchain-sha256' shall map each Debian "
            "architecture that may build this package to the sha256 of "
            "its 'go%s.linux-<goarch>.tar.gz', as listed at "
            "https://go.dev/dl/" % toolchain)
    for architecture, digest in value.items():
        if architecture not in GOARCH:
            raise package._error(
                "'extends: go: toolchain-sha256' names '%s', which is not "
                "a known Debian architecture, expected one of %s"
                % (architecture, ", ".join(sorted(GOARCH))))
        if type(digest) != type("") or SHA256.match(digest) is None:
            raise package._error(
                "'extends: go: toolchain-sha256' has '%s' for '%s', which "
                "is not a sha256" % (digest, architecture))
    return value

# What 'defaults: extends: go:' says, checked where it is written.
def check_defaults(settings):
    class Defaults:
        def _error(self, message):
            return ValueError(f"'defaults: extends: go': {message}")
    package = Defaults()
    toolchain = parsing.parse_string(package, "go", settings, "toolchain")
    _parse_toolchain_sha256(package, settings, toolchain)

def _toolchain_sha256(package):
    digest = package.ext["go"].toolchain_sha256.get(HOST_ARCH)
    if digest is None:
        raise ValueError(
            "package '%s': 'extends: go: toolchain-sha256' has no entry "
            "for '%s', the architecture of this machine"
            % (package.name, HOST_ARCH))
    return digest

# Listed, not discovered, so a build that makes fewer binaries fails.
def _parse_commands(package, settings):
    commands = settings.get("commands")
    if type(commands) != type([]) or len(commands) == 0:
        raise package._error(
            "'extends: go: commands' shall be a non-empty list of "
            "{package: <Go package to build>, binary: <installed name>}")
    parsed = []
    for entry in commands:
        if (type(entry) != type({}) or not {"package", "binary"} <= set(entry)
                or not set(entry) <= {"package", "binary", "links", "alternatives"}
                or type(entry["package"]) != type("")
                or type(entry["binary"]) != type("")):
            raise package._error(
                "'extends: go: commands' entries shall be {package: <Go "
                "package to build>, binary: <installed name>}, and may "
                "have 'links' and 'alternatives'")
        if "/" in entry["binary"] or entry["binary"] in ("", ".", ".."):
            raise package._error(
                "'extends: go: commands' names '%s' as a binary, which "
                "is not a plain file name" % entry["binary"])
        command = dict(entry)
        command["links"] = _parse_links(package, command)
        command["alternatives"] = _parse_alternatives(package, command)
        parsed.append(command)
    _check_paths(package, parsed)
    return parsed

# A link is a plain name, put next to the binary, or an absolute path.
# Packages may not install into /usr/local: dh_usrlocal refuses it.
def _check_link(package, setting, link):
    if (type(link) != type("") or link in ("", ".", "..", "/")
            or any(c.isspace() for c in link)
            or ".." in link.split("/")
            or (not link.startswith("/") and "/" in link)):
        raise package._error(
            "'extends: go: %s' has '%s': a link shall be a plain file "
            "name, or an absolute path" % (setting, link))
    if link.startswith("/usr/local/"):
        raise package._error(
            "'extends: go: %s' has '%s': a package cannot install "
            "into /usr/local" % (setting, link))

def _parse_links(package, command):
    links = command.get("links", [])
    if type(links) != type([]):
        raise package._error("'extends: go: links' shall be a list")
    for link in links:
        _check_link(package, "links", link)
    return list(links)

def _parse_alternatives(package, command):
    alternatives = command.get("alternatives", [])
    if type(alternatives) != type([]):
        raise package._error("'extends: go: alternatives' shall be a list")
    parsed = []
    for entry in alternatives:
        if type(entry) == type(""):
            entry = {"link": entry}
        if (type(entry) != type({}) or "link" not in entry
                or set(entry) - {"link", "priority"}):
            raise package._error(
                "'extends: go: alternatives' entries shall be a link, or "
                "{link: <link>, priority: <number>}")
        _check_link(package, "alternatives", entry["link"])
        priority = entry.get("priority", 50)
        if type(priority) != type(0) or type(priority) == type(True):
            raise package._error(
                "'extends: go: alternatives' has priority '%s', which is "
                "not a number" % (priority,))
        parsed.append({"link": entry["link"], "priority": priority})
    return parsed

# Two files at one path would make the package fail to install.
def _check_paths(package, commands):
    seen = set()
    for command in commands:
        for path in [BIN + command["binary"]] + [
                link_path(link) for link in command["links"]] + [
                link_path(a["link"]) for a in command["alternatives"]]:
            if path in seen:
                raise package._error(
                    "'extends: go: commands' installs '%s' twice" % path)
            seen.add(path)

BIN = "/usr/bin/"

def link_path(link):
    return link if link.startswith("/") else BIN + link

# Link targets are relative, so they stay right if the tree is mounted elsewhere.
def _entry(binary, link):
    path = link_path(link)
    directory = os.path.dirname(path)
    return {"path": path, "directory": directory,
            "name": os.path.basename(path), "binary": binary,
            "target": os.path.relpath(binary, directory)}

def links_of(commands):
    plain, alternatives = [], []
    for command in commands:
        binary = BIN + command["binary"]
        plain += [_entry(binary, link) for link in command["links"]]
        for alternative in command["alternatives"]:
            entry = _entry(binary, alternative["link"])
            entry["priority"] = alternative["priority"]
            alternatives.append(entry)
    return plain, alternatives

# Runs in the builder image, which has network.
def fetch_toolchain(builder, package):
    directory = _toolchain_dir(package)
    marker = os.path.join(directory, FETCHED)
    if os.path.isfile(marker):
        return directory
    os.makedirs(directory, exist_ok=True)
    goarch = GOARCH.get(HOST_ARCH, {}).get("GOARCH", HOST_ARCH)
    url = TOOLCHAIN_URL % (package.ext["go"].toolchain, goarch)
    filename = os.path.basename(url)
    print("fetching '%s'" % url)
    builder.builderImage.exec(
        ["sh", "-c",
         "curl -sSfL -o %s %s && "
         "echo '%s  %s' | sha256sum -c - && "
         "tar -xf %s"
         % (filename, url, _toolchain_sha256(package), filename, filename)],
        volumes=[(directory, WORKDIR)], workdir=WORKDIR)
    open(marker, "w").close()
    return directory

# The build phase has no network, so the Go modules are fetched now.
# 'build:' is where go.mod is, which may not be the top of the clone.
def _vendor(builder, package, sourcedir):
    build_dir = os.path.join(sourcedir, package.ext["go"].build)
    if os.path.isdir(os.path.join(build_dir, "vendor")):
        return
    toolchain = fetch_toolchain(builder, package)
    print("vendoring go modules for '%s'" % package.name)
    builder.builderImage.exec(
        ["sh", "-c",
         "export GOROOT=/goroot-vendor/go GOTOOLCHAIN=local "
         "GOPATH=/goroot-vendor/gopath "
         "PATH=/goroot-vendor/go/bin:$PATH; "
         "cd %s/%s && go mod vendor" % (WORKDIR, package.ext["go"].build)],
        volumes=[(sourcedir, WORKDIR), (toolchain, "/goroot-vendor")])

# Only written when the package has alternatives to register.
GO_SCRIPTS = ("postinst", "prerm")

def go_packaging():
    return templates.load_templates("go", templates.FILES + GO_SCRIPTS)

# Needed even without cgo: dh_strip wants a cross binutils.
def _cross_architectures(builder, package):
    return sorted(a for a in builder.architectures(package)
                  if builder.cross(package, a))

# What the build reads besides the source, for the digest of a build.
def digest_fields(builder, package, architecture):
    settings = package.ext["go"]
    return [
        ("toolchain", settings.toolchain),
        ("toolchain-sha256", str(settings.toolchain_sha256.get(HOST_ARCH))),
        ("build", settings.build),
        ("commands", json.dumps(settings.commands, sort_keys=True)),
        ("cgo", str(settings.cgo)),
        ("ldflags", settings.ldflags),
        ("tags", settings.tags),
        ("build-depends", ",".join(settings.build_depends)),
        ("runtime-depends", ",".join(settings.runtime_depends)),
        ("runtime-suggests", ",".join(settings.runtime_suggests)),
        ("packaging", go_packaging()[1]),
    ]

def excerpt(package):
    settings = package.ext["go"]
    shown = {"toolchain": settings.toolchain, "build": settings.build,
             "commands": settings.commands}
    for name, value in [("cgo", settings.cgo), ("ldflags", settings.ldflags),
                        ("tags", settings.tags),
                        ("build-depends", settings.build_depends),
                        ("runtime-depends", settings.runtime_depends),
                        ("runtime-suggests", settings.runtime_suggests)]:
        if value:
            shown[name] = value
    return shown

# Replaces any debian/ the tree has with packaging made from the settings.
def extend(builder, package, sourcedir, epoch):
    settings = package.ext["go"]
    _vendor(builder, package, sourcedir)

    debian = templates.reset_debian(sourcedir)
    found, _ = go_packaging()
    links, alternatives = links_of(settings.commands)
    context = {
        **templates.base_context(
            package, epoch, f"Packaged by seine from {package.source}."),
        "source": package.source,
        "build_dir": settings.build,
        "toolchain": settings.toolchain,
        "toolchain_name": toolchain_name(package),
        "host_arch": HOST_ARCH,
        "commands": settings.commands,
        "links": links,
        "alternatives": alternatives,
        "cgo": settings.cgo,
        "cross_architectures": _cross_architectures(builder, package),
        "ldflags": settings.ldflags,
        "tags": settings.tags,
        "build_depends": settings.build_depends,
        "runtime_depends": settings.runtime_depends,
        "runtime_suggests": settings.runtime_suggests,
        "goarch": sorted(GOARCH.items()),
    }
    scripts = {name: f"{package.name}.{name}" for name in GO_SCRIPTS}
    if len(alternatives) == 0:
        found = {name: text for name, text in found.items()
                 if name not in GO_SCRIPTS}
    templates.render_files(debian, found, context, names=scripts)
