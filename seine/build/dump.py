import copy
import os
import sys
import yaml

from seine.diffing import colorless, diff, recall
from seine.utils import redact, redactions

# Reading a loaded specification back out: as YAML, a file's own text,
# or what changed since the last build. Mixed into BuildCmd.
class SpecDump:
    # The merged specification as YAML, without what only seine needs. On a
    # copy: what is hidden from a reader is still what the build walks.
    def dump(self, spec):
        spec = copy.deepcopy(spec)
        if "image" in spec:
            # hide internal attributes (_foo) but also "priority" settings
            # from "partitions" and "volumes" sections
            for what in [ "partitions", "volumes" ]:
                if what not in spec["image"]:
                    continue
                objects = []
                for o in spec["image"][what]:
                    kvp = {}
                    for k in o:
                        if k.startswith("_") == False and k != "priority":
                            kvp[k] = o[k]
                    objects.append(kvp)
                spec["image"][what] = objects

        if "packages" in spec:
            # hide internal attributes (_foo) and the "priority" settings,
            # as done above for partitions and volumes
            packages = []
            for p in spec["packages"]:
                packages.append({k: v for k, v in p.items()
                                 if k.startswith("_") == False and k != "priority"})
            spec["packages"] = packages

        if "playbook" in spec:
            # hide "hosts" settings from playbooks since they are added by
            # us to make ansible happy
            playbooks = []
            for p in spec["playbook"]:
                p.pop("hosts", None)
                playbooks.append(p)
            spec["playbook"] = playbooks

        # hide the "requires" section since YAML files were supposedly merged
        # together and we now have a consolidated specification
        spec.pop("requires", None)

        # Redacts everywhere the patterns appear, but leaves the 'redact'
        # section itself alone -- its patterns describe what's hidden, and
        # matching itself would hide that.
        rules = redactions(spec)
        for section in spec:
            if section != "redact":
                spec[section] = redact(spec[section], rules, path=(section,))

        # return the spec in YAML format
        return yaml.dump(spec)

    # A single file's own text, not the merged spec -- redacted, but never
    # written back to disk. Refused unless in loaded_files or
    # 'extra_allowed'. Read as-is, no Jinja rendering.
    def dump_file(self, path, extra_allowed=()):
        real = os.path.realpath(path)
        if real not in self.loaded_files and real not in extra_allowed:
            raise ValueError("%s is not one of this build's own loaded files" % path)
        with open(real, "r") as f:
            try:
                spec = yaml.safe_load(f.read()) or {}
            except yaml.YAMLError as e:
                raise ValueError("%s: %s" % (path, e)) from e
        patterns = redactions(self.spec)
        return yaml.dump(redact(spec, patterns))

    # Local files this build's 'packages:' reference (patches, kernel/
    # derived-flavour fragments) -- never 'defaults.packages:', which
    # builds nothing. Read fresh, not cached.
    def referenced_files(self):
        from seine import packages
        try:
            parsed = packages.parse(self.spec)
        except ValueError:
            return set()
        return {os.path.realpath(f) for p in parsed for f in p.referenced_files()}

    # Falls back to a referenced file (patch, kernel fragment) when
    # dump_file() refuses -- redacted as flat text, no YAML round-trip.
    # No path-containment check: the real build already reads whatever
    # a 'patches:'/'fragments:' entry names.
    def read(self, path, extra_allowed=()):
        try:
            return self.dump_file(path, extra_allowed=extra_allowed)
        except ValueError:
            pass
        real = os.path.realpath(path)
        if real not in self.referenced_files():
            raise ValueError(
                "%s is not one of this build's own loaded files, siblings, "
                "or a local file a 'packages:' entry references" % path)
        with open(real, "r") as f:
            text = f.read()
        return redact(text, redactions(self.spec))


    # The same, marked with what changed since these files last built. With
    # no baseline nothing is marked, and stderr says why -- stdout carries
    # the specification, whatever is reading it.
    def changed(self, files, spec):
        baseline = recall(files)
        if baseline is None:
            sys.stderr.write(
                "nothing was built from %s here yet, so there is nothing to "
                "compare this against\n" % ", ".join(files))
        return diff(baseline, self.dump(spec),
                    color=colorless(self.options) == False)
