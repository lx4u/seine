# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0
"""What a spec's playbooks may do, and the host files they read."""

import glob
import os
import re

import yaml

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


# Plays that name files or trees these static forms can list. Anything else
# that reads files is 'dynamic' and needs a 'uses:' on its play.
_LOOKUP = re.compile(r"lookup\(")
_STATIC_LOOKUP = re.compile(
    r"""lookup\(\s*['"](?:ansible\.builtin\.)?(?:file|template)['"]"""
    r"""\s*,\s*['"]([^'"{}]+)['"]\s*\)""")
_FILE_MODULES = {"copy", "template", "patch", "unarchive", "assemble"}
_SRC_ARG = re.compile(r"\bsrc=(\S+)")
_TEMPLATE_REF = re.compile(r"{%-?\s*(?:include|extends|import|from)\s+['\"]([^'\"]+)['\"]")
_ROLE_KEYS = {"roles", "include_role", "import_role"}
_DYNAMIC_KEYS = {"import_playbook", "include_playbook", "with_fileglob"}
_MAX_TEMPLATE = 1 << 20


def resolve(node, fix):
    """Return a copy of a playbook with each static host path run through fix().

    These are the paths that scan() follows. fix() gets the path as written
    and returns the one to use instead (or the same one).
    """
    def path(value):
        return fix(value) if isinstance(value, str) and "{{" not in value else value

    def command(value):
        if not isinstance(value, str):
            return value
        head, _, rest = value.partition(" ")
        return (path(head) + " " + rest).strip()

    def paths(value):
        return [path(v) for v in value] if isinstance(value, list) else path(value)

    if isinstance(node, list):
        return [resolve(item, fix) for item in node]
    if not isinstance(node, dict):
        return node
    out = {}
    for key, value in node.items():
        name = str(key).rsplit(".", 1)[-1]
        value = resolve(value, fix)
        if name == "src":
            # The src of a link is where it points, not a file to read.
            if node.get("state") not in ("link", "hard"):
                value = paths(value)
        elif name in ("vars_files", "with_file"):
            value = paths(value)
        elif name in _FILE_MODULES and isinstance(value, str):
            value = _SRC_ARG.sub(lambda m: "src=" + path(m.group(1)), value)
        elif name == "script":
            if isinstance(value, dict) and "cmd" in value:
                value = dict(value, cmd=command(value["cmd"]))
            else:
                value = command(value)
        elif name in ("include_tasks", "import_tasks", "include_vars"):
            if isinstance(value, dict):
                value = {k: path(v) if k in ("file", "dir") else v for k, v in value.items()}
            else:
                value = path(value)
        out[key] = value
    return out


def scan(playbooks, spec_files=None):
    """Return (host files, dynamic keys) of 'playbooks'.

    Files come from static references. Dynamic keys name what could not be
    listed, except in a play that declares its files under 'uses:'.
    """
    spec_dir = os.path.dirname(spec_files[0]) if spec_files else "."
    files, dynamic = set(), set()
    for play in playbooks:
        scanner = _Scanner(spec_dir)
        scanner.walk(play, spec_dir)
        uses = play.get("uses") if isinstance(play, dict) else None
        if uses:
            scanner.uses(uses)
        else:
            dynamic |= scanner.dynamic
        files |= scanner.files
    return sorted(files), sorted(dynamic)


class _Scanner:
    def __init__(self, spec_dir):
        self.spec_dir = spec_dir
        self.files = set()
        self.dynamic = set()
        self._seen = set()

    def _inside(self, path):
        path = os.path.abspath(path)
        roots = (os.path.abspath(self.spec_dir), os.getcwd())
        return any(path == r or path.startswith(r + os.sep) for r in roots)

    def _find(self, name, base):
        # An absolute name is a project file only when it is inside the project.
        if os.path.isabs(name):
            return name if os.path.exists(name) and self._inside(name) else None
        for where in (base, self.spec_dir, "."):
            path = os.path.join(where, name)
            if os.path.exists(path):
                if not self._inside(path):
                    raise ValueError(f"playbook path '{name}' leaves the project directory")
                return path
        return None

    def _take(self, path):
        if os.path.isdir(path):
            for top, _dirs, names in os.walk(path):
                self.files.update(os.path.join(top, n) for n in names)
        else:
            self.files.add(path)

    def _add(self, name, base):
        """Add a file or a tree; return its path, or None if it is not there."""
        path = self._find(name, base)
        if path is not None:
            self._take(path)
        return path

    def _static(self, name, base, label):
        """Add 'name' if it is a literal path; else record 'label' as dynamic."""
        if not isinstance(name, str) or "{{" in name:
            self.dynamic.add(label)
            return None
        return self._add(name, base)

    def walk(self, node, base):
        if isinstance(node, list):
            for item in node:
                self.walk(item, base)
        elif isinstance(node, dict):
            for key, value in node.items():
                self._key(str(key).rsplit(".", 1)[-1], value, base)
                self.walk(value, base)
        elif isinstance(node, str):
            self._lookups(node, base)

    def _key(self, name, value, base):
        if name == "src" and isinstance(value, str):
            self._add(value, base)
        elif name in _FILE_MODULES and isinstance(value, str):
            found = _SRC_ARG.search(value)
            if found:
                self._add(found.group(1), base)
        if name == "template":
            src = value.get("src") if isinstance(value, dict) else None
            if isinstance(src, str) and "{{" not in src:
                self._template(self._find(src, base))
        elif name == "script":
            cmd = value.get("cmd") if isinstance(value, dict) else value
            self._static(cmd.split()[0] if isinstance(cmd, str) and cmd.split() else cmd,
                         base, name)
        elif name in ("include_tasks", "import_tasks"):
            ref = value.get("file") if isinstance(value, dict) else value
            self._tasks(self._static(ref, base, name))
        elif name == "vars_files":
            for ref in _flatten(value):
                self._static(ref, base, name)
        elif name == "include_vars":
            ref = value if isinstance(value, str) else (value or {}).get("file") or (value or {}).get("dir")
            self._static(ref, base, name)
        elif name == "with_file":
            for ref in _flatten(value):
                self._static(ref, base, name)
        elif name in _ROLE_KEYS:
            self._roles(name, value, base)
        elif name in _DYNAMIC_KEYS:
            self.dynamic.add(name)

    def _lookups(self, text, base):
        static = _STATIC_LOOKUP.findall(text)
        for ref in static:
            self._add(ref, base)
        if len(_LOOKUP.findall(text)) > len(static):
            self.dynamic.add("lookup")

    def _roles(self, key, value, base):
        names = []
        for item in _flatten(value):
            names.append(item.get("role") or item.get("name") if isinstance(item, dict) else item)
        if key != "roles" and isinstance(value, dict):
            names = [value.get("name")]
        for name in names:
            if not isinstance(name, str) or "{{" in name or not self._add(f"roles/{name}", base):
                self.dynamic.add(key)

    def _tasks(self, path):
        if path is None or not os.path.isfile(path) or path in self._seen:
            return
        self._seen.add(path)
        with open(path) as f:
            self.walk(yaml.safe_load(f), os.path.dirname(path))

    def _template(self, path):
        if path is None or not os.path.isfile(path) or path in self._seen:
            return
        self._seen.add(path)
        self.files.add(path)
        if os.path.getsize(path) > _MAX_TEMPLATE:
            return
        with open(path, errors="replace") as f:
            refs = _TEMPLATE_REF.findall(f.read())
        for ref in refs:
            self._template(self._find(ref, os.path.dirname(path)))

    def uses(self, patterns):
        for pattern in _flatten(patterns):
            if os.path.isabs(pattern):
                raise ValueError(f"'uses: {pattern}' is not relative")
            matches = []
            for where in (self.spec_dir, "."):
                matches = glob.glob(os.path.join(where, pattern), recursive=True)
                if matches:
                    break
            if not matches:
                raise ValueError(f"'uses: {pattern}' matches no file")
            for match in matches:
                if not self._inside(match):
                    raise ValueError(f"'uses: {pattern}' leaves the project directory")
                self._take(match)
        return self.files


def _flatten(value):
    if isinstance(value, list):
        for item in value:
            yield from _flatten(item)
    elif value is not None:
        yield value
