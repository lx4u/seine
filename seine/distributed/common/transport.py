# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0
"""Server URL and TLS checks shared by the agent and, later, the client."""

import base64
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


def _origin(url: str) -> tuple[str, str, int]:
    parts = urlsplit(url)
    return parts.scheme, parts.hostname or "", parts.port or (443 if parts.scheme == "https" else 80)


def credential_headers(credential: dict[str, str]) -> dict[str, str]:
    """The Authorization header for an Artifactory token, or a user and password."""
    if credential.get("token"):
        return {"Authorization": f"Bearer {credential['token']}"}
    pair = f"{credential['user']}:{credential['password']}".encode()
    return {"Authorization": "Basic " + base64.b64encode(pair).decode()}


def resolve_download(
    server_url: str, token: Optional[str], url: str,
    storage: Optional[tuple[str, dict[str, str]]] = None,
) -> tuple[str, dict[str, str]]:
    """Return the absolute URL and request headers for one artifact download.

    A relative URL is served by the seine server itself, so it is joined to
    the server's address and authenticated with the user's own seine token.
    An absolute URL points at storage and carries its own authorisation,
    unless it is on the storage endpoint the user holds a credential for,
    ``storage`` being (endpoint, credential): then that credential is sent,
    to that one origin only, and over https or to a loopback host only,
    whatever --insecure says.
    """
    if url.startswith("/"):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return server_url.rstrip("/") + url, headers
    if storage and _origin(url) == _origin(storage[0]):
        host = urlsplit(url).hostname
        if urlsplit(url).scheme != "https" and not (host and is_loopback(host)):
            raise ValueError(f"refusing to send your storage credential over plain http to {host}")
        return url, credential_headers(storage[1])
    return url, {}


def ws_ssl_context(url: str, ca_cert: Optional[str]) -> Optional[ssl.SSLContext]:
    """SSL context for a wss:// URL, None for ws://."""
    if not url.startswith("wss://"):
        return None
    return ssl.create_default_context(cafile=ca_cert)
