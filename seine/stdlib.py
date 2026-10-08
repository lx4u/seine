# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# Standard specification library (stdlib) discovery and resolution.

import importlib.resources
import os
from typing import List, Optional

PREFIX = "stdlib:"
SYSTEM_STDLIB_DIR = "/usr/share/seine/stdlib"


def search_paths(project_root: Optional[str] = None) -> List[str]:
    """Return ordered list of directories searched for stdlib specifications."""
    paths: List[str] = []

    # 1. Staged worktree for remote builds
    if project_root:
        staged = os.path.abspath(os.path.join(project_root, ".seine-stdlib"))
        if os.path.isdir(staged) and staged not in paths:
            paths.append(staged)
    cwd_staged = os.path.abspath(os.path.join(os.getcwd(), ".seine-stdlib"))
    if os.path.isdir(cwd_staged) and cwd_staged not in paths:
        paths.append(cwd_staged)

    # 2. Environment override
    env_dir = os.environ.get("SEINE_STDLIB_DIR")
    if env_dir:
        abs_env = os.path.abspath(env_dir)
        if abs_env not in paths:
            paths.append(abs_env)

    # 3. Source repository checkout root
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if os.path.exists(os.path.join(repo_root, "setup.py")):
        repo_stdlib = os.path.abspath(os.path.join(repo_root, "stdlib"))
        if repo_stdlib not in paths:
            paths.append(repo_stdlib)

    # 4. Installed Debian package location
    if SYSTEM_STDLIB_DIR not in paths:
        paths.append(SYSTEM_STDLIB_DIR)

    # 5. Packaged Python resources fallback
    try:
        pkg_stdlib = os.fspath(importlib.resources.files("seine").joinpath("stdlib"))
        abs_pkg = os.path.abspath(pkg_stdlib)
        if abs_pkg not in paths:
            paths.append(abs_pkg)
    except Exception:
        pass

    return paths


def resolve(
    arg: str,
    project_root: Optional[str] = None,
    search_dirs: Optional[List[str]] = None,
) -> str:
    """Resolve a specification reference.

    If arg starts with 'stdlib:', search candidate standard library directories
    and return the resolved path. Otherwise, return arg unmodified.
    """
    if not isinstance(arg, str) or not arg.startswith(PREFIX):
        return arg

    subpath = arg[len(PREFIX):].lstrip("/")
    if not subpath:
        raise ValueError(f"empty standard library specification path '{arg}'")

    norm_subpath = os.path.normpath(subpath)
    if os.path.isabs(norm_subpath) or norm_subpath.startswith("..") or "/../" in f"/{norm_subpath}/":
        raise ValueError(f"invalid standard library path '{arg}'")

    if norm_subpath.endswith(".yml") or norm_subpath.endswith(".yaml"):
        candidates = [norm_subpath]
    else:
        candidates = [f"{norm_subpath}.yml", f"{norm_subpath}.yaml", norm_subpath]

    dirs = search_dirs if search_dirs is not None else search_paths(project_root)
    for d in dirs:
        if not os.path.isdir(d):
            continue
        for cand in candidates:
            target = os.path.join(d, cand)
            if os.path.isfile(target):
                return os.path.abspath(target)

    paths_str = ", ".join(dirs) if dirs else "none"
    raise FileNotFoundError(
        f"standard library specification '{arg}' not found (searched: {paths_str})"
    )


def is_stdlib_path(path: str, search_dirs: Optional[List[str]] = None) -> bool:
    """Return True if path is a stdlib reference or points inside a stdlib directory."""
    if not path or not isinstance(path, str):
        return False
    if path.startswith(PREFIX):
        return True
    try:
        real_target = os.path.realpath(path)
    except (OSError, ValueError):
        return False
    dirs = search_dirs if search_dirs is not None else search_paths()
    for d in dirs:
        if not d:
            continue
        try:
            real_d = os.path.realpath(d)
        except (OSError, ValueError):
            continue
        if real_d != os.sep and (real_target == real_d or real_target.startswith(real_d + os.sep)):
            return True
    return False


def stdlib_relpath(path: str, search_dirs: Optional[List[str]] = None) -> Optional[str]:
    """Return the relative path of a stdlib file inside its stdlib directory, or None."""
    if not path or not isinstance(path, str):
        return None
    if path.startswith(PREFIX):
        return path[len(PREFIX):].lstrip("/")
    try:
        real_target = os.path.realpath(path)
    except (OSError, ValueError):
        return None
    dirs = search_dirs if search_dirs is not None else search_paths()
    for d in dirs:
        if not d:
            continue
        try:
            real_d = os.path.realpath(d)
        except (OSError, ValueError):
            continue
        if real_d != os.sep and real_target.startswith(real_d + os.sep):
            return os.path.relpath(real_target, real_d)
    return None
