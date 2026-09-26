# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# What 'extends: uki-addon:' means: build a systemd-stub cmdline addon
# (systemd-stub(7), '*.efi.extra.d/*.addon.efi') for another package's
# 'extends: uki:'. No upstream to fetch and no kernel/initrd of its
# own -- modeled on uki.py, minus everything that wraps a kernel.
# Needs systemd >= 254 (trixie and later; bookworm ships 252).

from seine.extends import parsing
from seine.extends import templates
from seine.extends import uki
from seine.utils import distribution

SETTINGS = ["cmdline", "signing-key", "uki"]

def is_uki_addon_package(package):
    return getattr(package, "uki_addon", False)

def parse(package, extends):
    settings = extends.get("uki-addon", {})
    package.uki_addon = "uki-addon" in extends
    if package.uki_addon == False:
        package.uki_addon_uki = None
        package.uki_addon_cmdline = ""
        package.uki_addon_signing_key = None
        return

    parsing.require_generated_source(package, "uki-addon")
    package.uki_addon_uki = parsing.parse_string(
        package, "uki-addon", settings, "uki",
        hint="the 'extends: uki:' package this addon extends")
    package.uki_addon_cmdline = parsing.parse_string(
        package, "uki-addon", settings, "cmdline", shell_safe=True)
    # Same vault-only shape as 'extends: uki: signing-key'. None means
    # "inherit the parent's own key" -- resolved_signing_key() below.
    package.uki_addon_signing_key = parsing.parse_vault_key(
        package, "uki-addon", settings, "signing-key")

# Checked right after parsing, before ordering -- a bad reference
# should fail here with its own message, not with order()'s generic
# "'after' names an unknown package" once depend_on_parents() runs.
def check_uki_addons(packages, spec):
    release = distribution(spec)["release"]
    by_name = {package.name: package for package in packages}
    for package in packages:
        if is_uki_addon_package(package) == False:
            continue
        if release == "bookworm":
            raise package._error(
                "'extends: uki-addon' needs systemd-stub >= 254 to load "
                "'*.efi.extra.d/' addons; bookworm ships 252 -- target "
                "trixie or later")
        parent = by_name.get(package.uki_addon_uki)
        if parent is None or uki.is_uki_package(parent) == False:
            raise package._error(
                "'extends: uki-addon: uki' names '%s', which is not an "
                "'extends: uki' package in this specification"
                % package.uki_addon_uki)
        if parent.uki_tool != "ukify":
            raise package._error(
                "'extends: uki-addon: uki' names '%s', built with "
                "'tool: efibootguard' -- a systemd-stub addon needs its "
                "parent built with 'tool: ukify'" % package.uki_addon_uki)

# Gives order() a real edge from an addon to its parent, the way
# module.depend_on_kernels() does for a module -- so a cache digest
# folds the parent's own (its signing key included) automatically.
def depend_on_parents(packages):
    for package in packages:
        if package.uki_addon and package.uki_addon_uki not in package.after:
            package.after.append(package.uki_addon_uki)

# Mirrors module._built_kernel()'s "search the passed-in package list
# by name" pattern. Only valid once builder.packages is populated,
# which tasks()/run() do before _rebuild() ever calls this.
def _parent(builder, package):
    for other in builder.packages:
        if other.name == package.uki_addon_uki:
            return other
    raise package._error(
        "'extends: uki-addon: uki' names '%s', which is not among the "
        "packages being built" % package.uki_addon_uki)

def resolved_signing_key(builder, package):
    if package.uki_addon_signing_key is not None:
        return package.uki_addon_signing_key
    return _parent(builder, package).uki_signing_key

def uki_addon_packaging():
    return templates.load_templates("uki-addon")

def extend(builder, package, sourcedir, epoch):
    if package.uki_addon == False:
        return

    debian = templates.reset_debian(sourcedir)

    context = {
        **templates.base_context(package, epoch),
        "uki_name": package.uki_addon_uki,
        "ukify_cmd": " ".join(uki.ukify_argv(
            None, None, templates.sh_quote(package.uki_addon_cmdline),
            "debian/$(PACKAGE)/boot/EFI/Linux/%s.efi.extra.d/"
            "$(PACKAGE).addon.efi" % package.uki_addon_uki)),
    }
    templates.render_files(debian, uki_addon_packaging()[0], context)
