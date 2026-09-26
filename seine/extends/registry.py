# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# The 'extends:' kinds, and what seine asks of each one. Adding a kind
# means a module and one entry below.

import collections

from seine import kernel
from seine.extends import module
from seine.extends import uefi_keys
from seine.extends import uki
from seine.extends import uki_addon

# generates_source: no 'source:', extend() writes the whole tree.
# no_changelog: the fetched tree has none to date the build by.
# extra_setting: (pattern, name) for settings not on a fixed list.
Extension = collections.namedtuple(
    "Extension",
    ["name", "settings", "parse", "extend", "what", "generates_source",
     "no_changelog", "extra_setting"],
    defaults=[None, None, False, False, None])

# Kernels are grafted onto a tree, not written from templates: no extend()
# here, kernel.extend() runs later with its own arguments.
EXTENSIONS = [
    Extension("kernel", kernel.SETTINGS, kernel.parse),
    Extension("module", module.SETTINGS, module.parse, module.extend,
              "an out-of-tree module", no_changelog=True,
              extra_setting=(module.MODULE_KERNELS, "<architecture>-kernels")),
    Extension("uefi-keys", uefi_keys.SETTINGS, uefi_keys.parse,
              uefi_keys.extend, "the UEFI key provisioning",
              generates_source=True),
    Extension("uki", uki.SETTINGS, uki.parse, uki.extend, "a UKI wrapper",
              generates_source=True),
    Extension("uki-addon", uki_addon.SETTINGS, uki_addon.parse,
              uki_addon.extend, "a UKI addon", generates_source=True),
]

BY_NAME = {extension.name: extension for extension in EXTENSIONS}

# Checks 'extends:' as written, then reads it onto the package: every
# kind is parsed, so a package always has all its extension attributes.
def parse_all(package, extends):
    for kind, settings in extends.items():
        if kind not in BY_NAME:
            raise package._error(
                f"'extends' has no '{kind}' build type, expected one of "
                + ", ".join(sorted(BY_NAME)))
        if type(settings) != type({}):
            raise package._error(f"'extends: {kind}' shall be a dictionary")
        _check_settings(package, BY_NAME[kind], settings)
    for extension in EXTENSIONS:
        extension.parse(package, extends)

def _check_settings(package, extension, settings):
    for setting in settings:
        if setting in extension.settings:
            continue
        if (extension.extra_setting is not None
                and extension.extra_setting[0].match(setting)):
            continue
        expected = sorted(extension.settings)
        if extension.extra_setting is not None:
            expected.append(extension.extra_setting[1])
        raise package._error(
            f"'extends: {extension.name}' has no '{setting}' setting, "
            "expected one of " + ", ".join(expected))

# The kinds this package extends, in table order.
def in_use(package):
    return [extension for extension in EXTENSIONS
            if extension.name in package.extends]

# The kind that writes this package's whole tree, if any.
def generator(package):
    for extension in in_use(package):
        if extension.generates_source:
            return extension
    return None

def no_changelog(package):
    return any(extension.no_changelog for extension in in_use(package))

# Writes each kind's packaging into the tree.
def extend_all(builder, package, sourcedir, epoch):
    for extension in in_use(package):
        if extension.extend is not None:
            extension.extend(builder, package, sourcedir, epoch)
