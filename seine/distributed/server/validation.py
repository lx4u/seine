# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0
"""Checks on what workers and clients send to the server."""

import re
from typing import Any

from seine.distributed.common.models import BUILD_OPTION_KEYS

_SHA256 = re.compile(r"[0-9a-f]{64}")
_SECRET_MARKERS = ("token", "secret", "password", "key", "credential")


def is_plain_name(name: Any) -> bool:
    """Return True for a non-empty basename of at most 255 characters."""
    return (
        isinstance(name, str)
        and 0 < len(name) <= 255
        and name not in (".", "..")
        and not any(c in name for c in "/\\\0")
    )


def check_artifacts(artifacts: list[dict[str, Any]], prefix: str) -> list[dict[str, Any]]:
    """Return the manifest as stored, raising ValueError on the first bad entry."""
    clean = []
    for entry in artifacts:
        name = entry.get("name")
        if not is_plain_name(name):
            raise ValueError(f"invalid artifact name {name!r}")
        if entry.get("key") != f"{prefix}{name}":
            raise ValueError(f"artifact {name!r}: key must be '{prefix}{name}'")
        sha256, size = entry.get("sha256"), entry.get("size")
        if not isinstance(sha256, str) or not _SHA256.fullmatch(sha256):
            raise ValueError(f"artifact {name!r}: sha256 must be 64 lowercase hex digits")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ValueError(f"artifact {name!r}: size must be an integer >= 0")
        clean.append({"name": name, "key": entry["key"], "sha256": sha256, "size": size})
    return clean


def check_build_options(options: dict[str, Any]) -> None:
    """Raise ValueError unless every option is one the build reads."""
    secret = sorted(k for k in options if any(m in k.lower() for m in _SECRET_MARKERS))
    if secret:
        raise ValueError(f"options must not carry secrets: {', '.join(secret)}")
    unknown = sorted(set(options) - BUILD_OPTION_KEYS)
    if unknown:
        raise ValueError(f"unknown build options: {', '.join(unknown)}")


MAX_SECRET_FEEDS = 64
MAX_SECRET_LENGTH = 4096


def check_transient_secrets(secrets: dict[str, Any]) -> None:
    """Raise ValueError unless secrets is {'feeds': {id: {'login', 'password'}}} within limits."""
    if not secrets:
        return
    if set(secrets) != {"feeds"}:
        raise ValueError("transient_secrets takes only 'feeds'")
    feeds = secrets["feeds"]
    if not isinstance(feeds, dict) or not 0 < len(feeds) <= MAX_SECRET_FEEDS:
        raise ValueError(f"transient_secrets.feeds takes 1 to {MAX_SECRET_FEEDS} feeds")

    def text(value: Any) -> bool:
        return isinstance(value, str) and 0 < len(value) <= MAX_SECRET_LENGTH

    for feed_id, pair in feeds.items():
        if not text(feed_id):
            raise ValueError("transient_secrets.feeds: feed ids must be non-empty strings")
        if not isinstance(pair, dict) or set(pair) != {"login", "password"} \
                or not all(text(v) for v in pair.values()):
            raise ValueError(
                f"transient_secrets.feeds: each feed takes a login and a password "
                f"of 1 to {MAX_SECRET_LENGTH} characters"
            )
