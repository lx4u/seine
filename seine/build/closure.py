# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0
"""The local files and trees a parsed build reads from disk."""

import os

from seine import stdlib
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


def stdlib_files(builds):
    """Return standard library files read by builds, sub-builds included."""
    paths = set()
    for build in builds:
        for f in build.loaded_files:
            if stdlib.is_stdlib_path(f):
                paths.add(os.path.realpath(f))
        for lib in build.options.get("ansible_library") or []:
            if stdlib.is_stdlib_path(lib):
                paths.add(os.path.realpath(lib))
        image = build.image
        for p in image.host_files():
            if stdlib.is_stdlib_path(p):
                paths.add(os.path.realpath(p))
        for package in image.packages:
            for p in package.referenced_files():
                if stdlib.is_stdlib_path(p):
                    paths.add(os.path.realpath(p))
        paths.update(stdlib_files(build.subbuilds.values()))

    found = set()
    for p in paths:
        if os.path.isdir(p):
            for dirpath, _, filenames in os.walk(p):
                for f in filenames:
                    found.add(os.path.realpath(os.path.join(dirpath, f)))
        elif os.path.isfile(p):
            found.add(p)
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
        playbooks = (build.spec or {}).get("playbook") or []
        keys.update(playbook.scan(playbooks, build.options.get("files"))[1])
        keys.update(unmodeled(build.subbuilds.values()))
    return sorted(keys)

