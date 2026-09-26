# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# What 'extends: uki:' means: wrap a built 'linux-image-*' and an
# 'initrd:' artifact into one Unified Kernel Image. No upstream to fetch --
# a uki package carries no 'source:'.

import os
import shutil
import types

from seine.container import ContainerEngine
from seine.extends import parsing
from seine.extends import templates
from seine.utils import distribution

# Bump when this code changes what a UKI is built into.
REVISION = 1

SETTINGS = ["cmdline", "initrd", "linux-image", "signing-key", "tool"]

TOOLS = ["ukify", "efibootguard"]

# Build-Depends that put each tool and its EFI stub into the chroot.
TOOL_BUILD_DEPENDS = {
    "ukify": "systemd-ukify, systemd-boot-efi",
    "efibootguard": "efibootguard",
}

# UEFI's own architecture names ('kernel-stubx64.efi'), not Debian's.
EFI_ARCH = {
    "amd64": "x64",
    "arm64": "aa64",
}

INITRD_NAME = "initrd.img"

def is_uki_package(package):
    return "uki" in package.ext

def parse(package, extends):
    if "uki" not in extends:
        return None
    settings = extends["uki"]
    parsing.require_generated_source(package, "uki")

    tool = settings.get("tool")
    if tool not in TOOLS:
        raise package._error(
            "'extends: uki: tool' shall be one of %s" % ", ".join(TOOLS))
    return types.SimpleNamespace(
        tool=tool,
        linux_image=parsing.parse_string(
            package, "uki", settings, "linux-image",
            hint="the 'linux-image' package this UKI wraps"),
        initrd=parsing.parse_string(
            package, "uki", settings, "initrd",
            hint="the 'initrd:' artifact this UKI is built from"),
        # Also shell-quoted before reaching debian/rules; belt and suspenders.
        cmdline=parsing.parse_string(
            package, "uki", settings, "cmdline", "", shell_safe=True),
        # Names the vault key this UKI's '.efi' is signed with, post-build
        # (seine/uki_sign.py) -- sbuild's unshare chroot has no network to
        # reach a vault from, so signing can't happen inside 'ukify build'.
        signing_key=parsing.parse_vault_key(
            package, "uki", settings, "signing-key"))

def initrd_path(distro, filename):
    if os.path.isabs(filename):
        return filename
    return os.path.join(ContainerEngine.deploy_root(), distro["release"], filename)

def _require_initrd(package, distro):
    settings = package.ext["uki"]
    path = initrd_path(distro, settings.initrd)
    if os.path.isfile(path) == False:
        raise ValueError(
            "package '%s': 'extends: uki: initrd' names '%s', which is "
            "not a deployed file (%s) -- build its own specification "
            "first" % (package.name, settings.initrd, path))
    return path

# Checked right after parsing, before any bootstrap or fetch work starts.
def check_initrds(packages, spec):
    distro = distribution(spec)
    for package in packages:
        if is_uki_package(package):
            _require_initrd(package, distro)

def uki_packaging(tool):
    directory = "uki-ukify" if tool == "ukify" else "uki-efibootguard"
    return templates.load_templates(directory)

# Shared between package-build (extend(), rendered into debian/rules as
# shell), image-build (imager.py, real paths), and a cmdline-only addon
# (uki_addon.py, linux=initrd=None -- no kernel/initrd of its own).
# Quoting is the caller's job -- pre-quoted for the shell-rendered
# caller, plain for argv.
def ukify_argv(linux, initrd, cmdline, output, extra=()):
    argv = ["ukify", "build"]
    if linux is not None:
        argv.append("--linux=%s" % linux)
    if initrd is not None:
        argv.append("--initrd=%s" % initrd)
    if cmdline:
        argv.append("--cmdline=%s" % cmdline)
    argv.extend(extra)
    argv.append("--output=%s" % output)
    return argv

# The named 'initrd:' is read as its own bytes, not by name.
def digest_fields(builder, package, architecture):
    settings = package.ext["uki"]
    initrd = initrd_path(builder.distro, settings.initrd)
    # Digests are computed up front, so an 'after:'-ordered initrd may not
    # exist yet. A missing file matches no real hash: one rebuild.
    content = b"<initrd not yet built>"
    if os.path.isfile(initrd):
        with open(initrd, "rb") as f:
            content = f.read()
    return [
        ("tool", settings.tool),
        ("linux-image", settings.linux_image),
        ("cmdline", settings.cmdline),
        # A different (or no) vault key changes the '.efi' bytes.
        ("signing-key", str(settings.signing_key)),
        ("initrd", content),
        ("packaging", uki_packaging(settings.tool)[1]),
    ]

def excerpt(package):
    settings = package.ext["uki"]
    shown = {"tool": settings.tool, "linux-image": settings.linux_image,
             "initrd": settings.initrd}
    if settings.cmdline:
        shown["cmdline"] = settings.cmdline
    if settings.signing_key:
        shown["signing-key"] = parsing.VAULT_PREFIX + settings.signing_key
    return shown

def extend(builder, package, sourcedir, epoch):
    if "uki" not in package.ext:
        return

    settings = package.ext["uki"]

    debian = templates.reset_debian(sourcedir)

    initrd = _require_initrd(package, builder.distro)
    shutil.copy(initrd, os.path.join(sourcedir, INITRD_NAME))

    architecture = builder.distro["architecture"]
    efi_arch = EFI_ARCH.get(architecture)
    if settings.tool == "efibootguard" and efi_arch is None:
        raise ValueError(
            "package '%s': 'extends: uki: tool: efibootguard' has no "
            "EFI stub name for architecture '%s' -- add it to "
            "uki.EFI_ARCH" % (package.name, architecture))

    context = {
        **templates.base_context(
            package, epoch,
            f"Packaged by seine from {settings.linux_image} and {INITRD_NAME}."),
        "linux_image": settings.linux_image,
        "tool_build_depends": TOOL_BUILD_DEPENDS[settings.tool],
        "initrd": INITRD_NAME,
        "efi_arch": efi_arch,
        "cmdline_arg": ("--cmdline=%s" % templates.sh_quote(settings.cmdline)
                        if settings.cmdline else ""),
        "ukify_cmd": " ".join(ukify_argv(
            '"$$vmlinuz"', INITRD_NAME,
            templates.sh_quote(settings.cmdline) if settings.cmdline else "",
            "debian/$(PACKAGE)/boot/EFI/Linux/$(PACKAGE).efi")),
    }
    templates.render_files(debian, uki_packaging(settings.tool)[0], context)
