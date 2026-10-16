# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# Text for the Cache screen and cache inspection commands.

import re

from textual.widgets import Static

from seine import doctor
from seine import packages
from seine.cache import CACHES, CacheCmd
from seine.sbuild import BuilderImage
from seine.tui.base import BaseScreen
from seine.tui.render import _captured


# Not spec-scoped: cache and environment are shared by every build.
class CacheScreen(BaseScreen):
    def update_body(self):
        text = getattr(self.app, "cache_text", None)
        if text:
            self.query_one("#body", Static).update(text)
        else:
            self.query_one("#body", Static).update(render_cache(self.app.context))


def render_s3_status(context=None):
    options = {}
    if context and context.active and context.builds:
        options = getattr(context.builds[0], "options", {}) or {}
        if "spec" not in options and hasattr(context.builds[0], "spec"):
            options = dict(options, spec=context.builds[0].spec)
    check = doctor.check_s3(options)
    if check is None:
        return ""
    status_label = "online (reachable)" if check.status == "ok" else f"offline ({check.detail})"
    lines = [
        "",
        "REMOTE CACHE (S3)",
        f"  Target:     {check.name}",
        f"  Status:     {status_label}",
    ]
    return "\n".join(lines) + "\n"

# Renders local cache storage usage, followed by remote S3 cache status when configured.
def render_cache(context=None, matching=None):
    pattern = re.compile(matching) if matching else None
    text = _captured(lambda: CacheCmd().info(list(CACHES.keys()), entries=True,
                                             matching=pattern))
    s3_text = render_s3_status(context)
    if s3_text:
        text += s3_text
    return text

def render_cache_explain(targets, options=None):
    return _captured(lambda: CacheCmd().explain(targets, options=options or {}))

# A package can live inside a 'multiconfig:' sub-group's own
# 'packages:', not just the top-level image's -- walk 'subbuilds' the
# same way Image.tasks() does to build the real task graph.
def _find_package(image, name):
    for package in image.packages:
        if package.name == name:
            return image, package
    for sub in image.subbuilds.values():
        found = _find_package(sub.image, name)
        if found is not None:
            return found
    return None

# Why a 'packages:' entry did not come from the cache. Reuses the
# Builder a real build already made when there is one: only that
# instance still has miss_reason() for a miss it already explained.
def render_cache_why(context, name, architecture=None):
    if not context.active:
        return "no active specification -- '/use SPEC...' picks one\n"
    for build in context.builds:
        found = _find_package(build.image, name)
        if found is None:
            continue
        image, package = found
        distro = image.spec["distribution"]
        builder = image._builder
        if builder is None:
            builder = packages.Builder(distro, image.options,
                                       BuilderImage(distro, image.options))
        current = {(p.name, a) for p, a, _ in builder.current(image.packages)}
        archs = [architecture] if architecture else builder.architectures(package)
        lines = []
        for a in archs:
            if (package.name, a) in current:
                lines.append("%s/%s: cached" % (package.name, a))
                continue
            reason = builder.miss_reason(package, a)
            if reason is None:
                lines.append("%s/%s: not cached" % (package.name, a))
            else:
                lines.append("%s/%s: not cached --" % (package.name, a))
                lines += ["  %s" % r for r in reason]
        return "\n".join(lines) + "\n"
    return "'%s' is not in this specification's 'packages:' section\n" % name
