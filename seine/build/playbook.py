# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0
"""What a spec's playbooks may do, and the host files they read."""

import os
import re

# Lookups that read host state or run host code: not reproducible builds.
IMPURE_LOOKUPS = ("pipe", "lines", "env", "url", "password", "random_string",
                  "hashi_vault")
_IMPURE = re.compile(
    r"\b(?:lookup|query|q)\(\s*['\"](?:\w+\.\w+\.)?(%s)['\"]" % "|".join(IMPURE_LOOKUPS))


def strings(node):
    """Yield every string nested in a playbook, dict keys excluded."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, list):
        for item in node:
            yield from strings(item)
    elif isinstance(node, dict):
        for value in node.values():
            yield from strings(value)


def check(playbooks):
    """Raise ValueError if a playbook uses a lookup that is not hermetic."""
    for text in strings(playbooks):
        found = _IMPURE.search(text)
        if found:
            raise ValueError(
                "playbook uses lookup('%s', ...): impure lookups are blocked, "
                "they break build reproducibility. Use 'defaults:', 'vault:' or "
                "'credentials:' for values, or build a package." % found.group(1))


# Keys that read files seine cannot list; 'with_*' loops included.
UNMODELED_KEYS = {
    "roles", "include_role", "import_role", "include_tasks", "import_tasks",
    "import_playbook", "include_playbook", "vars_files", "include_vars",
    "script", "with_file", "with_fileglob",
}


def items(node):
    """Yield every (key, value) of the dicts nested in a playbook, lists included."""
    if isinstance(node, list):
        for item in node:
            yield from items(item)
    elif isinstance(node, dict):
        for key, value in node.items():
            yield key, value
            yield from items(value)


def host_files(playbooks, spec_dir="."):
    """Return the host files a relative 'src:' names. Absolute ones are target paths."""
    found = set()
    for key, value in items(playbooks):
        if key == "src" and isinstance(value, str) and not os.path.isabs(value):
            for base in (spec_dir, "."):
                path = os.path.join(base, value)
                if os.path.isfile(path):
                    found.add(path)
                    break
    return sorted(found)


def unmodeled(playbooks):
    """Return the keys of 'playbooks' that read files host_files() cannot see."""
    keys = set()
    for key, value in items(playbooks):
        name = str(key).rsplit(".", 1)[-1]
        if name in UNMODELED_KEYS:
            keys.add(name)
        elif isinstance(value, str) and "lookup(" in value:
            keys.add("lookup")
    return sorted(keys)
