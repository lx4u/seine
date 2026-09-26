# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# What the 'extends:' kinds that write Debian packaging have in common:
# loading their templates, rendering them, and laying out debian/.

import functools
import os
import shutil

from datetime import datetime
from datetime import timezone
from email.utils import format_datetime

import jinja2

from seine.utils import GIT_EMAIL
from seine.utils import GIT_NAME

DATA = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")

FILES = ("changelog", "control", "rules")

# Same '[[ ]]' delimiters as spec rendering. Avoid bash's '[[ ]]' test in
# rules recipes because of it; '[' works the same.
TEMPLATE = jinja2.Environment(
    variable_start_string="[[", variable_end_string="]]",
    block_start_string="[%", block_end_string="%]",
    comment_start_string="[#", comment_end_string="#]",
    trim_blocks=True, lstrip_blocks=True, keep_trailing_newline=True,
    undefined=jinja2.StrictUndefined)

# The templates in seine/data/<directory>, and their bytes joined: what
# a package is built from, for its digest.
@functools.lru_cache(maxsize=None)
def load_templates(directory, files=FILES):
    templates = {}
    content = b""
    for name in files:
        with open(os.path.join(DATA, directory, name), "rb") as f:
            raw = f.read()
        content += raw
        templates[name] = raw.decode()
    return templates, content

def write(path, content, mode=None):
    with open(path, "w") as f:
        f.write(content)
    if mode is not None:
        os.chmod(path, mode)

# An empty debian/ for a native package: whatever the tree came with is
# replaced by the packaging seine writes.
def reset_debian(sourcedir):
    debian = os.path.join(sourcedir, "debian")
    if os.path.isdir(debian):
        shutil.rmtree(debian)
    os.makedirs(os.path.join(debian, "source"))
    write(os.path.join(debian, "source", "format"), "3.0 (native)\n")
    return debian

def base_context(package, epoch):
    return {
        "name": package.name,
        "version": package.upstream_version,
        "maintainer": GIT_NAME,
        "email": GIT_EMAIL,
        "date": format_datetime(datetime.fromtimestamp(epoch, timezone.utc)),
    }

# Writes each template into debian/, as 'names' renames it (if at all).
def render_files(debian, templates, context, names=None):
    for name, template in templates.items():
        target = (names or {}).get(name, name)
        write(os.path.join(debian, target),
              TEMPLATE.from_string(template).render(context),
              mode=0o755 if name == "rules" else None)

def sh_quote(value):
    return "'" + value.replace("'", "'\\''") + "'"
