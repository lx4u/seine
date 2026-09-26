# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# Settings that hold a text (a copyright, a service unit) may give it inline,
# as a 'file://' path relative to the YAML file, or as 'https://' with the
# sha256 the download must have: 'https://host/copyright;sha256sum=<hex>'.

import hashlib
import os
import re
import shutil
import tempfile

from seine.container import ContainerEngine
from seine.extends import parsing
from seine.utils import WORKDIR

# Settings holding a text, under 'extends: <kind>:'.
NAMES = ["copyright", "systemd-unit"]

FILE = "file://"
URLS = ("http://", "https://")
SHA256 = re.compile(r"^[0-9a-f]{64}$")

def is_file(value):
    return value.startswith(FILE)

def is_url(value):
    return value.startswith(URLS)

def split_url(value):
    url, *parameters = value.split(";")
    return url, dict(p.partition("=")[::2] for p in parameters)

def parse(package, kind, settings, name):
    if name not in settings:
        return None
    path = parsing.setting_path(kind, name)
    value = parsing.parse_string(package, kind, settings, name)
    if is_file(value):
        if not os.path.isfile(value[len(FILE):]):
            raise package._error(f"'{path}' names '{value}', which is not a file")
    elif is_url(value):
        url, parameters = split_url(value)
        digest = parameters.get("sha256sum", "")
        if set(parameters) != {"sha256sum"} or SHA256.match(digest) is None:
            raise package._error(
                f"'{path}' shall give the sha256 of what it downloads: "
                f"'{url};sha256sum=<64 hex digits>'")
    return value

# Makes a 'file://' path in a spec entry relative to the file naming it.
def resolve(value, dirname):
    if type(value) == type("") and is_file(value):
        return FILE + os.path.normpath(os.path.join(dirname, value[len(FILE):]))
    return value

# The files the package's texts come from, for its digest.
def files(package):
    found = []
    for settings in package.ext.values():
        for name in NAMES:
            value = getattr(settings, name.replace("-", "_"), None)
            if value is not None and is_file(value):
                found.append(os.path.normpath(value[len(FILE):]))
    return found

# The text itself. A download is kept by its hash, and checked on this
# machine: a container verifying its own download proves nothing.
def read(builder, value):
    if value is None:
        return None
    if is_file(value):
        with open(value[len(FILE):]) as f:
            return f.read()
    if is_url(value):
        url, parameters = split_url(value)
        with open(_download(builder, url, parameters["sha256sum"])) as f:
            return f.read()
    return value

def _sha256(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()

def _download(builder, url, expected):
    kept = os.path.join(ContainerEngine.cache("texts"), expected)
    if os.path.isfile(kept) and _sha256(kept) == expected:
        return kept
    os.makedirs(os.path.dirname(kept), exist_ok=True)
    directory = tempfile.mkdtemp(dir=os.path.dirname(kept))
    try:
        print(f"fetching '{url}'")
        builder.builderImage.exec(
            ["curl", "-sSfL", "-o", "text", url],
            volumes=[(directory, WORKDIR)], workdir=WORKDIR)
        found = _sha256(os.path.join(directory, "text"))
        if found != expected:
            raise ValueError(
                f"'{url}' is not what 'sha256sum' says it would be\n"
                f"  expected {expected}\n  fetched  {found}\n"
                "Either the file changed where it is served from, or it was "
                "changed on the way here.")
        shutil.move(os.path.join(directory, "text"), kept)
    finally:
        shutil.rmtree(directory, ignore_errors=True)
    return kept
