import os

from seine.extends import registry

# Merging one loaded specification into the one built so far, section
# by section. Mixed into BuildCmd: it works on self.spec.
class SpecMerger:
    # direction: most-specific file wins (docs/merging.md).
    def _merge_distro(self, spec):
        if "distribution" in spec:
            if "distribution" in self.spec:
                for setting in spec["distribution"]:
                    if setting == "feeds":
                        self._merge_feeds(spec["distribution"]["feeds"])
                        continue
                    if setting == "architectures":
                        self._merge_distro_architectures(
                            spec["distribution"]["architectures"])
                        continue
                    self.spec["distribution"][setting] = spec["distribution"][setting]
            elif "distribution" not in self.spec:
                self.spec["distribution"] = spec["distribution"]

    # Feeds merge by suite (like partitions/volumes merge by label), so
    # adding one feed doesn't require restating the others.
    #
    # direction: most-specific file wins, per setting within a matched
    # suite (docs/merging.md).
    def _merge_feeds(self, feeds):
        merged = self.spec["distribution"].get("feeds")
        if merged is None:
            self.spec["distribution"]["feeds"] = feeds
            return

        for feed in feeds:
            suite = feed.get("suite") if type(feed) == type({}) else None
            existing = [f for f in merged
                        if type(f) == type({}) and f.get("suite") == suite]
            if suite is not None and len(existing) > 0:
                existing[0].update(feed)
            else:
                merged.append(feed)

    # Unlike other 'distribution:' settings, 'architectures' (plural)
    # accumulates across fragments instead of the last one winning.
    # 'architecture' (singular, this run's target) still last-wins.
    #
    # direction: additive, deduplicated (docs/merging.md).
    def _merge_distro_architectures(self, architectures):
        for arch in architectures:
            archs = self.spec["distribution"].setdefault("architectures", [])
            if arch not in archs:
                archs.append(arch)

    # direction: most-specific file wins (docs/merging.md).
    def _merge_imager(self, spec):
        if "imager" in spec:
            if "imager" in self.spec:
                for setting in spec["imager"]:
                    self.spec["imager"][setting] = spec["imager"][setting]
            else:
                self.spec["imager"] = spec["imager"]

    # A group's file list is replaced outright when named again, not
    # extended -- a board file fully overrides what a shared fragment
    # asked a group to load.
    #
    # direction: most-specific file wins (docs/merging.md).
    def _merge_multiconfig(self, spec):
        if "multiconfig" in spec:
            if "multiconfig" in self.spec:
                for name in spec["multiconfig"]:
                    self.spec["multiconfig"][name] = spec["multiconfig"][name]
            else:
                self.spec["multiconfig"] = spec["multiconfig"]

    # Merged by name so a fragment reached twice via two 'requires:'
    # paths doesn't duplicate its playbook entry. 'tasks:' stays additive
    # (order matters for ansible), not merged task-by-task.
    #
    # direction: asking file wins within 'requires:'; a peer file amends
    # by field instead (docs/merging.md).
    def _merge_playbooks(self, spec, peer=False):
        if "playbook" not in spec:
            return
        if "playbook" not in self.spec:
            self.spec["playbook"] = spec["playbook"]
            return
        self._merge_named_list(self.spec["playbook"], spec["playbook"],
                               self._name_of,
                               lambda e, n: self._merge_playbook_entry(e, n, peer=peer))

    # direction: asking file wins within 'requires:', peer amends
    # instead; 'tasks' additive either way (docs/merging.md).
    def _merge_playbook_entry(self, entry, newentry, peer=False):
        self._merge_settings(entry, newentry, appends=lambda s: s == "tasks", peer=peer)

    # Merged by name like 'packages:', so a fragment reached twice via
    # two 'requires:' paths amends its entry instead of duplicating it.
    # See seine.testing for what a 'test:' entry holds and how it runs.
    #
    # direction: asking file wins within 'requires:'; a peer file amends
    # by field instead (docs/merging.md).
    def _merge_tests(self, spec, peer=False):
        if "test" not in spec:
            return
        if "test" not in self.spec:
            self.spec["test"] = spec["test"]
            return
        self._merge_named_list(self.spec["test"], spec["test"],
                               self._name_of,
                               lambda e, n: self._merge_test_entry(e, n, peer=peer))

    # direction: asking file wins within 'requires:', peer amends
    # instead; 'tests'/'keywords' merge by name below, 'keywords' stays
    # identity-or-error either way (docs/merging.md).
    def _merge_test_entry(self, entry, newentry, peer=False):
        skip = set()
        if "tests" in newentry and type(entry.get("tests")) == type([]):
            self._merge_named_list(entry["tests"], newentry["tests"],
                                   self._name_of,
                                   lambda e, n: self._merge_test_case(e, n, peer=peer))
            skip.add("tests")
        if "keywords" in newentry and type(entry.get("keywords")) == type([]):
            self._merge_named_list(entry["keywords"], newentry["keywords"],
                                   self._name_of, self._merge_keyword)
            skip.add("keywords")
        if "variables" in newentry and type(entry.get("variables")) == type({}):
            for name, value in newentry["variables"].items():
                if peer or name not in entry["variables"]:
                    entry["variables"][name] = value
            skip.add("variables")
        self._merge_settings(entry, newentry,
                             appends=lambda s: s in ("library", "tags"), skip=skip,
                             peer=peer)

    # Two 'steps:' for the same case name is likely a mistake, not
    # deliberate composition -- raised rather than silently keeping the
    # first (same identity-or-error rule as _merge_keyword()).
    #
    # direction: asking file wins for settings within 'requires:', peer
    # amends instead; 'steps' is identity-or-error regardless (docs/merging.md).
    def _merge_test_case(self, case, newcase, peer=False):
        if "steps" in newcase and "steps" in case and case["steps"] != newcase["steps"]:
            raise ValueError(
                "test case '%s' is defined differently by two 'test:' "
                "entries -- give one of them a different name if they "
                "are meant to be two cases" % case.get("name"))
        self._merge_settings(case, newcase, appends=lambda s: s == "tags",
                             skip=("steps",) if "steps" in case else (), peer=peer)

    # Two fragments defining the same keyword (reached via two
    # 'requires:' paths) is fine only if identical -- caught here at
    # load time instead of surfacing later when 'seine test' runs.
    #
    # direction: identity-or-error -- never silently overrides
    # (docs/merging.md).
    def _merge_keyword(self, keyword, newkeyword):
        plain = {k: v for k, v in keyword.items() if k != self.ORIGINS}
        newplain = {k: v for k, v in newkeyword.items() if k != self.ORIGINS}
        if plain != newplain:
            raise ValueError(
                "keyword '%s' is defined differently by two 'test:' "
                "entries -- give one of them a different name if they "
                "are meant to be two keywords" % plain.get("name"))

    # Generic named-entry merge: an entry from 'new' matching one already
    # in 'existing' (by name_of()) is folded into it with merge_entry(),
    # otherwise appended. Shared by packages, playbook, and test merging.
    #
    # direction: set by the caller's merge_entry (docs/merging.md).
    def _merge_named_list(self, existing, new, name_of, merge_entry):
        for entry in new:
            name = name_of(entry)
            match = [e for e in existing if name is not None and name_of(e) == name]
            if len(match) == 0:
                existing.append(entry)
            else:
                merge_entry(match[0], entry)

    # First-loaded-wins per setting, unless appends(setting) says to add
    # together instead (_added()). 'skip' is whatever the caller already
    # merged itself. 'peer' flips the direction for a file reached
    # outside 'requires:', which amends instead of losing.
    #
    # direction: asking file wins (docs/merging.md); peer amends instead.
    def _merge_settings(self, entry, newentry, appends=lambda setting: False,
                        skip=(), peer=False):
        for setting in newentry:
            if setting == self.ORIGINS or setting in skip:
                continue
            if appends(setting) and setting in entry:
                entry[setting] = self._added(entry[setting], newentry[setting])
                self._take_origin(entry, newentry, setting)
            elif setting not in entry or peer:
                entry[setting] = newentry[setting]
                self._take_origin(entry, newentry, setting)

    # The name_of() every _merge_named_list() caller that matches by a
    # plain 'name' field can share -- packages matches by source package
    # name instead (its own callback), but playbook/test entries and any
    # future named-list section need nothing more than this.
    def _name_of(self, entry):
        if type(entry) != type({}):
            return None
        name = entry.get("name")
        return name if type(name) == type("") else None

    # direction: asking file wins within 'requires:', peer amends
    # instead (docs/merging.md).
    def _merge_packages(self, spec, peer=False):
        if "packages" not in spec:
            return
        if "packages" not in self.spec:
            self.spec["packages"] = spec["packages"]
            return
        self._merge_named_list(self.spec["packages"], spec["packages"],
                               self._package_name,
                               lambda e, n: self._merge_package(e, n, peer=peer))

    # 'vendor:' is a list of asks, merged by name like other named
    # lists. A lock file's 'vendor:' is instead a dict keyed by suite
    # (already-resolved versions) -- told apart by type(), and kept in
    # '_vendor_lock' so existing readers (vendor.py's parse()) still see
    # a plain list.
    #
    # direction: asking file wins within 'requires:', peer amends
    # instead (docs/merging.md).
    def _merge_vendor(self, spec, peer=False):
        if "vendor" not in spec:
            return
        incoming = spec["vendor"]
        if type(incoming) == type({}):
            self.spec.setdefault("_vendor_lock", {}).update(incoming)
            return
        if "vendor" not in self.spec:
            self.spec["vendor"] = incoming
            return
        self._merge_named_list(self.spec["vendor"], incoming,
                               self._vendor_name,
                               lambda e, n: self._merge_settings(e, n, peer=peer))

    def _vendor_name(self, entry):
        if type(entry) != type({}):
            return None
        name = entry.get("name")
        return name if type(name) == type("") else None

    # Additive, deduplicated -- like 'redact': the file that knows a
    # build-dep isn't worth vendoring is rarely the file a vendor run
    # starts from.
    #
    # direction: additive, deduplicated -- order doesn't matter
    # (docs/merging.md).
    def _merge_vendor_exclude(self, spec):
        for name in spec.get("vendor-exclude") or []:
            excluded = self.spec.setdefault("vendor-exclude", [])
            if name not in excluded:
                excluded.append(name)

    # A 'defaults' package entry describes a package without asking to
    # build it (e.g. which kernel flavour is meant, without forcing a
    # rebuild). Last file wins here, unlike 'packages:' -- so a board
    # file overrides the architecture file it sits on.
    #
    # 'defaults: vault:' is unrelated to packages: fixed dev-only
    # key/secret material so independent dev-vault builds of the same
    # spec agree instead of each generating their own (vault/dev.py).
    # Merged by name, later files overriding same-named entries.
    #
    # 'defaults: sign-key' names the repository signing key when neither
    # --sign-key nor SEINE_SIGN_KEY does -- weakest of the three, so the
    # machine always wins over the spec. A later file overrides.
    #
    # 'defaults: apt:' sets what a playbook's apt tasks do unless a task
    # says otherwise. A later file overrides, setting by setting.
    #
    # direction: most-specific file wins (docs/merging.md).
    def _merge_defaults(self, spec):
        if "defaults" not in spec:
            return
        defaults = spec["defaults"]
        if type(defaults) != type({}):
            raise ValueError("'defaults' shall be a dictionary!")
        for setting in defaults:
            if setting not in ("packages", "extends", "vault", "sign-key", "apt"):
                raise ValueError(
                    "'defaults' holds package entries, 'extends', 'vault', "
                    "'sign-key' or 'apt', not '%s'" % setting)

        merged = self.spec.setdefault("defaults", {}).setdefault("packages", [])
        for package in defaults.get("packages") or []:
            name = self._package_name(package)
            existing = [p for p in merged if self._package_name(p) == name]
            if name is None or len(existing) == 0:
                merged.append(package)
            else:
                self._override_package(existing[0], package)

        kinds = defaults.get("extends")
        if kinds is not None:
            registry.check_defaults(kinds)
            self.spec["defaults"].setdefault("extends", {}).update(kinds)

        vault = defaults.get("vault")
        if vault is not None:
            if type(vault) != type({}):
                raise ValueError("'defaults: vault' shall be a dictionary")
            self.spec["defaults"].setdefault("vault", {}).update(vault)

        sign_key = defaults.get("sign-key")
        if sign_key is not None:
            if type(sign_key) != type(""):
                raise ValueError("'defaults: sign-key' shall be a key name")
            self.spec["defaults"]["sign-key"] = sign_key

        apt = defaults.get("apt")
        if apt is not None:
            if type(apt) != type({}):
                raise ValueError("'defaults: apt' shall be a dictionary")
            for setting, value in apt.items():
                if setting != "install_recommends":
                    raise ValueError(
                        "'defaults: apt' holds 'install_recommends', not '%s'"
                        % setting)
                if type(value) != type(True):
                    raise ValueError(
                        "'defaults: apt: install_recommends' shall be true "
                        "or false")
            self.spec["defaults"].setdefault("apt", {}).update(apt)

    # 'overrides' changes seine's own default behaviour (e.g. installing
    # every locale) rather than standing in for a value found elsewhere.
    # Last file wins, same direction as 'defaults'.
    def _merge_overrides(self, spec):
        if "overrides" not in spec:
            return
        overrides = spec["overrides"]
        if type(overrides) != type({}):
            raise ValueError("'overrides' shall be a dictionary!")
        for setting in overrides:
            if setting not in ("locales",):
                raise ValueError("'overrides' holds 'locales', not '%s'" % setting)

        locales = overrides.get("locales")
        if locales is not None:
            if type(locales) != type([]):
                raise ValueError("'overrides: locales' shall be a list")
            self.spec.setdefault("overrides", {})["locales"] = locales

    # As _merge_package(), with the two files the other way round: what the
    # later one says replaces what the earlier one did.
    def _override_package(self, package, newpackage):
        for setting in newpackage:
            if setting == self.ORIGINS:
                continue
            if setting == "extends" and type(package.get(setting)) == type({}):
                for kind in newpackage[setting]:
                    if type(package[setting].get(kind)) != type({}):
                        package[setting][kind] = newpackage[setting][kind]
                    else:
                        for name, value in (newpackage[setting][kind] or {}).items():
                            if self._appends(kind, name):
                                value = self._added(
                                    package[setting][kind].get(name), value,
                                    kind, name)
                            package[setting][kind][name] = value
                    for name in newpackage[setting][kind] or []:
                        self._take_origin(package, newpackage,
                                          "extends.%s.%s" % (kind, name))
            else:
                package[setting] = newpackage[setting]
                self._take_origin(package, newpackage, setting)

    # Folds defaults into the packages actually asked for, once every
    # file is read. Parsed first so a typo is reported by the file that
    # has it, not by whichever image happens to build a kernel.
    def _apply_defaults(self):
        from seine.packages import Package

        held = self.spec.get("defaults") or {}
        defaults = held.pop("packages", None) or []
        kinds = held.pop("extends", None) or {}
        # True is apt's own default: only a false moves the rootfs digest.
        # Dropped here, not per file, so a later true can undo a false.
        apt = held.get("apt") or {}
        if apt.get("install_recommends") is True:
            del apt["install_recommends"]
        if len(apt) == 0:
            held.pop("apt", None)
        # 'vault' stays: Builder/Imager read it later, once the vault is
        # actually needed. Only drop 'defaults' once nothing is left.
        if len(held) == 0:
            self.spec.pop("defaults", None)
        for index, default in enumerate(defaults):
            Package(default, index)
            self._drop_unbuilt_kernels(default)
            name = self._package_name(default)
            for package in self.spec.get("packages") or []:
                if self._package_name(package) == name:
                    self._merge_package(package, default)
        for package in self.spec.get("packages") or []:
            if type(package) == type({}):
                registry.fill_defaults(package.get("extends"), kinds)

    # A default may name a kernel this spec doesn't build -- not a
    # mistake, just "if we build our own kernel, add modules to it too".
    # Drop kernels nothing builds; under 'packages:' that stays an error.
    # Only bare names are dropped, not 'apt://' kernels (the distro's own).
    def _drop_unbuilt_kernels(self, default):
        module = (default.get("extends") or {}).get("module")
        if type(module) != type({}):
            return
        built = {self._package_name(package)
                 for package in self.spec.get("packages") or []}
        for setting, kernels in module.items():
            if self._appends("module", setting) == False:
                continue
            if type(kernels) != type([]):
                continue
            module[setting] = [
                kernel for kernel in kernels
                if type(kernel) != type("") or "://" in kernel
                or kernel in built]

    # direction: asking file wins within 'requires:', peer amends
    # instead; 'extends:' recurses the same way (docs/merging.md).
    def _merge_package(self, package, newpackage, peer=False):
        skip = set()
        if "extends" in newpackage and type(package.get("extends")) == type({}):
            self._merge_extends(package["extends"], newpackage["extends"],
                                package, newpackage, peer=peer)
            skip.add("extends")
        self._merge_settings(package, newpackage, skip=skip, peer=peer)

    # A setting and the file that wrote it move together, so copying a
    # whole 'extends' block also copies each nested setting's origin.
    def _take_origin(self, package, newpackage, setting):
        origins = newpackage.get(self.ORIGINS) or {}
        taken = {name: origin for name, origin in origins.items()
                 if name == setting or name.startswith("%s." % setting)}
        if len(taken) > 0:
            package.setdefault(self.ORIGINS, {}).update(taken)

    # 'extends' is a dict of kinds; two files describing the same kernel
    # describe the same 'kernel' entry rather than replacing each other.
    #
    # direction: asking file wins within 'requires:', peer amends
    # instead, unless _appends() says the setting is additive either way
    # (docs/merging.md).
    def _merge_extends(self, extends, newextends, package, newpackage, peer=False):
        if type(newextends) != type({}):
            return
        for kind in newextends:
            if type(extends.get(kind)) != type({}) or type(newextends[kind]) != type({}):
                if kind not in extends:
                    extends[kind] = newextends[kind]
                    for setting in newextends[kind] or []:
                        self._take_origin(package, newpackage,
                                          "extends.%s.%s" % (kind, setting))
                continue
            for setting in newextends[kind]:
                if self._appends(kind, setting):
                    extends[kind][setting] = self._added(
                        extends[kind].get(setting), newextends[kind][setting],
                        kind, setting)
                    self._take_origin(package, newpackage,
                                      "extends.%s.%s" % (kind, setting))
                elif setting not in extends[kind] or peer:
                    extends[kind][setting] = newextends[kind][setting]
                    self._take_origin(package, newpackage,
                                      "extends.%s.%s" % (kind, setting))

    # Settings two files add to rather than settle between them: which
    # kernels a module targets, and 'kernel: derived-flavours'/'configs'
    # -- "first stands" would silently drop what a second file added.
    def _appends(self, kind, setting):
        from seine.extends.module import MODULE_KERNELS
        if kind == "module" and MODULE_KERNELS.match(setting) is not None:
            return True
        return kind == "kernel" and setting in ("derived-flavours", "configs")

    # Union of two lists (order preserved, no dupes) -- or for dicts,
    # merged key by key: 'configs' one group at a time, 'derived-flavours'
    # one base at a time, rather than the second replacing the first.
    def _added(self, listed, added, kind=None, setting=None):
        if kind == "kernel" and setting == "configs":
            return self._added_configs(listed or {}, added or {})
        if type(listed) == type({}) or type(added) == type({}):
            if type(listed) != type({}) or type(added) != type({}):
                return added if type(added) == type({}) else listed
            merged = {base: dict(names) for base, names in listed.items()}
            for base, names in added.items():
                merged.setdefault(base, {}).update(names)
            return merged
        if type(listed) != type([]) or type(added) != type([]):
            return added if type(added) == type([]) else listed
        return listed + [entry for entry in added if entry not in listed]

    # 'configs' maps group name -> list of lines (unlike 'derived-
    # flavours', name -> dict), so a group named by both files unions
    # its two line lists instead of the second replacing the first.
    def _added_configs(self, listed, added):
        merged = {group: list(lines) for group, lines in listed.items()}
        for group, lines in added.items():
            current = merged.setdefault(group, [])
            merged[group] = current + [line for line in lines if line not in current]
        return merged

    # Identifies a package by its 'name', or failing that by parsing its
    # 'source' URI -- kept simple since the URI is parsed properly later,
    # so a wrong guess here just fails to merge, it doesn't merge wrongly.
    def _package_name(self, package):
        if type(package) != type({}):
            return None
        if type(package.get("name")) == type(""):
            return package["name"]
        if type(package.get("source")) != type(""):
            return None
        _, _, rest = package["source"].partition("://")
        rest = rest.split(";")[0].partition("=")[0]
        return os.path.basename(rest).removesuffix(".git").split("_")[0]

    def _lookup_named_part_or_vol(self, parts, label, kind):
        for part in parts:
            if part["label"] == label:
                return part
        return None

    def _update_named_part_or_vol(self, parts, newpart, kind):
        index = 0
        for part in parts:
            if part["label"] == newpart["label"]:
                parts[index] = newpart
            index = index + 1
        return parts

    # direction: additive; a '~flag' removes one a fragment already set
    # (docs/merging.md).
    def _merge_part_flags(self, part, newpart):
        for flag in newpart["flags"]:
            if flag.startswith("~"):
                flag = flag[1:]
                if flag in part["flags"]:
                    part["flags"].remove(flag)
            else:
                if not flag in part["flags"]:
                    part["flags"].append(flag)
        return part

    # direction: asking file wins within 'requires:', peer amends
    # instead; 'flags' additive either way (docs/merging.md).
    def _merge_part_or_vol(self, part, newpart, kind, peer=False):
        for setting in newpart:
            if setting == "flags":
                if "flags" in part:
                    part = self._merge_part_flags(part, newpart)
                else:
                    part["flags"] = []
                    for flag in newpart["flags"]:
                        if not flag.startswith("~"):
                            part["flags"].append(flag)
            elif setting not in part or peer:
                part[setting] = newpart[setting]
        return part

    # direction: asking file wins within 'requires:', peer amends
    # instead, matched by 'label' (docs/merging.md).
    def _merge_parts_or_vols(self, spec, kind, peer=False):
        parts = self.spec["image"][kind]
        for newpart in spec["image"][kind]:
            part = self._lookup_named_part_or_vol(parts, newpart["label"], kind)
            if part is None:
                parts.append(newpart)
            else:
                part = self._merge_part_or_vol(part, newpart, kind, peer=peer)
                parts = self._update_named_part_or_vol(parts, part, kind)
        self.spec["image"][kind] = parts

    # direction: most-specific file wins, key by key; 'sources:' merges by
    # group name, then key by key (docs/merging.md).
    def _merge_ostree(self, incoming):
        if incoming is None:
            self.spec["image"].pop("ostree", None)
            return
        current = self.spec["image"].setdefault("ostree", {})
        if type(incoming) != type({}):
            self.spec["image"]["ostree"] = incoming
            return
        for key, value in incoming.items():
            if value is None:
                current.pop(key, None)
            elif key == "sources" and type(value) == type({}):
                sources = current.setdefault("sources", {})
                for group, settings in value.items():
                    if settings is None:
                        sources.pop(group, None)
                    elif type(settings) == type({}):
                        group_dict = sources.setdefault(group, {})
                        for k, v in settings.items():
                            if v is None:
                                group_dict.pop(k, None)
                            else:
                                group_dict[k] = v
                    else:
                        sources[group] = settings
            elif key == "payload" and type(value) == type({}):
                payload = current.setdefault("payload", {})
                for k, v in value.items():
                    if v is None:
                        payload.pop(k, None)
                    else:
                        payload[k] = v
            else:
                current[key] = value

    # direction: most-specific file wins for a plain setting;
    # 'partitions'/'volumes' route to _merge_parts_or_vols instead (asking
    # file wins within 'requires:', matched by 'label'); 'ostree' merges
    # key by key (docs/merging.md).
    def _merge_image(self, spec, peer=False):
        if type(spec.get("image")) != type({}):
            self.spec["image"] = spec.get("image")
            return
        if "image" not in self.spec or type(self.spec["image"]) != type({}):
            self.spec["image"] = {}
        for setting in spec["image"]:
            if (setting == "partitions" or setting == "volumes") and (setting in self.spec["image"]):
                self._merge_parts_or_vols(spec, setting, peer=peer)
            elif setting == "ostree":
                self._merge_ostree(spec["image"]["ostree"])
            else:
                self.spec["image"][setting] = spec["image"][setting]

    # Same as '_merge_image''s own plain-setting branch: most-specific
    # file wins. 'initrd:' has no 'partitions'/'volumes' equivalent, so
    # nothing routes to _merge_parts_or_vols.
    def _merge_initrd(self, spec):
        if "initrd" in self.spec:
            for setting in spec["initrd"]:
                self.spec["initrd"][setting] = spec["initrd"][setting]
        else:
            self.spec["initrd"] = spec["initrd"]

    # direction: most-specific file wins for 'defaults:' settings, same as
    # 'image:''s own scalars; 'formats:' entries matched by name like
    # 'partitions'/'volumes' -- asking file wins within 'requires:', peer
    # amends instead (docs/merging.md).
    def _merge_vms(self, spec, peer=False):
        incoming = spec["vms"]
        if "vms" not in self.spec:
            self.spec["vms"] = incoming
            return
        current = self.spec["vms"]
        for setting in incoming.get("defaults", {}):
            current.setdefault("defaults", {})[setting] = incoming["defaults"][setting]
        formats = current.setdefault("formats", {})
        for name, settings in incoming.get("formats", {}).items():
            if name not in formats:
                formats[name] = settings
            else:
                existing = formats[name]
                for setting in settings:
                    if setting not in existing or peer:
                        existing[setting] = settings[setting]

    # Gathered from every file, not just the last: the fragment holding a
    # secret is the one that knows it's a secret. An entry is a pattern
    # or a path rule; both merge the same way (docs/merging.md).
    def _merge_redact(self, spec):
        for entry in spec.get("redact") or []:
            entries = self.spec.setdefault("redact", [])
            if entry not in entries:
                entries.append(entry)

    # 'peer' is True for a file with nothing reaching for it -- a
    # top-level CLI file after the first, or a side-loaded fragment --
    # as opposed to one reached via 'requires:' (see _load()).
    def merge(self, spec, peer=False):
        self._merge_redact(spec)
        self._merge_distro(spec)
        self._merge_imager(spec)
        self._merge_multiconfig(spec)
        self._merge_defaults(spec)
        self._merge_overrides(spec)
        self._merge_packages(spec, peer=peer)
        self._merge_vendor(spec, peer=peer)
        self._merge_vendor_exclude(spec)
        self._merge_playbooks(spec, peer=peer)
        self._merge_tests(spec, peer=peer)
        self._merge_containers(spec)
        if "image" in spec:
            self._merge_image(spec, peer=peer)
        if "initrd" in spec:
            self._merge_initrd(spec)
        if "vms" in spec:
            self._merge_vms(spec, peer=peer)
        return self.spec

    def _merge_containers(self, spec):
        if "containers" not in spec:
            return
        if "containers" not in self.spec:
            self.spec["containers"] = spec["containers"]
            return
        from seine.containers import merge_containers
        self.spec["containers"] = merge_containers(
            self.spec.get("containers") or [], spec.get("containers") or [])
