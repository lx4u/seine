import jinja2
import jinja2.meta
import os
import re
import yaml

from seine.build import playbook
from seine.extends import texts
from seine.extends.templates import TEMPLATE
from seine.utils import lock_sibling

# TEMPLATE renders specs before parsing (one file covers several
# archs/releases): a missing value fails loudly, not silently building
# for the wrong machine. PROBE is the same, but unresolved names render
# empty: its output only collects what names get set, then is thrown away.
PROBE = TEMPLATE.overlay(undefined=jinja2.ChainableUndefined)

# Reading specification files: Jinja rendering, file resolution and
# where each setting came from. Mixed into BuildCmd.
class SpecLoader:
    # A spec passed in as text rather than a file to walk: nothing to
    # probe, so it renders against the spec merged so far.
    def loads(self, yaml_spec):
        return self._load("<string>", yaml_spec)

    # Probes every named file before loading any -- a file may need a
    # name a later file sets. Each file's lock sibling (foo.yaml ->
    # foo.lock.yaml) is auto-spliced in right after it.
    def load_all(self, yaml_files):
        self.options.setdefault("files", list(yaml_files))
        expanded = []
        for yaml_file in yaml_files:
            expanded.append(yaml_file)
            lock = lock_sibling(yaml_file)
            if lock is not None and os.path.isfile(lock):
                expanded.append(lock)
        for yaml_file in expanded:
            self._probe(yaml_file, check=False)
        self._check_names(self._prober._names if self._prober else [])
        for yaml_file in expanded:
            self.load(yaml_file)
        return self.spec

    # Learns what a spec sets before loading it for real, so ordering
    # between files doesn't matter for name lookups. Uses a lenient jinja
    # pass and throws away its output -- only the discovered names survive.
    def _probe(self, yaml_file, check=True):
        if self._prober is None:
            self._prober = type(self)()
            self._prober._probing = True
        self._prober.load(yaml_file)
        self._probed.add(os.path.realpath(yaml_file))
        self._variables = self._prober.spec or {}
        if check:
            self._check_names(self._prober._names)

    # Reports every unset name asked for, all at once rather than one
    # error per run.
    def _check_names(self, names):
        missing = []
        for filename, asked in names:
            for name in sorted(asked - set(self._variables or {})):
                missing.append("%s: '%s' is not set by this specification"
                               % (filename, name))
        if len(missing) > 0:
            raise ValueError("\n".join(missing))

    # Tracks the requires-chain to catch loops (two files requiring each
    # other used to recurse until the stack blew, with no useful
    # traceback). A file reached twice via different paths is not a loop
    # and loads again, on purpose.
    def load(self, yaml_file):
        if not self.options.get("files"):
            self.options["files"] = [yaml_file]
        if self._probing is False and len(self._loading) == 0 \
                and os.path.realpath(yaml_file) not in self._probed:
            self._probe(yaml_file)
        path = os.path.realpath(yaml_file)
        if path in self._loading:
            loop = self._loading[self._loading.index(path):] + [path]
            raise ValueError("'requires' loops: %s!" % " -> ".join(loop))
        self._loading.append(path)
        try:
            with open(yaml_file, "r") as f:
                return self._load(yaml_file, f.read())
        finally:
            self._loading.pop()

    # Rendered against the spec built so far, so a fragment can read what
    # reached for it. Only '[[ ]]' substitutions are allowed, not '[% %]'
    # blocks -- 'requires:' is how a spec branches, kept readable without
    # running it.
    BLOCKS = re.compile(re.escape(TEMPLATE.block_start_string))

    # 'requires:' is stripped from the template text before rendering, so a
    # require can't itself be templated (else which files load would
    # depend on the render, which the files decide).
    REQUIRES = re.compile(r"^([ \t]*)requires:.*?(?=^\1\S|\Z)",
                          re.MULTILINE | re.DOTALL)

    def _render(self, yaml_filename, yaml_spec):
        block = SpecLoader.BLOCKS.search(yaml_spec)
        if block is not None:
            raise ValueError("%s: '%s' blocks are not accepted, only '%s %s' "
                "substitutions -- list the fragments that apply under "
                "'requires' instead!"
                % (yaml_filename, TEMPLATE.block_start_string,
                   TEMPLATE.variable_start_string, TEMPLATE.variable_end_string))
        for requires in SpecLoader.REQUIRES.finditer(yaml_spec):
            if TEMPLATE.variable_start_string in requires.group(0):
                raise ValueError("%s: 'requires' cannot be templated!"
                                 % yaml_filename)
        context = self._variables if self._variables is not None else self.spec
        from seine import vault as _vault
        try:
            template = PROBE if self._probing else TEMPLATE
            if self._probing:
                self._names.append((yaml_filename,
                    jinja2.meta.find_undeclared_variables(
                        template.parse(yaml_spec)) - {"vault"}))
                return template.from_string(yaml_spec).render(
                    dict(context or {}, vault=lambda ref: ""))
            self._collect_vault_defaults(yaml_spec)
            rendered = template.from_string(yaml_spec).render(
                dict(context or {}, vault=self._vault_lookup))
            return rendered
        except _vault.VaultError as e:
            raise ValueError("%s: %s" % (yaml_filename, e)) from e
        except jinja2.TemplateError as e:
            raise ValueError("%s:%s: %s"
                % (yaml_filename, getattr(e, "lineno", "?"), e)) from e

    # A file renders before it merges, so its own defaults would miss
    # the vault() refs beside them without this.
    def _collect_vault_defaults(self, yaml_spec):
        try:
            parsed = yaml.safe_load(yaml_spec)
        except yaml.YAMLError:
            return
        if type(parsed) == type({}):
            vault = (parsed.get("defaults") or {}).get("vault") if \
                type(parsed.get("defaults")) == type({}) else None
            if type(vault) == type({}):
                self._vault_defaults.update(vault)
        if type(self.spec) == type({}):
            vault = (self.spec.get("defaults") or {}).get("vault") if \
                type(self.spec.get("defaults")) == type({}) else None
            if type(vault) == type({}):
                self._vault_defaults.update(vault)

    # One provider per build, started on first use. Resolved values are
    # recorded so dumps redact them and digests hide them.
    def _vault_lookup(self, ref):
        from seine import vault as _vault
        self._collect_vault_defaults_from_spec()
        if self._vault_provider is None:
            self._vault_provider = _vault.for_build(self._vault_defaults)
        else:
            defaults = getattr(self._vault_provider, "_defaults", None)
            if type(defaults) == type({}) and defaults is not self._vault_defaults:
                defaults.update(self._vault_defaults)
        value = self._vault_provider.kv_read(ref)
        _vault.record_secret(value if isinstance(value, str) else str(value))
        return value

    # Picks up defaults merged since the current file was collected.
    def _collect_vault_defaults_from_spec(self):
        if type(self.spec) == type({}):
            vault = (self.spec.get("defaults") or {}).get("vault") if \
                type(self.spec.get("defaults")) == type({}) else None
            if type(vault) == type({}):
                self._vault_defaults.update(vault)

    # Takes raw text, not a stream, so loads() and load() can share this.
    # A YAML error while probing is swallowed (the lenient render may have
    # left a name empty) -- the real load below reports it properly.
    def _load(self, yaml_filename, yaml_spec):
        if yaml_filename != "<string>":
            path = os.path.realpath(yaml_filename)
            if path not in self.loaded_files:
                self.loaded_files.append(path)
        try:
            spec = yaml.safe_load(self._render(yaml_filename, yaml_spec))
        except yaml.YAMLError:
            if self._probing:
                return self.spec
            raise

        # Patch/kconfig paths are relative to the file listing them; resolve
        # here while we still know which file that was.
        for package in self._package_entries(spec):
            self._resolve_files(package, os.path.dirname(yaml_filename))
            self._record_origins(package, yaml_filename)

        # Host files a playbook reads are relative to the file naming them,
        # like patches: resolve now, while we still know which file that was.
        if yaml_filename != "<string>" and type(spec.get("playbook")) == type([]):
            spec["playbook"] = playbook.resolve(
                spec["playbook"], self._next_to(os.path.dirname(yaml_filename)))

        # A fragment ships its own Ansible modules the way it ships kconfig
        # fragments: 'library/' beside it, found by convention rather than a
        # setting naming it.
        if yaml_filename != "<string>":
            libdir = os.path.join(os.path.dirname(yaml_filename), "library")
            if os.path.isdir(libdir):
                libdir = os.path.realpath(libdir)
                if libdir not in self.options["ansible_library"]:
                    self.options["ansible_library"].append(libdir)

        # 'multiconfig:' paths are relative to the file listing them, like
        # 'patches:'. Resolve now, before merge picks a winning group.
        if yaml_filename != "<string>" and type(spec.get("multiconfig")) == type({}):
            self._resolve_multiconfig(spec["multiconfig"], os.path.dirname(yaml_filename))

        if self.spec is None:
            self.spec = spec
        else:
            self.merge(spec, peer=len(self._loading) <= 1)

        if "requires" in spec:
            for req in spec["requires"]:
                req_path = os.path.join(os.path.dirname(yaml_filename), req)
                req_yml = os.path.normpath("%s.yml" % req_path)
                req_yaml = os.path.normpath("%s.yaml" % req_path)
                if os.path.isfile(req_yml):
                    req_path = req_yml
                elif os.path.isfile(req_yaml):
                    req_path = req_yaml
                else:
                    raise FileNotFoundError("%s: '%s' could not be found in %s/!"
                        % (yaml_filename, req, os.path.dirname(req_path)))
                self.load(req_path)
        return self.spec

    # The path to a file that sits next to the spec naming it; a name that
    # is not there, or leaves the project, stays as written for the
    # playbook scan to judge (it may be for the target to find).
    def _next_to(self, dirname):
        files = self.options.get("files") or []
        roots = [os.getcwd()]
        if files:
            roots.append(os.path.dirname(os.path.abspath(files[0])))

        def fix(name):
            if os.path.isabs(name):
                return name
            path = os.path.normpath(os.path.join(dirname, name))
            inside = any(os.path.commonpath([os.path.abspath(path), r]) == r for r in roots)
            return path if inside and os.path.exists(path) else name
        return fix

    # Every package entry a file holds, whether it is asking for a build or
    # only describing one: both name files relative to the file they are in.
    def _package_entries(self, spec):
        spec = spec or {}
        entries = list(spec.get("packages") or [])
        entries += list((spec.get("defaults") or {}).get("packages") or [])
        return [e for e in entries if type(e) == type({})]

    # Which file wrote each of a package's settings (a package is often
    # described by several files). Nested settings use a dotted path
    # ('extends.kernel.upstream'); stored under an '_'-prefixed key so
    # dump() already hides it.
    ORIGINS = "_origins"

    def _record_origins(self, package, filename, prefix=""):
        origins = package.setdefault(SpecLoader.ORIGINS, {}) if prefix == "" else None
        for setting, value in list(package.items()):
            if setting.startswith("_"):
                continue
            if prefix == "" and setting == "extends" and type(value) == type({}):
                for kind, settings in value.items():
                    if type(settings) != type({}):
                        continue
                    for name in settings:
                        package[SpecLoader.ORIGINS]["extends.%s.%s" % (kind, name)] = filename
                continue
            package[SpecLoader.ORIGINS][setting] = filename
        return package

    # Where a setting was written down, for the messages that ask someone
    # to change it.
    @staticmethod
    def origin_of(package, setting):
        return (package.get(SpecLoader.ORIGINS) or {}).get(setting)

    # The settings of a package that name files, as the path to reach them
    # from the package's own dictionary.
    FILE_LISTS = [["patches"], ["extends", "kernel", "fragments"]]

    def _resolve_files(self, package, dirname):
        for path in SpecLoader.FILE_LISTS:
            holder = package
            for key in path[:-1]:
                holder = holder.get(key) if type(holder) == type({}) else None
            if type(holder) != type({}):
                continue
            names = holder.get(path[-1])
            if type(names) != type([]):
                continue
            holder[path[-1]] = [
                os.path.normpath(os.path.join(dirname, name))
                if type(name) == type("") else name for name in names]

        # 'derived-flavours' nests fragments two levels deeper than
        # FILE_LISTS reaches; resolved here for the same reason: relative
        # to the file that named it, not to the build's own directory.
        extends = package.get("extends")
        kernel = extends.get("kernel") if type(extends) == type({}) else None
        derived = kernel.get("derived-flavours") if type(kernel) == type({}) else None
        if type(derived) == type({}):
            for base, names in derived.items():
                if type(names) != type({}):
                    continue
                for name, fragments in names.items():
                    if type(fragments) != type([]):
                        continue
                    names[name] = [
                        os.path.normpath(os.path.join(dirname, f))
                        if type(f) == type("") else f for f in fragments]

        # A text setting ('copyright') may name a file the same way.
        for settings in (extends.values() if type(extends) == type({}) else []):
            if type(settings) == type({}):
                for name in texts.NAMES:
                    if name in settings:
                        settings[name] = texts.resolve(settings[name], dirname)

        # 'source: file://' names a directory the same way 'patches' names
        # a file -- relative to this spec, not a plain list so FILE_LISTS
        # does not already reach it.
        source = package.get("source")
        if type(source) == type("") and source.startswith("file://"):
            path = source[len("file://"):]
            package["source"] = "file://" + os.path.normpath(
                os.path.join(dirname, path))

    # Like 'requires:': a '.yml'/'.yaml' suffix is optional, and paths
    # are relative to the file naming them.
    def _resolve_multiconfig(self, groups, dirname):
        for name, value in groups.items():
            if type(value) == type([]):
                groups[name] = [self._resolve_spec_file(name, entry, dirname)
                                for entry in value]
            elif type(value) == type({}) and type(value.get("specs")) == type([]):
                value["specs"] = [self._resolve_spec_file(name, entry, dirname)
                                  for entry in value["specs"]]

    def _resolve_spec_file(self, group, entry, dirname):
        if type(entry) != type(""):
            return entry
        path = os.path.normpath(os.path.join(dirname, entry))
        if os.path.isfile(path):
            return path
        for suffix in (".yml", ".yaml"):
            candidate = os.path.normpath("%s%s" % (path, suffix))
            if os.path.isfile(candidate):
                return candidate
        raise FileNotFoundError(
            "'multiconfig: %s' names '%s', which could not be found in %s/ "
            "(with or without a '.yml'/'.yaml' suffix)!"
            % (group, entry, dirname))
