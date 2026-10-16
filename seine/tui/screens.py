# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# Secondary inspection screens and renderers: plan, packages, analyze, doctor, diff.

import contextlib
import io
import json
import os
import re
import time

from textual.widgets import Static

from seine import analyze
from seine import packages
from seine import sbom
from seine.diffing import diff, recall
from seine.progress import elapsed
from seine.sbuild import BuilderImage
from seine.tui.artifacts import _human_size
from seine.tui.base import BaseScreen


# 'Image.plan()' prints straight to stdout, like every other 'seine'
# command; captured rather than reimplemented.
def _captured(fn):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        fn()
    return out.getvalue()


def render_plan(context):
    if not context.active:
        return "no active specification -- '/use SPEC...' picks one\n"
    sections = []
    for files, build in zip(context.groups, context.builds):
        text = _captured(build.image.plan)
        baseline = recall(files)
        if baseline is not None:
            changes = diff(baseline, build.dump(build.spec), color=False)
            text = "changed since last build:\n%s\n%s" % (changes, text)
        sections.append(text)
    return "\n\n".join(sections)


# Two data sources, labelled apart: what 'packages:' asked to rebuild
# from source, and what the last '--sbom' run found installed.
def render_packages(context):
    if not context.active:
        return "no active specification -- '/use SPEC...' picks one\n"
    sections = []
    for build in context.builds:
        distro = build.spec["distribution"]
        source_packages = build.image.packages
        lines = ["REBUILT FROM SOURCE (packages: section)", ""]
        if len(source_packages) == 0:
            lines.append("  none")
        else:
            builder = packages.Builder(distro, build.options,
                                       BuilderImage(distro, build.options))
            # 'Builder.current()' already only names a stamp on disk.
            current = {(package.name, architecture)
                      for package, architecture, _ in builder.current(source_packages)}
            for package in source_packages:
                for architecture in builder.architectures(package):
                    mark = "✔" if (package.name, architecture) in current else "○"
                    label = builder.label(package, architecture)
                    source = package.spec.get("source") or "(description only)"
                    lines.append("  %s %-24s %s" % (mark, label, source))
                    origin = package.origins.get("source") or package.origins.get("name")
                    if origin:
                        lines.append("      declared by: %s" % origin)

        lines.append("")
        lines.append("INSTALLED (from the last SBOM)")
        output = build.image._output
        sbom_path = sbom.output_path(output) if output else None
        if sbom_path is None or not os.path.isfile(sbom_path):
            lines.append(
                "  no SBOM for this build: run with --sbom to populate this section")
        else:
            try:
                with open(sbom_path) as f:
                    spdx = json.load(f)
                installed = sorted(spdx.get("packages", []),
                                  key=lambda p: p.get("name", ""))
                lines.append("  %d packages -- %s" % (len(installed), sbom_path))
                for entry in installed:
                    lines.append("  %-32s %s" % (entry.get("name", "?"),
                                                  entry.get("versionInfo", "")))
            except (OSError, ValueError) as e:
                lines.append("  could not read %s: %s" % (sbom_path, e))
        sections.append("\n".join(lines))
    return "\n\n".join(sections) + "\n"


# 'analyze.blame()'/'analyze.critical_chain()' print, captured for the
# newest recorded run. 'ROOTFS SIZE' below is the one section that reads
# more than the newest run, since a single number can't show growth.
def render_analyze(context):
    if not context.active:
        return "no active specification -- '/use SPEC...' picks one\n"
    sections = []
    for build in context.builds:
        digest = analyze.spec_digest(build.spec)
        history = analyze.runs(digest)
        if len(history) == 0:
            sections.append("no recorded run for this specification yet -- "
                            "'/build' it once first")
            continue
        latest = history[0]
        text = _captured(lambda run=latest: analyze.blame(run))
        text += "\n" + _captured(lambda run=latest: analyze.critical_chain(run))
        # Newest first, same order analyze.runs() returns. Older runs
        # made before this field existed just have nothing to add here.
        sized = [run for run in history if run.get("rootfs_size") is not None]
        if len(sized) > 0:
            lines = ["", "ROOTFS SIZE"]
            for i, run in enumerate(sized):
                ago = max(0, time.time() - run["started"])
                line = "  %s ago   %s" % (elapsed(ago), _human_size(run["rootfs_size"]))
                if i + 1 < len(sized):
                    delta = run["rootfs_size"] - sized[i + 1]["rootfs_size"]
                    if delta != 0:
                        line += "  (%s%s)" % ("+" if delta > 0 else "-",
                                              _human_size(abs(delta)))
                lines.append(line)
            text += "\n".join(lines) + "\n"
        sections.append(text)
    return "\n\n".join(sections)


def render_doctor(pull=False, options=None):
    from rich.text import Text

    from seine import doctor
    raw = doctor.render(doctor.run(options=options, pull=pull))
    text = Text(raw)
    for m in re.finditer(r"  ! ", raw):
        text.stylize("dark_orange", m.start() + 2, m.start() + 3)
    return text


class PlanScreen(BaseScreen):
    def update_body(self):
        self.query_one("#body", Static).update(render_plan(self.app.context))


class PackagesScreen(BaseScreen):
    def update_body(self):
        self.query_one("#body", Static).update(render_packages(self.app.context))


class AnalyzeScreen(BaseScreen):
    def update_body(self):
        self.query_one("#body", Static).update(render_analyze(self.app.context))


class DoctorScreen(BaseScreen):
    def update_body(self):
        options = {}
        if self.app.context and self.app.context.active and self.app.context.builds:
            options = getattr(self.app.context.builds[0], "options", {}) or {}
            if "spec" not in options and hasattr(self.app.context.builds[0], "spec"):
                options = dict(options, spec=self.app.context.builds[0].spec)
        self.query_one("#body", Static).update(render_doctor(options=options))


# What /diff last computed -- not spec-scoped, so reads app.diff_text.
class DiffScreen(BaseScreen):
    def update_body(self):
        text = self.app.diff_text or (
            "no diff yet -- '/diff OLD.spdx.json NEW.spdx.json'\n")
        self.query_one("#body", Static).update(text)
