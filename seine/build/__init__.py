# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier Apache-2.0

import copy
import getopt
import jinja2
import jinja2.meta
import os
import re
import subprocess
import sys
import yaml

from seine            import settings
from seine.credentials import CredentialError
from seine.image      import Image
from seine.extends import module
from seine.cmd        import Cmd
from seine.build.credentials import collect_credentials
from seine.build.dump import SpecDump
from seine.build.merger import SpecMerger
from seine.build.resources import format_resources, parse_resources
from seine.partition  import PartitionHandler
from seine.tasks      import Interrupted
from seine.container import ContainerEngine
from seine.utils import distribution, locked
from seine.utils      import lock_sibling
from seine.diffing    import remember
from seine.extends import texts
from seine.extends.templates import TEMPLATE

# TEMPLATE renders specs before parsing (one file covers several
# archs/releases): a missing value fails loudly, not silently building
# for the wrong machine. PROBE is the same, but unresolved names render
# empty: its output only collects what names get set, then is thrown away.
PROBE = TEMPLATE.overlay(undefined=jinja2.ChainableUndefined)

class BuildCmd(SpecMerger, SpecDump, Cmd):
    # Command name and its '-h' text. A variant of this command (see
    # PlanCmd) overrides these instead of copying main().
    NAME = "build"
    SHORT_OPTIONS = "dDhj:kv"
    LONG_OPTIONS = [
        "ca-cert=",
        "cache-bootstraps",
        "cache-rootfs",
        "debug",
        "dest-dir=",
        "dry-run",
        "dump",
        "help",
        "insecure",
        "jobs=",
        "keep",
        "no-cache-bootstraps",
        "no-color",
        "no-download",
        "offline",
        "min-arch-score=",
        "packages-only",
        "parallel=",
        "prefer-native",
        "project=",
        "rebuild",
        "release",
        "remote=",
        "reproducible",
        "require-hashes",
        "require-native",
        "resource=",
        "rootfs-only",
        "s3-bucket=",
        "s3-cache",
        "s3-endpoint=",
        "s3-offline-mode=",
        "s3-region=",
        "sbom",
        "sign-key=",
        "spec-only",
        "target=",
        "target-arch=",
        "tasks-only",
        "token=",
        "verbose"
    ]

    def __init__(self):
        self.image = None
        # 'jobs' falls back to the persisted setting (see settings.py / '/set
        # jobs N') before the hardcoded '1'; '-j'/'--jobs' below overrides both.
        self.options = { "ansible_library": [], "build": True, "color": None,
                         "cache_bootstraps": True,
                         "cache_rootfs": False,
                         "debug": False, "dest_dir": None, "dry_run": False,
                         "jobs": settings.load().get("jobs") or 1, "keep": False,
                         "min_arch_score": None,
                         "no_download": False, "offline": False,
                         "packages_only": False, "parallel": None,
                         "prefer_native": False,
                         "project": os.environ.get("SEINE_PROJECT"),
                         "rebuild": False, "release": False,
                         "remote": None, "reproducible": False,
                         "require_hashes": False, "require_native": False,
                         "resources": settings.load().get("resources"),
                         "rootfs_only": False,
                         "s3_bucket": None, "s3_cache": False,
                         "s3_endpoint": None, "s3_offline_mode": "fallback",
                         "s3_region": None,
                         "sbom": False, "sign_key": None, "spec": True,
                         "target": None,
                         "tasks": True, "token": os.environ.get("SEINE_TOKEN"),
                         "verbose": False }
        self.partitionHandler = PartitionHandler()
        self.spec = None
        # self.spec exactly as merged, before parse() mutates it in place
        # (size: strings -> byte ints) -- what Inspector needs.
        self.raw_spec = None
        self._loading = []
        # Every real file loaded, in order first reached. Unlike _loading,
        # never popped -- dump_file() checks paths against this.
        self.loaded_files = []
        self._probing = False
        self._variables = None
        self._names = []
        self._prober = None
        self._probed = set()
        # Lazily started on first vault() use, so specs without vault
        # refs build with no vault configured.
        self._vault_provider = None
        # 'defaults: vault:' seeds collected so far, shared with the
        # provider so a miss seeds the spec's throwaway. Remote vaults
        # never see them.
        self._vault_defaults = {}
        # 'multiconfig:' groups this specification declares, name -> the
        # BuildCmd that parsed it -- see _parse_multiconfig(). Empty for
        # a specification with none.
        self.subbuilds = {}

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
            self._prober = BuildCmd()
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
        block = BuildCmd.BLOCKS.search(yaml_spec)
        if block is not None:
            raise ValueError("%s: '%s' blocks are not accepted, only '%s %s' "
                "substitutions -- list the fragments that apply under "
                "'requires' instead!"
                % (yaml_filename, TEMPLATE.block_start_string,
                   TEMPLATE.variable_start_string, TEMPLATE.variable_end_string))
        for requires in BuildCmd.REQUIRES.finditer(yaml_spec):
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
        origins = package.setdefault(BuildCmd.ORIGINS, {}) if prefix == "" else None
        for setting, value in list(package.items()):
            if setting.startswith("_"):
                continue
            if prefix == "" and setting == "extends" and type(value) == type({}):
                for kind, settings in value.items():
                    if type(settings) != type({}):
                        continue
                    for name in settings:
                        package[BuildCmd.ORIGINS]["extends.%s.%s" % (kind, name)] = filename
                continue
            package[BuildCmd.ORIGINS][setting] = filename
        return package

    # Where a setting was written down, for the messages that ask someone
    # to change it.
    @staticmethod
    def origin_of(package, setting):
        return (package.get(BuildCmd.ORIGINS) or {}).get(setting)

    # The settings of a package that name files, as the path to reach them
    # from the package's own dictionary.
    FILE_LISTS = [["patches"], ["extends", "kernel", "fragments"]]

    def _resolve_files(self, package, dirname):
        for path in BuildCmd.FILE_LISTS:
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


    def parse(self):
        if self.image is None:
            self.image = Image(self.partitionHandler, self.options)
        self._apply_defaults()
        self.raw_spec = copy.deepcopy(self.spec)
        # A vendor-only spec (no image:/packages:/playbook:) skips both
        # parsers -- nothing to build. 'vendor:' is still validated here so a
        # typo is caught now, not later in 'seine vendor'.
        if "image" in self.spec:
            self.spec = self.partitionHandler.parse(self.spec)
            self.spec = self.image.parse(self.spec)
            module.check_kbuild(self.image.packages)
        elif "initrd" in self.spec or "packages" in self.spec or "playbook" in self.spec or "containers" in self.spec:
            # No 'image:' section, but something to build: the root
            # file-system tarball itself becomes this build's real
            # output (Image.parse()/own_tasks()).
            self.spec = self.image.parse(self.spec)
            module.check_kbuild(self.image.packages)
        else:
            from seine import vendor
            distro = distribution(self.spec)
            vendor.suites(vendor.parse(self.spec), distro)
            vendor.exclusions(self.spec)
        self._parse_multiconfig()
        return self.spec

    # Loads each 'multiconfig:' group as its own sub-build (like a CLI
    # '--' group). A sub-group's own 'image:' never reaches this spec --
    # only self.spec's 'image:' owns the disk. 'after'/'before' are
    # resolved into one 'after' set per group before loading, so
    # Image.tasks() has it ready to wire each group's 'needs'.
    def _parse_multiconfig(self):
        from seine import multiconfig
        groups = self.spec.get("multiconfig") or {}
        parsed = {name: multiconfig._parse_group(name, value)
                  for name, value in groups.items()}
        after = multiconfig.resolve_order(parsed)
        self.subbuilds = {
            name: multiconfig._load(files, self.options,
                                    defer_uki_check=len(after[name]) > 0)
            for name, (files, _after, _before) in parsed.items()}
        self.image.subbuilds = self.subbuilds
        self.image.multiconfig_after = after

    def build(self, reporter=None):
        if self.spec is None or self.image is None:
            raise RuntimeError("no specification was loaded or parsed!")
        return self.image.build(reporter=reporter)

    # Prunes intermediate images once, after everything's built, not
    # after each image. 'podman image prune' is machine-wide, so it's
    # skipped (not blocked on) when another build already holds the lock.
    def _prune(self):
        try:
            with locked(ContainerEngine.storage_lock(), blocking=False):
                ContainerEngine.run(["image", "prune", "-f"], check=False)
        except BlockingIOError:
            pass

    def usage(self):
        return USAGE

    def main(self, argv):
        # gnu_getopt would eat the first '--', which separates the groups.
        cut = argv.index("--") if "--" in argv else len(argv)
        try:
            opts, args = getopt.gnu_getopt(argv[:cut], self.SHORT_OPTIONS, self.LONG_OPTIONS)
            args += argv[cut:]
        except getopt.GetoptError as err:
            sys.stderr.write(str(err))
            sys.stderr.write(self.usage())
            sys.exit(1)
        for o, a in opts:
            if o in ("-d", "--debug"):
                self.options["debug"] = True
                self.options["verbose"] = True
            elif o in ("-h", "--help"):
                print(self.usage())
                sys.exit()
            elif o in ("-j", "--jobs"):
                # How many build steps may run at once. Defaults to 1 -- the
                # ordering/output a build has always had, and the easiest to debug.
                try:
                    self.options["jobs"] = int(a)
                except ValueError:
                    sys.stderr.write("error: --jobs expects a number\n")
                    sys.exit(1)
                if self.options["jobs"] < 1:
                    sys.stderr.write("error: --jobs shall be at least 1\n")
                    sys.exit(1)
            elif o in ("-k", "--keep"):
                self.options["keep"] = True
            elif o in ("--no-color"):
                self.options["color"] = False
            elif o in ("--spec-only"):
                self.options["tasks"] = False
            elif o in ("--tasks-only"):
                self.options["spec"] = False
            elif o in ("--packages-only"):
                self.options["packages_only"] = True
            elif o in ("--rootfs-only"):
                self.options["rootfs_only"] = True
            elif o in ("--target"):
                # Validated later against the task graph (Image.tasks()), not
                # here -- task names depend on the parsed spec, not just the CLI.
                self.options["target"] = a
            elif o in ("--dry-run"):
                self.options["dry_run"] = True
            elif o in ("-D", "--dump"):
                self.options["build"] = False
            elif o in ("--parallel"):
                # Cores one package build may use. Left unset it follows
                # --jobs, so raising --jobs divides the machine instead of
                # multiplying it.
                try:
                    self.options["parallel"] = int(a)
                except ValueError:
                    sys.stderr.write("error: --parallel expects a number\n")
                    sys.exit(1)
                if self.options["parallel"] < 1:
                    sys.stderr.write("error: --parallel shall be at least 1\n")
                    sys.exit(1)
            elif o in ("--require-hashes"):
                self.options["require_hashes"] = True
            elif o in ("--resource"):
                # A class no --resource names falls back to --jobs.
                try:
                    self.options["resources"] = parse_resources(
                        a, self.options["resources"])
                except ValueError as e:
                    sys.stderr.write("error: --resource %s\n" % e)
                    sys.exit(1)
            elif o in ("--rebuild"):
                self.options["rebuild"] = True
            elif o in ("--reproducible"):
                self.options["reproducible"] = True
            elif o in ("--offline",):
                self.options["offline"] = True
            elif o in ("--cache-bootstraps",):
                self.options["cache_bootstraps"] = True
            elif o in ("--no-cache-bootstraps",):
                self.options["cache_bootstraps"] = False
            elif o in ("--cache-rootfs",):
                self.options["cache_rootfs"] = True
            elif o in ("--s3-cache",):
                self.options["s3_cache"] = True
            elif o in ("--s3-endpoint",):
                self.options["s3_endpoint"] = a
            elif o in ("--s3-bucket",):
                self.options["s3_bucket"] = a
            elif o in ("--s3-region",):
                self.options["s3_region"] = a
            elif o in ("--s3-offline-mode",):
                if a not in ("fallback", "strict"):
                    sys.stderr.write("error: --s3-offline-mode must be 'fallback' or 'strict'\n")
                    sys.exit(1)
                self.options["s3_offline_mode"] = a
            elif o in ("--prefer-native",):
                self.options["prefer_native"] = True
            elif o in ("--require-native",):
                self.options["require_native"] = True
            elif o in ("--min-arch-score",):
                try:
                    self.options["min_arch_score"] = float(a)
                except ValueError:
                    sys.stderr.write("error: --min-arch-score expects a float\n")
                    sys.exit(1)
            elif o in ("--remote",):
                self.options["remote"] = a
            elif o in ("--ca-cert",):
                self.options["ca_cert"] = a
            elif o in ("--insecure",):
                self.options["insecure"] = True
            elif o in ("--dest-dir",):
                self.options["dest_dir"] = a
            elif o in ("--no-download",):
                self.options["no_download"] = True
            elif o in ("--token",):
                self.options["token"] = a
            elif o in ("--project",):
                self.options["project"] = a
            elif o in ("--release",):
                self.options["release"] = True
            elif o in ("--target-arch",):
                self.options["target_arch"] = a
            elif o in ("--sign-key"):
                self.options["sign_key"] = a
            elif o in ("--sbom"):
                self.options["sbom"] = True
            elif o in ("-v", "--verbose"):
                self.options["verbose"] = True
            else:
                assert False, "unhandled option"

        if len(args) == 0:
            sys.stderr.write("error: %s command expects a YAML file\n" % self.NAME)
            sys.exit(1)

        if self.options.get("remote"):
            from seine.distributed.client.remote import build_remote
            sys.exit(
                build_remote(
                    server_url=self.options["remote"],
                    project=self.options.get("project"),
                    spec_files=args,
                    options=self.options,
                    token=self.options.get("token"),
                    is_release=self.options.get("release", False),
                )
            )

        try:
            # '--' separates groups of files: several images, one scheduler. A
            # single group takes the path below as always; multiconfig.run()
            # handles more than one.
            from seine import multiconfig
            groups = multiconfig.split(args)
            if len(groups) > 1:
                sys.exit(multiconfig.run(groups, self.options))

            self.options["files"] = args
            self.load_all(args)

            spec = self.parse()
            result = 0
            if self.options["build"] == False:
                print(self.dump(spec))
            elif self.options["dry_run"]:
                # What a build would build, then how. No lock is taken and
                # nothing is pruned: a dry run writes no storage, so it has
                # nothing to wait for.
                if self.options["spec"]:
                    print(self.changed(args, spec))
                if self.options["tasks"]:
                    result = self.build()
            else:
                collect_credentials([self])
                # Taken before the build, which writes into the
                # specification as it goes -- the ansible runner puts each
                # playbook's environment there. Taken after, every playbook
                # would differ from the one a plan renders.
                recorded = self.dump(spec)
                # Shared: another build in a different terminal runs alongside
                # this one. What can't run alongside is anything sweeping
                # storage -- 'seine cache clear', and the prune below.
                with locked(ContainerEngine.storage_lock(), shared=True):
                    result = self.build()
                self._prune()
                # What the next plan compares against, recorded only for a
                # build that finished: build() returns None on success and
                # the code it failed with otherwise.
                if not result:
                    remember(args, recorded)
            sys.exit(result)

        except OSError as e:
            sys.stderr.write("error: couldn't open build YAML file: {0}\n".format(e))
            sys.exit(2)
        except CredentialError as e:
            sys.stderr.write("error: %s\n" % e)
            sys.exit(3)
        except ValueError as e:
            sys.stderr.write("error: YAML file is invalid: {0}\n".format(e))
            sys.exit(3)
        except subprocess.CalledProcessError as e:
            sys.stderr.write("error: build failed: {0}\n".format(e))
            sys.exit(4)
        # 128 plus SIGINT, as a shell reports it. A second Ctrl-C arrives as
        # a KeyboardInterrupt: the same answer, without the traceback.
        except (Interrupted, KeyboardInterrupt) as e:
            sys.stderr.write("error: build was %s\n" % (str(e) or "interrupted"))
            sys.exit(130)

# Everything 'build' does, minus doing it. Same command with one
# option decided for it, not a second implementation -- a plan's
# value depends on it being the exact graph a build would walk.
class PlanCmd(BuildCmd):
    NAME = "plan"

    # What is left says how the plan is printed, not what is in it. For the
    # plan of a build with particular options, 'seine build --dry-run'
    # still takes them all.
    SHORT_OPTIONS = "h"
    LONG_OPTIONS = ["help", "no-color", "spec-only", "tasks-only"]

    def __init__(self):
        super().__init__()
        self.options["dry_run"] = True

    def usage(self):
        return PLAN_USAGE

USAGE = """
Build an image using instructions from specifications files

Description:
  Builds an Embedded Linux image using instructions from one or more specification
  files defining the base distribution and the Ansible playbooks to execute to
  customize the image.

Usage:
  seine build [options] SPEC... [-- SPEC...]...

  '--' separates groups of specification files, each the same thing a
  single 'seine build' already takes -- one image per group, several
  built together under one scheduler, sharing what their specifications
  agree on: see 'Building several images together' in docs/building.md.

Examples:
  seine build demo-image.yml
  seine build -v demo-image.yml
  seine build pc-image.yml -- rpi4-image.yml

Flags:
  -d, --debug           print debug messages
  --dry-run             do not build anything, print the steps the build would
                        run and the packages it would leave alone
  -D, --dump            do not build the image, just dump the consolidated specification
  -h, --help            print this message
  -j, --jobs N          run up to N steps of the build at once (1 by default).
                        Steps that depend on each other still wait; what a
                        step's containers print goes to a file of its own
                        while more than one is running
  -k, --keep            keep temporary files
      --no-color        print the specification of a '--dry-run' without
                        colour. NO_COLOR says the same thing, and a plan
                        going anywhere but a terminal is plain anyway
      --packages-only   build the packages of the 'packages' section and stop,
                        without assembling a root file-system or writing an
                        image. What a machine filling a cache for others to
                        import runs, since the packages are the half worth
                        carrying
      --parallel N      cores one package build may use. Unset, it is derived
                        from --jobs so that the builds running together do not
                        ask for more of the machine than it has
      --ca-cert PATH    CA bundle to verify the remote server's TLS certificate
                        with ($SEINE_CA_CERT says the same thing)
      --insecure        allow plain http:// to a remote server that is not on
                        this machine. Without it only https:// is accepted
      --dest-dir PATH   custom directory to download build artifacts into
      --no-download     skip automatic artifact download after remote completion
      --project NAME    project for a remote build (default: $SEINE_PROJECT, else
                        your default project on the server, else it asks)
      --release         mark remote build as a release build (requires releaser or admin role)
      --remote URL      dispatch build to a remote seine-server. Ctrl+C asks the
                        server to cancel the build (exit status 130). Exit
                        status: 0 completed, 1 failed, 2 usage or server error
      --token TOKEN     bearer token for remote server authentication
                        ($SEINE_TOKEN says the same thing). Without one,
                        a token saved in the keyring or credentials.json is
                        used, else it is asked for and may be saved
  --sign-key KEY        sign the rebuilt packages and the repository holding
                        them with this gpg key, named however gpg will take it
                        -- a key id, a fingerprint, an email address. gpg runs
                        on this machine and talks to your agent, so seine
                        never sees the key itself. SEINE_SIGN_KEY says the
                        same thing
  --rebuild             rebuild the packages of the 'packages' section even if
                        they were built before
  --reproducible        normalize disk image partitions so two builds of the
                        same spec produce byte-identical images. Off by
                        default: slower, only CI/release builds usually
                        need it
  --require-hashes      refuse to build when a source is fetched over http with
                        no sha256 to check it against. Reported when the
                        specification is parsed, before anything is downloaded
  --resource CLASS=N    capacity N for a resource class steps may cost against
                        (e.g. 'net=2', 'io=4'). A class not given this falls
                        back to --jobs; may be given more than once
  --rootfs-only         build the root file-system as a tarball and stop,
                        without writing a disk image. What looking inside a
                        build rather than booting it wants
      --cache-rootfs    push and pull rootfs tarballs to and from network cache
      --no-cache-bootstraps
                        disable remote caching of bootstrap container images
      --s3-cache        enable remote network caching backed by S3 or Garage
      --s3-endpoint URL endpoint URL for S3/Garage network cache storage
      --s3-bucket NAME  bucket name for S3 cache (default: 'seine-cache')
      --s3-region NAME  region name for S3 signature (default: 'garage')
      --s3-offline-mode MODE
                        network cache failure behavior: 'fallback' (build
                        locally on failure, default) or 'strict' (abort)
  --sbom                produce a Software Bill of Materials (SBOM) using
                        debsbom
  --spec-only           with '--dry-run', print the specification and not the
                        steps
  --target TASK         build just this one task and whatever it needs (as
                        'plan' names them, e.g. 'package:linux') and stop --
                        note this is what it needs, not what needs it, so a
                        package alone does not reach the repository, which
                        is 'deploy:<name>' 's job
  --tasks-only          with '--dry-run', print the steps and not the
                        specification
  -v, --verbose         produce verbose output while building the image

"""

PLAN_USAGE = """
Say what a build would do, without doing any of it

Description:
  Prints the specification these files merge into, and then the steps a build
  of it would run, in the order it would run them and with what each waits
  for, and the packages it would leave alone with the stamp that says why.

  The specification is printed as a diff against the one these same files
  last built, so that what changed since is what stands out: added lines on
  green, removed lines on red, and what did not change folded away around
  them. Only a build records one, so files that have not built here before
  have nothing to compare against: their specification is printed as it is,
  and stderr says why nothing in it is marked.

  The plan is not a description of the build: it is the same graph a build
  walks, printed instead of walked. So a package already built from exactly
  these inputs has no steps in it at all -- which is the useful half of the
  answer.

  Nothing is fetched, built or written. 'seine build --dry-run' is the same
  thing.

Usage:
  seine plan [options] SPEC... [-- SPEC...]...

  '--' groups specification files the same way 'seine build' takes them;
  see there for what running several together means.

Examples:
  seine plan demo-image.yml
  seine plan --spec-only demo-image.yml
  seine plan pc-image.yml -- rpi4-image.yml

Flags:
  -h, --help            print this message
      --no-color        print the specification without colour. NO_COLOR says
                        the same thing, and a plan going anywhere but a
                        terminal is plain anyway
      --spec-only       print the specification and not the steps
      --tasks-only      print the steps and not the specification

  And nothing else: these say how the plan is printed, not what is in it. A
  plan is the same whoever asks for it. For the plan of a build with
  particular options, 'seine build --dry-run' takes all of them.

"""
