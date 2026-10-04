# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0
"""Server URL and TLS checks shared by the agent and, later, the client."""

import ipaddress
import ssl
from typing import Optional, Union
from urllib.parse import urlsplit

# Rejected tokens (a saved one included) before a client gives up asking.
MAX_TOKEN_REJECTIONS = 3


def is_loopback(host: str) -> bool:
    """Return True for localhost and loopback addresses."""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_server_url(url: str, insecure: bool = False) -> None:
    """Raise ValueError unless url is https, or http to loopback (or insecure)."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"invalid server URL {url!r}: expected http(s)://host[:port]")
    if parts.scheme == "http" and not insecure and not is_loopback(parts.hostname):
        raise ValueError(
            f"refusing plain http to {parts.hostname}: use https, or pass --insecure"
        )


def requests_verify(ca_cert: Optional[str]) -> Union[str, bool]:
    """Value for requests' verify= argument."""
    return ca_cert or True


def resolve_download(server_url: str, token: Optional[str], url: str) -> tuple[str, dict[str, str]]:
    """Return the absolute URL and request headers for one artifact download.

    A relative URL is served by the seine server itself, so it is joined to
    the server's address and authenticated with the user's own token. An
    absolute URL points at storage and carries its own authorisation.
    """
    if url.startswith("/"):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return server_url.rstrip("/") + url, headers
    return url, {}


def ws_ssl_context(url: str, ca_cert: Optional[str]) -> Optional[ssl.SSLContext]:
    """SSL context for a wss:// URL, None for ws://."""
    if not url.startswith("wss://"):
        return None
    return ssl.create_default_context(cafile=ca_cert)
