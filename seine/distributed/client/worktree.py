# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0
"""Worktree packing and unpacking for distributed builds."""

import fnmatch
import os
import sys
import tarfile
import tempfile
from typing import Iterable, Optional

from seine.storage.base import StorageError
from seine.storage.s3.provider import _file_sha256, _zstd_reader, _zstd_writer

DEFAULT_EXCLUDES = [
    ".git", "__pycache__", ".venv", "/build/", "*.pyc",
    "*.db", "*.db-shm", "*.db-wal",
    ".env", ".env.*", "*.key", "id_rsa*", "id_ed25519*",
    "/deploy/", "/home/",
]

# Names that look like secrets but are not excluded: warn, do not fail.
SECRET_NAME_HINTS = ("secret", "token", "credential")
SECRET_NAME_SUFFIXES = (".pem",)


class PathTraversalError(ValueError, tarfile.TarError, StorageError):
    """Raised when an archive member attempts directory traversal."""


class IgnoreRule:
    """Represents a single gitignore/seineignore pattern."""

    def __init__(self, raw_pattern: str):
        self.raw = raw_pattern.strip()
        self.negation = False
        self.only_dir = False
        self.anchored = False
        pattern = self.raw
        if pattern.startswith("!"):
            self.negation = True
            pattern = pattern[1:]
        if pattern.endswith("/"):
            self.only_dir = True
            pattern = pattern[:-1]
        if pattern.startswith("/"):
            self.anchored = True
            pattern = pattern[1:]
        elif "/" in pattern:
            self.anchored = True
        self.pattern = pattern

    def matches(self, relpath: str, is_dir: bool) -> bool:
        if self.only_dir and not is_dir:
            return False
        norm_path = relpath.replace(os.sep, "/")
        if self.anchored:
            return (
                fnmatch.fnmatch(norm_path, self.pattern)
                or fnmatch.fnmatch(norm_path, self.pattern + "/*")
            )
        else:
            basename = os.path.basename(norm_path)
            if fnmatch.fnmatch(basename, self.pattern):
                return True
            parts = norm_path.split("/")
            return any(fnmatch.fnmatch(p, self.pattern) for p in parts)


class IgnoreFilter:
    """Matches relative paths against a collection of ignore patterns."""

    def __init__(self, patterns: list[str]):
        self.rules = []
        for p in patterns:
            p = p.strip()
            if p and not p.startswith("#"):
                self.rules.append(IgnoreRule(p))

    def is_ignored(self, relpath: str, is_dir: bool = False) -> bool:
        norm_path = relpath.replace(os.sep, "/").strip("/")
        if not norm_path:
            return False

        if not is_dir:
            parts = norm_path.split("/")
            for i in range(1, len(parts)):
                parent = "/".join(parts[:i])
                if self._matches(parent, is_dir=True):
                    return True

        return self._matches(norm_path, is_dir=is_dir)

    def _matches(self, norm_path: str, is_dir: bool) -> bool:
        ignored = False
        for rule in self.rules:
            if rule.matches(norm_path, is_dir):
                ignored = not rule.negation
        return ignored


def _normalise(info: tarfile.TarInfo) -> tarfile.TarInfo:
    """Drop owner and mode noise; mtimes stay, seine derives timestamps from them."""
    info.mtime = int(info.mtime)
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    if info.issym():
        info.mode = 0o777
    elif info.isdir() or info.mode & 0o111:
        info.mode = 0o755
    else:
        info.mode = 0o644
    return info


def _ignore_filter(root_dir: str, ignore_rules: Optional[list[str]]) -> IgnoreFilter:
    """Build the filter from the default excludes, .gitignore, .seineignore and ignore_rules."""
    patterns = list(DEFAULT_EXCLUDES)
    for name in (".gitignore", ".seineignore"):
        path = os.path.join(root_dir, name)
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                patterns.extend(f.readlines())
    patterns.extend(ignore_rules or [])
    return IgnoreFilter(patterns)


def _add_staged(tar: tarfile.TarFile, staged: Optional[dict[str, str]]) -> None:
    """Add files from outside the project under the archive names given."""
    for arcname, source in sorted((staged or {}).items()):
        tar.add(source, arcname=arcname, recursive=False, filter=_normalise)


def _is_suspicious(name: str) -> bool:
    lower = os.path.basename(name).lower()
    return any(h in lower for h in SECRET_NAME_HINTS) or lower.endswith(SECRET_NAME_SUFFIXES)


def _warn_secrets(suspicious: list[str]) -> None:
    if suspicious:
        shown = ", ".join(suspicious[:5])
        more = f" (+{len(suspicious) - 5} more)" if len(suspicious) > 5 else ""
        print(f"warning: worktree bundle includes files that look like secrets: "
              f"{shown}{more}; add them to .seineignore", file=sys.stderr)


def pack_worktree(
    root_dir: str,
    ignore_rules: Optional[list[str]] = None,
    out_path: Optional[str] = None,
    staged: Optional[dict[str, str]] = None,
) -> tuple[str, str]:
    """Package root_dir into a reproducible .tar.zst and return (path, sha256)."""
    root_dir = os.path.abspath(root_dir)
    if not os.path.isdir(root_dir):
        raise FileNotFoundError(f"Root directory does not exist: {root_dir}")

    ignore_filter = _ignore_filter(root_dir, ignore_rules)

    if out_path is None:
        fd, out_path = tempfile.mkstemp(suffix=".tar.zst", prefix="worktree-")
        os.close(fd)

    suspicious = []
    try:
        with _zstd_writer(out_path) as out, \
                tarfile.open(fileobj=out, mode="w|", format=tarfile.PAX_FORMAT) as tar:
            for dirpath, dirnames, filenames in os.walk(root_dir, followlinks=False):
                rel_dir = os.path.relpath(dirpath, root_dir).replace(os.sep, "/")
                if rel_dir == ".":
                    rel_dir = ""

                dirnames.sort()
                kept_dirnames = []
                for d in dirnames:
                    rel_d = f"{rel_dir}/{d}" if rel_dir else d
                    full_d = os.path.join(dirpath, d)
                    if ignore_filter.is_ignored(rel_d, is_dir=True):
                        continue
                    # If directory symlink, add immediately since os.walk won't recurse into it
                    if os.path.islink(full_d):
                        tar.add(full_d, arcname=rel_d, recursive=False, filter=_normalise)
                    else:
                        kept_dirnames.append(d)
                dirnames[:] = kept_dirnames

                if rel_dir and not os.path.islink(dirpath):
                    tar.add(dirpath, arcname=rel_dir, recursive=False, filter=_normalise)

                for f in sorted(filenames):
                    rel_f = f"{rel_dir}/{f}" if rel_dir else f
                    full_f = os.path.join(dirpath, f)
                    if os.path.abspath(full_f) == os.path.abspath(out_path):
                        continue
                    if not ignore_filter.is_ignored(rel_f, is_dir=False):
                        tar.add(full_f, arcname=rel_f, recursive=False, filter=_normalise)
                        if _is_suspicious(f):
                            suspicious.append(rel_f)
            _add_staged(tar, staged)
        tree_digest = _file_sha256(out_path)
    except BaseException:
        os.unlink(out_path)
        raise

    _warn_secrets(suspicious)

    return out_path, tree_digest


class OutsideRootError(ValueError):
    """Raised when a file a build reads is not under the project directory."""


def _relative(root_dir: str, path: str) -> str:
    """Return path relative to root_dir, whether either is spelled through a symlink."""
    for root in dict.fromkeys((os.path.abspath(root_dir), os.path.realpath(root_dir))):
        rel = os.path.relpath(path, root)
        if rel != ".." and not rel.startswith(".." + os.sep):
            return rel
    raise OutsideRootError(f"'{path}' is outside the project directory '{root_dir}'")


def _sparse_entries(root_dir: str, paths: Iterable[str], ignore_filter: IgnoreFilter) -> list[str]:
    """Return the sorted relative names to pack: 'paths' with trees expanded.

    A symlink, or a symlinked parent directory, is kept under its own name, and
    so is its target when that is inside the project: the spec names the link,
    the link needs the target.
    """
    entries: set[str] = set()

    def add(path: str) -> None:
        rel = os.path.normpath(_relative(root_dir, os.path.abspath(path)))
        if rel in entries:
            return
        # Pack a symlinked parent as a link and follow the path to its target.
        parts = rel.split(os.sep)
        for i in range(1, len(parts)):
            if os.path.islink(os.path.join(root_dir, *parts[:i])):
                entries.add(os.sep.join(parts[:i]))
                try:
                    add(os.path.realpath(path))
                except OutsideRootError:
                    pass
                return
        if os.path.islink(path):
            entries.add(rel)
            try:
                add(os.path.realpath(path))
            except OutsideRootError:
                pass
        elif os.path.isdir(path):
            walk(path)
        elif os.path.isfile(path):
            entries.add(rel)
        else:
            raise ValueError(f"'{path}' does not exist")

    def walk(tree: str) -> None:
        for dirpath, dirnames, filenames in os.walk(tree):
            rel_dir = os.path.normpath(_relative(root_dir, os.path.abspath(dirpath)))
            kept = []
            for d in dirnames:
                full = os.path.join(dirpath, d)
                if ignore_filter.is_ignored(os.path.join(rel_dir, d), True):
                    continue
                if os.path.islink(full):
                    add(full)
                else:
                    kept.append(d)
            dirnames[:] = kept
            for f in filenames:
                if not ignore_filter.is_ignored(os.path.join(rel_dir, f)):
                    add(os.path.join(dirpath, f))

    for path in paths:
        add(path)
    return sorted(e.replace(os.sep, "/") for e in entries)


def _normalise_dir(info: tarfile.TarInfo) -> tarfile.TarInfo:
    """Directories carry no timestamp: an edit beside a file must not change the bundle."""
    info = _normalise(info)
    info.mtime = 0
    return info


def pack_sparse_worktree(
    root_dir: str,
    paths: Iterable[str],
    out_path: Optional[str] = None,
    staged: Optional[dict[str, str]] = None,
) -> tuple[str, str]:
    """Package only 'paths' (files or trees under root_dir) and return (path, sha256).

    'staged' maps an archive name to a file outside root_dir to pack under it.
    """
    root_dir = os.path.abspath(root_dir)
    outside = {os.path.realpath(p) for p in (staged or {}).values()}
    paths = [p for p in paths if os.path.realpath(p) not in outside]
    entries = _sparse_entries(root_dir, paths, _ignore_filter(root_dir, None))
    if out_path is None:
        fd, out_path = tempfile.mkstemp(suffix=".tar.zst", prefix="worktree-")
        os.close(fd)
    try:
        with _zstd_writer(out_path) as out, \
                tarfile.open(fileobj=out, mode="w|", format=tarfile.PAX_FORMAT) as tar:
            added = set()
            for rel in entries:
                parts = rel.split("/")
                for i in range(1, len(parts)):
                    parent = "/".join(parts[:i])
                    if parent not in added:
                        added.add(parent)
                        tar.add(os.path.join(root_dir, parent), arcname=parent,
                                recursive=False, filter=_normalise_dir)
                tar.add(os.path.join(root_dir, rel), arcname=rel,
                        recursive=False, filter=_normalise)
            _add_staged(tar, staged)
        tree_digest = _file_sha256(out_path)
    except BaseException:
        os.unlink(out_path)
        raise
    _warn_secrets([e for e in entries if _is_suspicious(e)])
    return out_path, tree_digest


def unpack_worktree(archive_path: str, dest_dir: str) -> str:
    """Safely unpack the .tar.zst bundle into dest_dir, preventing path traversal."""
    dest_dir = os.path.abspath(dest_dir)
    os.makedirs(dest_dir, exist_ok=True)

    # A stream cannot be rewound: check every member first, then extract
    with _zstd_reader(archive_path) as reader, \
            tarfile.open(fileobj=reader, mode="r|*") as tar:
        for member in tar:
            if member.name.startswith("/") or member.name.startswith("\\"):
                raise PathTraversalError(f"Absolute path in archive: {member.name}")

            target_path = os.path.abspath(os.path.join(dest_dir, member.name))
            if not (target_path == dest_dir or target_path.startswith(dest_dir + os.sep)):
                raise PathTraversalError(f"Path traversal detected: {member.name}")

            if member.issym() or member.islnk():
                if member.linkname.startswith("/") or member.linkname.startswith("\\"):
                    raise PathTraversalError(f"Absolute link target detected: {member.linkname}")
                link_target = os.path.abspath(os.path.join(os.path.dirname(target_path), member.linkname))
                if not (link_target == dest_dir or link_target.startswith(dest_dir + os.sep)):
                    raise PathTraversalError(
                        f"Symlink traversal detected: {member.name} -> {member.linkname}"
                    )

    with _zstd_reader(archive_path) as reader, \
            tarfile.open(fileobj=reader, mode="r|*") as tar:
        if hasattr(tarfile, "data_filter"):
            tar.extractall(dest_dir, filter="data")
        else:
            tar.extractall(dest_dir)

    return dest_dir
