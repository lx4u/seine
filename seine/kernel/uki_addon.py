# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# What 'extends: uki-addon:' means: build a systemd-stub cmdline addon
# (systemd-stub(7), '*.efi.extra.d/*.addon.efi') for another package's
# 'extends: uki:'. No upstream to fetch and no kernel/initrd of its
# own -- modeled on uki.py, minus everything that wraps a kernel.
# Needs systemd >= 254 (trixie and later; bookworm ships 252).

import functools
import os
import shutil

from datetime import datetime
from datetime import timezone
from email.utils import format_datetime

import jinja2

from seine.kernel import uki
from seine.utils import GIT_EMAIL
from seine.utils import GIT_NAME
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

    if package.source is not None:
        raise package._error(
            "'extends: uki-addon' packages are generated entirely by "
            "seine: name the package with 'name:' alone, without a "
            "'source:'")
    package.source_name = package.name

    uki_name = settings.get("uki")
    if type(uki_name) != type("") or len(uki_name) == 0:
        raise package._error(
            "'extends: uki-addon: uki' shall name the 'extends: uki:' "
            "package this addon extends, as a string")
    package.uki_addon_uki = uki_name

    cmdline = settings.get("cmdline")
    if type(cmdline) != type("") or len(cmdline) == 0:
        raise package._error(
            "'extends: uki-addon: cmdline' shall be a non-empty string")
    for forbidden in uki.FORBIDDEN_CMDLINE:
        if forbidden in cmdline:
            raise package._error(
                "'extends: uki-addon: cmdline' contains '%s', which a "
                "kernel command line may not" % forbidden.strip())
    package.uki_addon_cmdline = cmdline

    # Same vault-only shape as 'extends: uki: signing-key'. None means
    # "inherit the parent's own key" -- resolved_signing_key() below.
    package.uki_addon_signing_key = settings.get("signing-key")
    if package.uki_addon_signing_key is not None:
        if (type(package.uki_addon_signing_key) != type("")
                or not package.uki_addon_signing_key.startswith("vault:")):
            raise package._error(
                "'extends: uki-addon: signing-key' shall be 'vault:<name>'")
        package.uki_addon_signing_key = \
            package.uki_addon_signing_key[len("vault:"):]

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

def _sh_quote(value):
    return "'" + value.replace("'", "'\\''") + "'"

_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
UKI_ADDON_PACKAGING = os.path.join(_DATA_DIR, "uki-addon")
UKI_ADDON_FILES = ["changelog", "control", "rules"]

@functools.lru_cache(maxsize=None)
def uki_addon_packaging():
    templates = {}
    for name in UKI_ADDON_FILES:
        with open(os.path.join(UKI_ADDON_PACKAGING, name), "rb") as f:
            templates[name] = f.read().decode()
    return templates

UKI_ADDON_TEMPLATE = jinja2.Environment(
    variable_start_string="[[", variable_end_string="]]",
    block_start_string="[%", block_end_string="%]",
    comment_start_string="[#", comment_end_string="#]",
    trim_blocks=True, lstrip_blocks=True, keep_trailing_newline=True,
    undefined=jinja2.StrictUndefined)

def _write(path, content):
    with open(path, "w") as f:
        f.write(content)

def extend(builder, package, sourcedir, epoch):
    if package.uki_addon == False:
        return

    debian = os.path.join(sourcedir, "debian")
    if os.path.isdir(debian):
        shutil.rmtree(debian)
    os.makedirs(os.path.join(debian, "source"), exist_ok=True)
    _write(os.path.join(debian, "source", "format"), "3.0 (native)\n")

    context = {
        "name": package.name,
        "version": package.upstream_version,
        "maintainer": GIT_NAME,
        "email": GIT_EMAIL,
        "date": format_datetime(datetime.fromtimestamp(epoch, timezone.utc)),
        "uki_name": package.uki_addon_uki,
        "ukify_cmd": " ".join(uki.ukify_argv(
            None, None, _sh_quote(package.uki_addon_cmdline),
            "debian/$(PACKAGE)/boot/EFI/Linux/%s.efi.extra.d/"
            "$(PACKAGE).addon.efi" % package.uki_addon_uki)),
    }
    for name, template in uki_addon_packaging().items():
        _write(os.path.join(debian, name),
               UKI_ADDON_TEMPLATE.from_string(template).render(context))
