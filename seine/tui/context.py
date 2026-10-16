# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# The TUI's "active specification": what /use sets, and what
# Overview/Plan act on. '/use a.yaml -- b.yaml' groups the same way
# 'seine build a.yaml -- b.yaml' does.

import os

from seine import gists, multiconfig, stdlib
from seine.build import BuildCmd

class Context:
    def __init__(self):
        self.groups = None
        self.builds = None
        # Set only by side_load()/side_unload()/overlay(): the spec just before
        # the most recent change, for SpecTree.load() to diff against.
        # Cleared by use(), overwritten (not accumulated) each call.
        self.changed_from = None
        self.base_groups = None
        self.overlay_fragment = None

    # Loads/parses each group like 'seine build' does; nothing is
    # built, so this is instant and side-effect-free.
    def use(self, args, reset_base=True):
        groups = multiconfig.split([stdlib.resolve(gists.resolve(arg)) for arg in args])
        builds = []
        for files in groups:
            build = BuildCmd()
            build.options = dict(build.options, ansible_library=[])
            # Without this, a TUI build logs to a bare mkdtemp() and
            # catalogs under a digest of no files at all.
            build.options["files"] = files
            build.load_all(files)
            build.parse()
            builds.append(build)
        self.groups = groups
        self.builds = builds
        self.changed_from = None
        if reset_base:
            self.base_groups = [list(g) for g in groups]
            self.overlay_fragment = None

    # Refuses more than one active group, same as /build/ /filesystem.
    def _one_active_group(self):
        if not self.active:
            raise ValueError("no active specification -- '/use SPEC' first")
        if len(self.builds) != 1:
            raise ValueError(
                "overlay and side-load need exactly one active group -- multi-group "
                "specifications ('/use a -- b') aren't supported here yet")

    # 'group's declared file list, amended by side_load()/side_unload()
    # when targeting a named sub-build instead of the outer spec.
    def _subbuild_files(self, build, group):
        groups = build.spec.get("multiconfig") or {}
        if group not in groups:
            raise ValueError(
                "'%s' is not one of the declared 'multiconfig:' groups (%s)"
                % (group, ", ".join(sorted(groups)) if groups else "none"))
        return groups[group]

    # Re-parses 'group' alone from 'files', in place; the outer spec and
    # every other sub-build are untouched. 'build.subbuilds' and
    # 'build.image.subbuilds' are the same dict, so mutating one suffices.
    def _reload_subbuild(self, build, group, files):
        if not files:
            raise ValueError(
                "'%s' has no files left to load -- refusing to leave it "
                "empty" % group)
        build.spec["multiconfig"][group] = files
        previous_spec = build.subbuilds[group].spec
        build.subbuilds[group] = multiconfig.load_group(
            files, build.options,
            apt_defaults=(build.spec.get("defaults") or {}).get("apt"))
        self.changed_from = previous_spec

    # One more fragment appended to the active group's file list, same
    # '--' composition as the CLI -- or, with 'group' given, to that
    # sub-build's file list instead, reparsing only that sub-build.
    def side_load(self, fragment, group=None):
        fragment = stdlib.resolve(gists.resolve(fragment))
        self._one_active_group()
        build = self.builds[0]
        if group is None:
            previous_spec = build.spec
            self.use(self.groups[0] + [fragment], reset_base=False)
            self.changed_from = previous_spec
            return
        files = self._subbuild_files(build, group)
        self._reload_subbuild(build, group, files + [fragment])

    # The reverse of side_load(): drops one fragment out of the file
    # list and reparses the rest. Works on any file in the list, not
    # only one side_load() itself added.
    def side_unload(self, fragment, group=None):
        fragment = stdlib.resolve(gists.resolve(fragment))
        self._one_active_group()
        build = self.builds[0]
        if group is None:
            files = self.groups[0]
            if fragment not in files:
                raise ValueError("'%s' isn't currently loaded" % fragment)
            previous_spec = build.spec
            remaining = [f for f in files if f != fragment]
            self.use(remaining, reset_base=False)
            self.changed_from = previous_spec
            return
        files = self._subbuild_files(build, group)
        if fragment not in files:
            raise ValueError(
                "'%s' isn't currently loaded in '%s'" % (fragment, group))
        self._reload_subbuild(build, group, [f for f in files if f != fragment])

    def _resolve_overlay_fragment(self, fragment, base_spec_file=None):
        if fragment.startswith("@"):
            fragment = fragment[1:]
        resolved = stdlib.resolve(gists.resolve(fragment))
        if os.path.exists(resolved):
            return os.path.abspath(resolved)
        if base_spec_file:
            base_dir = os.path.dirname(os.path.abspath(base_spec_file))
            for cand in [os.path.join(base_dir, fragment),
                         os.path.join(base_dir, f"{fragment}.yaml"),
                         os.path.join(base_dir, f"{fragment}.yml")]:
                if os.path.exists(cand):
                    return os.path.abspath(cand)
        return os.path.abspath(resolved) if not os.path.isabs(resolved) else resolved

    # Load an overlay fragment on top of the base specification, or clear
    # with 'none'. Replaces any previously active overlay fragment.
    def overlay(self, fragment):
        self._one_active_group()
        if not self.base_groups:
            self.base_groups = [list(g) for g in self.groups]
        base_files = self.base_groups[0]
        if not base_files:
            raise ValueError("no base specification files loaded")
        build = self.builds[0]
        previous_spec = build.spec

        if fragment.lower() == "none":
            self.use(base_files, reset_base=False)
            self.overlay_fragment = None
            self.changed_from = previous_spec
            return

        resolved = self._resolve_overlay_fragment(fragment, base_files[0])
        self.use(base_files + [resolved], reset_base=False)
        self.overlay_fragment = resolved
        self.changed_from = previous_spec

    @property
    def active(self):
        return self.builds is not None and len(self.builds) > 0

    # multiconfig._label() is the output filename without directory or
    # extension. Several groups show as 'a+b'.
    def label(self):
        if not self.active:
            return None
        return "+".join(multiconfig._label(build) for build in self.builds)


class AppContext(Context):
    def __init__(self, app):
        super().__init__()
        self._app = app
        self._builds_val = None

    @property
    def builds(self):
        if self._builds_val is None and not getattr(self._app, "is_running", False) and not getattr(self._app, "_loading_spec", False):
            if getattr(self._app, "_pending_files", None):
                self._app.load_pending_spec()
        return self._builds_val

    @builds.setter
    def builds(self, value):
        self._builds_val = value

