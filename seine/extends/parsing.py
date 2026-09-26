# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# Parsers for settings under 'extends: <kind>:'. Every error names the
# setting the same way, as 'extends: <kind>: <setting>'.

VAULT_PREFIX = "vault:"

# Would end a shell command: refused in values that reach a recipe.
SHELL_UNSAFE = ["`", "$(", ";", "&", "|", "\n"]

REQUIRED = object()

def setting_path(kind, name):
    return f"extends: {kind}: {name}"

def check_shell_safe(package, path, value, hint=""):
    for forbidden in SHELL_UNSAFE:
        if forbidden in value:
            raise package._error(
                f"'{path}' contains '{forbidden.strip()}', which is not "
                f"allowed{hint}")

# A string. Without a default it must be there, and not empty.
def parse_string(package, kind, settings, name, default=REQUIRED,
                 shell_safe=False, hint=""):
    path = setting_path(kind, name)
    value = settings.get(name, default)
    if value is REQUIRED or type(value) != type("") or (
            default is REQUIRED and len(value) == 0):
        kind_of = "a non-empty" if default is REQUIRED else "a"
        raise package._error(
            f"'{path}' shall be {kind_of} string"
            + (f": {hint}" if hint else ""))
    if shell_safe:
        check_shell_safe(package, path, value)
    return value

def parse_bool(package, kind, settings, name, default=False):
    value = settings.get(name, default)
    if type(value) != type(True):
        raise package._error(
            f"'{setting_path(kind, name)}' shall be true or false")
    return value

def parse_string_list(package, kind, settings, name):
    values = settings.get(name, [])
    if type(values) != type([]) or any(type(v) != type("") for v in values):
        raise package._error(
            f"'{setting_path(kind, name)}' shall be a list of strings")
    return list(values)

# Package relationships as debian/control writes them, one per entry.
def parse_relationships(package, kind, settings, name):
    values = parse_string_list(package, kind, settings, name)
    if any("\n" in value for value in values):
        raise package._error(
            f"'{setting_path(kind, name)}' shall be a list of package "
            "relationships, one per entry, as debian/control writes them")
    return values

def vault_name(package, path, value):
    if (type(value) != type("") or not value.startswith(VAULT_PREFIX)
            or len(value) == len(VAULT_PREFIX)):
        raise package._error(f"'{path}' shall be 'vault:<name>'")
    return value[len(VAULT_PREFIX):]

# The name of the vault key a setting points at, or None if it is unset.
def parse_vault_key(package, kind, settings, name):
    if settings.get(name) is None:
        return None
    return vault_name(package, setting_path(kind, name), settings[name])

# Packages seine writes from nothing have no 'source:' to fetch.
def require_generated_source(package, kind):
    if package.source is not None:
        raise package._error(
            f"'extends: {kind}' packages are generated entirely by seine: "
            "name the package with 'name:' alone, without a 'source:'")
    package.source_name = package.name
