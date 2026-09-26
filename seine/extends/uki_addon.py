# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# What 'extends: uki-addon:' means: build a systemd-stub cmdline addon
# (systemd-stub(7), '*.efi.extra.d/*.addon.efi') for another package's
# 'extends: uki:'. No upstream to fetch and no kernel/initrd of its
# own -- modeled on uki.py, minus everything that wraps a kernel.
# Needs systemd >= 254 (trixie and later; bookworm ships 252).

import types

from seine.extends import parsing
from seine.extends import templates
from seine.extends import uki
from seine.utils import distribution

# Bump when this code changes what an addon is built into.
REVISION = 1

SETTINGS = ["cmdline", "signing-key", "uki"]

def is_uki_addon_package(package):
    return "uki-addon" in package.ext

def parse(package, extends):
    if "uki-addon" not in extends:
        return None
    settings = extends["uki-addon"]
    parsing.require_generated_source(package, "uki-addon")
    return types.SimpleNamespace(
        uki=parsing.parse_string(
            package, "uki-addon", settings, "uki",
            hint="the 'extends: uki:' package this addon extends"),
        cmdline=parsing.parse_string(
            package, "uki-addon", settings, "cmdline", shell_safe=True),
        # Same vault-only shape as 'extends: uki: signing-key'. None means
        # "inherit the parent's own key" -- resolved_signing_key() below.
        signing_key=parsing.parse_vault_key(
            package, "uki-addon", settings, "signing-key"))

# Checked right after parsing, before ordering -- a bad reference
# should fail here with its own message, not with order()'s generic
# "'after' names an unknown package" once depend_on_parents() runs.
def check_uki_addons(packages, spec):
    release = distribution(spec)["release"]
    by_name = {package.name: package for package in packages}
    for package in packages:
        if not is_uki_addon_package(package):
            continue
        settings = package.ext["uki-addon"]
        if release == "bookworm":
            raise package._error(
                "'extends: uki-addon' needs systemd-stub >= 254 to load "
                "'*.efi.extra.d/' addons; bookworm ships 252 -- target "
                "trixie or later")
        parent = by_name.get(settings.uki)
        if parent is None or uki.is_uki_package(parent) == False:
            raise package._error(
                "'extends: uki-addon: uki' names '%s', which is not an "
                "'extends: uki' package in this specification"
                % settings.uki)
        if parent.ext["uki"].tool != "ukify":
            raise package._error(
                "'extends: uki-addon: uki' names '%s', built with "
                "'tool: efibootguard' -- a systemd-stub addon needs its "
                "parent built with 'tool: ukify'" % settings.uki)

# Gives order() a real edge from an addon to its parent, the way
# module.depend_on_kernels() does for a module -- so a cache digest
# folds the parent's own (its signing key included) automatically.
def depend_on_parents(packages):
    for package in packages:
        if is_uki_addon_package(package):
            parent = package.ext["uki-addon"].uki
            if parent not in package.after:
                package.after.append(parent)

# Mirrors module._built_kernel()'s "search the passed-in package list
# by name" pattern. Only valid once builder.packages is populated,
# which tasks()/run() do before _rebuild() ever calls this.
def _parent(builder, package):
    settings = package.ext["uki-addon"]
    for other in builder.packages:
        if other.name == settings.uki:
            return other
    raise package._error(
        "'extends: uki-addon: uki' names '%s', which is not among the "
        "packages being built" % settings.uki)

def resolved_signing_key(builder, package):
    settings = package.ext["uki-addon"]
    if settings.signing_key is not None:
        return settings.signing_key
    return _parent(builder, package).ext["uki"].signing_key

def uki_addon_packaging():
    return templates.load_templates("uki-addon")

# An unset key inherits the parent's own, which the parent's digest
# already carries.
def digest_fields(builder, package, architecture):
    settings = package.ext["uki-addon"]
    return [
        ("uki", settings.uki),
        ("cmdline", settings.cmdline),
        ("signing-key", str(settings.signing_key)),
        ("packaging", uki_addon_packaging()[1]),
    ]

def excerpt(package):
    settings = package.ext["uki-addon"]
    shown = {"uki": settings.uki, "cmdline": settings.cmdline}
    if settings.signing_key:
        shown["signing-key"] = parsing.VAULT_PREFIX + settings.signing_key
    return shown

def extend(builder, package, sourcedir, epoch):
    if not is_uki_addon_package(package):
        return

    settings = package.ext["uki-addon"]

    debian = templates.reset_debian(sourcedir)

    context = {
        **templates.base_context(
            package, epoch, "Packaged by seine as a systemd-stub cmdline "
            f"addon for {settings.uki}."),
        "uki_name": settings.uki,
        "ukify_cmd": " ".join(uki.ukify_argv(
            None, None, templates.sh_quote(settings.cmdline),
            "debian/$(PACKAGE)/boot/EFI/Linux/%s.efi.extra.d/"
            "$(PACKAGE).addon.efi" % settings.uki)),
    }
    templates.render_files(debian, uki_addon_packaging()[0], context)
