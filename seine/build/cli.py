# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier Apache-2.0

import copy
import getopt
import os
import subprocess
import sys

from seine            import gists, settings
from seine.credentials import CredentialError
from seine.image      import Image
from seine.extends import module
from seine.cmd        import Cmd
from seine.build.credentials import collect_credentials
from seine.build.dump import SpecDump
from seine.build.merger import SpecMerger
from seine.build.spec import SpecLoader
from seine.build.resources import parse_resources
from seine.partition  import PartitionHandler
from seine.tasks      import Interrupted
from seine.container import ContainerEngine
from seine.utils import distribution, locked
from seine.diffing    import remember


class BuildCmd(SpecLoader, SpecMerger, SpecDump, Cmd):
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
        "verbose",
        "worktree="
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
            name: multiconfig.load_group(files, self.options,
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
            elif o in ("--worktree",):
                if a not in ("auto", "sparse", "full"):
                    sys.stderr.write("error: --worktree expects auto, sparse or full\n")
                    sys.exit(1)
                self.options["worktree"] = a
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

        try:
            args = [gists.resolve(arg) for arg in args]
        except ValueError as e:
            sys.stderr.write("error: %s\n" % e)
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
      --worktree MODE   what a remote build uploads: sparse sends only the files
                        the specification reads, full sends the whole directory
                        (minus ignored files), auto (default) is sparse unless
                        the playbook reads files seine cannot list, such as
                        roles
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
