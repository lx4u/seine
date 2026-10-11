# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# Tells which part of a source package a patch file changes.

import re

# Paths a patch changes, as 'patch -p1' sees them (leading directory
# stripped). Covers new, deleted and renamed files.
def changed_paths(path):
    paths = []
    with open(path, "r", errors="replace") as f:
        for line in f:
            found = re.match(r"(?:--- |\+\+\+ )(\S+)", line)
            if found is None:
                found = re.match(r"rename (?:from|to) (\S+)", line)
                name = found.group(1) if found else None
            else:
                name = found.group(1)
                if name == "/dev/null":
                    continue
                name = name.split("/", 1)[1] if "/" in name else name
            if name is not None and name not in paths:
                paths.append(name)
    return paths

def in_debian(path):
    return path.startswith("debian/")
