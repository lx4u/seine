# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0
"""The local files and trees a parsed build reads from disk."""

import os

from seine.build import playbook

def collect(builds):
    """Return the paths (files or trees) that 'builds' read, sub-builds included.

    Paths keep the names the specification gives them: a symlink is not resolved.
    """
    found = set()
    for build in builds:
        found.update(build.loaded_files)
        found.update(build.options.get("ansible_library") or [])
        image = build.image
        found.update(os.path.abspath(p) for p in image.host_files())
        for package in image.packages:
            found.update(os.path.abspath(p) for p in package.referenced_files())
            tree = getattr(package, "file_path", None)
            if tree:
                found.add(os.path.abspath(tree))
        found.update(_container_archives(build))
        found.update(collect(build.subbuilds.values()))
    return found


def _container_archives(build):
    image = build.image
    files = build.options.get("files") or []
    spec_dir = os.path.dirname(files[0]) if files else "."
    arch = ((build.spec or {}).get("distribution") or {}).get("architecture")
    archives = (c.archive_for(arch, spec_dir=spec_dir) for c in image.containers or [])
    return {os.path.abspath(a) for a in archives if a and os.path.exists(a)}


def unmodeled(builds):
    """Return the playbook keys of 'builds' that read files collect() cannot see."""
    keys = set()
    for build in builds:
        keys.update(playbook.unmodeled((build.spec or {}).get("playbook") or []))
        keys.update(unmodeled(build.subbuilds.values()))
    return sorted(keys)
