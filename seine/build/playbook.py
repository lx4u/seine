# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0
"""What a spec's playbooks may do, and the host files they read."""

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
