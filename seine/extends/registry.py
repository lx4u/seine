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
# revision: bump when the code changes what is built (a kernel has none).
Extension = collections.namedtuple(
    "Extension",
    ["name", "settings", "revision", "parse", "extend", "digest_fields",
     "excerpt", "what", "generates_source", "no_changelog", "extra_setting"],
    defaults=[None, None, None, None, False, False, None])

# Kernels are grafted onto a tree, not written from templates: no extend()
# here, kernel.extend() runs later with its own arguments.
EXTENSIONS = [
    Extension("kernel", kernel.SETTINGS, None, kernel.parse),
    Extension("module", module.SETTINGS, module.REVISION, module.parse,
              module.extend, module.digest_fields, module.excerpt,
              "an out-of-tree module", no_changelog=True,
              extra_setting=(module.MODULE_KERNELS, "<architecture>-kernels")),
    Extension("uefi-keys", uefi_keys.SETTINGS, uefi_keys.REVISION,
              uefi_keys.parse, uefi_keys.extend, uefi_keys.digest_fields,
              uefi_keys.excerpt, "the UEFI key provisioning",
              generates_source=True),
    Extension("uki", uki.SETTINGS, uki.REVISION, uki.parse, uki.extend,
              uki.digest_fields, uki.excerpt, "a UKI wrapper",
              generates_source=True),
    Extension("uki-addon", uki_addon.SETTINGS, uki_addon.REVISION,
              uki_addon.parse, uki_addon.extend, uki_addon.digest_fields,
              uki_addon.excerpt, "a UKI addon", generates_source=True),
]

BY_NAME = {extension.name: extension for extension in EXTENSIONS}

# Checks 'extends:' as written, then keeps what each kind read from it in
# 'package.ext', by kind name. A kind the package does not use has no entry.
# A kernel is the exception: it keeps its own 'kernel_*' attributes.
def parse_all(package, extends):
    for kind, settings in extends.items():
        if kind not in BY_NAME:
            raise package._error(
                f"'extends' has no '{kind}' build type, expected one of "
                + ", ".join(sorted(BY_NAME)))
        if type(settings) != type({}):
            raise package._error(f"'extends: {kind}' shall be a dictionary")
        _check_settings(package, BY_NAME[kind], settings)
    package.ext = {}
    for extension in EXTENSIONS:
        parsed = extension.parse(package, extends)
        if parsed is not None:
            package.ext[extension.name] = parsed

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

# The kinds this package extends, in table order (never the kernel).
def in_use(package):
    return [extension for extension in EXTENSIONS
            if extension.name in package.ext]

# The kind that writes this package's whole tree, if any.
def generator(package):
    for extension in in_use(package):
        if extension.generates_source:
            return extension
    return None

def no_changelog(package):
    return any(extension.no_changelog for extension in in_use(package))

# What the digest of a build reads for each kind in use, as
# ('<kind>.<setting>', value). Changes to a kind's code count through its
# revision.
def digest_fields(builder, package, architecture):
    fields = []
    for extension in in_use(package):
        if extension.revision is not None:
            fields.append(
                (f"{extension.name}.revision", str(extension.revision)))
        if extension.digest_fields is not None:
            fields += [(f"{extension.name}.{label}", value)
                       for label, value in extension.digest_fields(
                           builder, package, architecture)]
    return fields

# The settings behind a build, per kind, as 'cache' shows them.
def excerpts(package):
    return {extension.name: extension.excerpt(package)
            for extension in in_use(package)
            if extension.excerpt is not None}

# Writes each kind's packaging into the tree.
def extend_all(builder, package, sourcedir, epoch):
    for extension in in_use(package):
        if extension.extend is not None:
            extension.extend(builder, package, sourcedir, epoch)
