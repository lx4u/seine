# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier Apache-2.0

"""Resolve a feed's ``auth: {login, password}`` pair without ever putting
a literal secret in a specification.

Each field is a pipe-separated chain of ``backend:name`` segments, tried
left-to-right; the first hit wins::

    login:    keyring:corp-login | settings:corp-login | env:CORP_LOGIN
    password: keyring:corp-pass  | env:CORP_PASS

Backends
--------
``env:VAR``
    The process environment. Miss if unset; an empty string is a valid
    value, not a miss.

``keyring:name``
    The desktop/OS keyring via the ``keyring`` package (an optional
    extra). Miss-silent on: the package absent, no backend reachable
    (locked session, headless), or no item under that name -- so the
    chain keeps going in CI without noise.

``settings:name``
    A flat JSON file (``$SEINE_CREDENTIALS_FILE`` or
    ``~/.config/seine/credentials.json``), the same shape and location
    as ``seine/settings.py``'s own file. Miss if the file or the key is
    absent.

``vault:ref``
    Read through a caller-supplied *vault_reader* callable (the build's
    own vault lookup) -- this module never imports ``seine.vault``
    itself. Miss with no reader configured.

Write-back
----------
A value that had to be prompted for is written back to every *writable*
backend (``keyring``, ``settings``) actually named in that field's own
chain, once the credential is proven valid -- call :meth:`CredentialSource.
commit` only then. Naming a backend in the chain is the opt-in; nothing is
ever written to one the chain does not mention.

Retrying
--------
:class:`CredentialSource` owns one feed's ``auth:`` pair. ``get()``
resolves both fields (prompting once, for whichever fields missed, if a
*prompt* callable was given). ``failed()`` re-prompts both fields
together after a rejection (e.g. HTTP 401) -- bypassing the chain, since
it would just return the same wrong value -- up to ``MAX_ATTEMPTS`` total
prompts.
"""

import base64
import json
import os
import ssl
import stat
import sys
import urllib.error
import urllib.request
from urllib.parse import urlsplit

__all__ = [
    "CredentialError", "CredentialNotFound",
    "resolve", "CredentialSource", "probe",
    "remember_resolved", "resolved_for", "clear_resolved",
]

_KNOWN_BACKENDS    = frozenset(["env", "keyring", "settings", "vault"])
_WRITABLE_BACKENDS = frozenset(["keyring", "settings"])

# Namespace all seine credentials share in the OS keyring.
_KEYRING_SERVICE = "seine"

# User-level settings file -- same directory/format convention as
# seine/settings.py, kept separate so a credential is never one accidental
# edit away from being dumped alongside theme/jobs settings.
_SETTINGS_FILE_ENV     = "SEINE_CREDENTIALS_FILE"
_SETTINGS_FILE_DEFAULT = os.path.expanduser("~/.config/seine/credentials.json")

# Resolved values, by feed uri, for the netrc writer (seine/utils.py) --
# the only way a resolved secret reaches those call sites, since it is
# never written back into the spec itself.
_RESOLVED = {}


def remember_resolved(uri, login, password):
    _RESOLVED[uri.rstrip("/")] = (login, password)


def resolved_for(uri):
    return _RESOLVED.get(uri.rstrip("/"))


def clear_resolved():
    _RESOLVED.clear()


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class CredentialError(ValueError):
    """A chain spec is malformed, or a backend failed hard (not a miss)."""


class CredentialNotFound(CredentialError):
    """Every provider in the chain missed; no value is available."""


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _parse(spec):
    """Return ``[(backend, name), ...]`` for a ``|``-separated chain.

    Also used by ``seine.utils`` to validate an ``auth:`` field at spec
    load time without resolving anything.
    """
    if not isinstance(spec, str):
        raise CredentialError("credential chain must be a string, got %s"
                              % type(spec).__name__)
    providers = []
    for i, segment in enumerate(spec.split("|")):
        segment = segment.strip()
        if not segment:
            raise CredentialError(
                "empty segment at position %d in credential spec: %r"
                % (i + 1, spec))
        if ":" not in segment:
            raise CredentialError(
                "segment %r has no 'backend:name' colon in credential spec: %r"
                % (segment, spec))
        backend, _, name = segment.partition(":")
        backend = backend.strip()
        name    = name.strip()
        if not backend or not name:
            raise CredentialError(
                "segment %r in credential spec %r needs both a backend and "
                "a name" % (segment, spec))
        if backend not in _KNOWN_BACKENDS:
            raise CredentialError(
                "unknown backend %r in credential spec: %r (known: %s)"
                % (backend, spec, ", ".join(sorted(_KNOWN_BACKENDS))))
        providers.append((backend, name))
    if not providers:
        raise CredentialError("empty credential spec")
    return providers


# ---------------------------------------------------------------------------
# Backend: env
# ---------------------------------------------------------------------------

def _resolve_env(name):
    value = os.environ.get(name)
    if value is None:
        raise CredentialNotFound("env: %r is not set" % name)
    return value


# ---------------------------------------------------------------------------
# Backend: keyring (the 'keyring' package -- an optional extra)
# ---------------------------------------------------------------------------

def _keyring_reachable():
    """True if a real keyring backend (not the 'fail' stub) is available."""
    try:
        import keyring
        from keyring.backends.fail import Keyring as _FailKeyring
    except ImportError:
        return False
    try:
        return not isinstance(keyring.get_keyring(), _FailKeyring)
    except Exception:
        return False


def _resolve_keyring(name):
    try:
        import keyring
    except ImportError:
        raise CredentialNotFound("keyring: the 'keyring' package is not installed")
    try:
        value = keyring.get_password(_KEYRING_SERVICE, name)
    except Exception as e:
        raise CredentialNotFound("keyring: lookup failed for %r: %s" % (name, e))
    if value is None:
        raise CredentialNotFound("keyring: no item named %r" % name)
    return value


def _save_to_keyring(name, value):
    """Write *value* to the keyring. Best-effort; never raises."""
    try:
        import keyring
        keyring.set_password(_KEYRING_SERVICE, name, value)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Backend: settings (flat JSON file)
# ---------------------------------------------------------------------------

def _settings_file():
    return os.environ.get(_SETTINGS_FILE_ENV) or _SETTINGS_FILE_DEFAULT


def _warn_if_too_open(path, mode):
    if mode & 0o077:
        sys.stderr.write(
            "warning: %s is readable by others (mode %04o) -- "
            "chmod 600 it to keep credentials private\n" % (path, mode))


def _resolve_settings(name):
    path = _settings_file()
    try:
        with open(path) as f:
            text = f.read()
    except FileNotFoundError:
        raise CredentialNotFound("settings: %r not found" % path)
    except OSError as e:
        raise CredentialNotFound("settings: cannot read %r: %s" % (path, e))
    _warn_if_too_open(path, stat.S_IMODE(os.stat(path).st_mode))
    try:
        data = json.loads(text)
    except ValueError as e:
        raise CredentialError("settings: %r is not valid JSON: %s" % (path, e))
    if not isinstance(data, dict):
        raise CredentialError("settings: %r must be a JSON object" % path)
    if name not in data:
        raise CredentialNotFound("settings: no key %r in %r" % (name, path))
    value = data[name]
    if not isinstance(value, str):
        raise CredentialError(
            "settings: key %r in %r must be a string, got %s"
            % (name, path, type(value).__name__))
    return value


def _save_to_settings(name, value):
    """Write *name: value* into the settings file. Best-effort; never raises."""
    path = _settings_file()
    try:
        directory = os.path.dirname(os.path.abspath(path)) or "."
        if not os.path.isdir(directory):
            os.makedirs(directory, mode=0o700)
        try:
            with open(path) as f:
                data = json.load(f)
        except (FileNotFoundError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        data[name] = value
        tmp = path + ".new"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=1, sort_keys=True)
        os.replace(tmp, path)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Backend: vault (read-only, via an injected reader -- no seine.vault import)
# ---------------------------------------------------------------------------

def _resolve_vault(ref, vault_reader):
    if vault_reader is None:
        raise CredentialNotFound(
            "vault: no vault reader configured for this resolution")
    try:
        return vault_reader(ref)
    except CredentialError:
        raise
    except Exception as e:
        # Classified by name, not isinstance -- this module never imports
        # seine.vault, so it cannot know VaultNotFound/VaultError directly.
        if type(e).__name__ == "VaultNotFound":
            raise CredentialNotFound("vault: no item at %r: %s" % (ref, e)) from e
        raise CredentialError("vault: error reading %r: %s" % (ref, e)) from e


# ---------------------------------------------------------------------------
# Write-back
# ---------------------------------------------------------------------------

def _write_back(providers, value):
    """Persist *value* to every writable backend *providers* names."""
    for backend, name in providers:
        if backend == "keyring":
            if _keyring_reachable():
                _save_to_keyring(name, value)
        elif backend == "settings":
            _save_to_settings(name, value)


# ---------------------------------------------------------------------------
# Public: resolve()
# ---------------------------------------------------------------------------

def resolve(spec, vault_reader=None):
    """Resolve one field's chain to a plaintext value.

    :param vault_reader: ``ref -> value``, backing every ``vault:``
        segment; ``None`` makes ``vault:`` segments miss.
    :raises CredentialNotFound: every provider in the chain missed.
    :raises CredentialError: the spec is malformed, or a provider hard-failed.
    """
    providers = _parse(spec)
    misses = []
    for backend, name in providers:
        try:
            if backend == "env":
                return _resolve_env(name)
            if backend == "keyring":
                return _resolve_keyring(name)
            if backend == "settings":
                return _resolve_settings(name)
            if backend == "vault":
                return _resolve_vault(name, vault_reader)
        except CredentialNotFound as e:
            misses.append(str(e))
    raise CredentialNotFound(
        "no provider resolved %r:\n  %s" % (spec, "\n  ".join(misses)))


# ---------------------------------------------------------------------------
# Public: CredentialSource
# ---------------------------------------------------------------------------

class CredentialSource:
    """One feed's ``auth:`` pair -- upfront resolution, retry on rejection.

    ::

        src = CredentialSource(
            {"login": "keyring:x | env:X", "password": "keyring:y | env:Y"},
            context="https://repo.example.com/debian", prompt=interactive,
            vault_reader=build._vault_lookup)
        values = src.get()                # {'login': ..., 'password': ...}
        ...
        values = src.failed()             # rejected (e.g. 401); re-prompts
        ...
        src.commit()                      # proven valid; persist prompted fields

    *prompt* is called as ``prompt(context, fields)`` at most once per
    ``get()``/``failed()`` call, where *fields* maps each field name to
    ``(default, secret)`` -- *default* is whatever that field's chain
    already resolved (empty if it missed), *secret* says whether it
    should be masked. It must return a ``{field: value}`` dict covering
    every field in *fields*.
    """

    MAX_ATTEMPTS = 3

    # Fields shown masked by the prompt; everything else is plain text.
    SECRET_FIELDS = frozenset(["password"])

    def __init__(self, fields, context=None, prompt=None, vault_reader=None):
        self._fields    = dict(fields)
        self._providers = {field: _parse(spec) for field, spec in self._fields.items()}
        self._context     = context
        self._prompt       = prompt
        self._vault_reader = vault_reader
        self._values   = None
        self._prompted = set()   # fields whose current value came from *prompt*
        self._attempts = 0       # prompts issued so far, across get()+failed()

    def get(self):
        """Resolve on first call; return the cached pair on later calls."""
        if self._values is not None:
            return dict(self._values)
        values  = {}
        missing = []
        for field, spec in self._fields.items():
            try:
                values[field] = resolve(spec, vault_reader=self._vault_reader)
            except CredentialNotFound:
                missing.append(field)
        if missing:
            values = self._ask(values)
        self._values = values
        return dict(values)

    def failed(self):
        """The pair was rejected -- re-prompt both fields and return the new pair.

        Bypasses the chain (it would just return the same wrong value).
        Raises :exc:`CredentialNotFound` once :attr:`MAX_ATTEMPTS` prompts
        have been issued, or if no *prompt* was given.
        """
        self._values = self._ask(dict(self._values or {}), retry=True)
        return dict(self._values)

    def commit(self):
        """Persist every prompted field to its own chain's writable backends.

        Idempotent: fields already written are cleared, so a later call
        (once per apt transaction) does nothing.
        """
        for field in self._prompted:
            _write_back(self._providers[field], self._values[field])
        self._prompted.clear()

    def _ask(self, defaults, retry=False):
        if self._attempts >= self.MAX_ATTEMPTS:
            raise CredentialNotFound(
                "gave up on this feed's credentials after %d failed attempts"
                % self.MAX_ATTEMPTS)
        if self._prompt is None:
            missing = sorted(f for f in self._fields if f not in defaults)
            raise CredentialNotFound(
                "no provider resolved %s and no interactive prompt is available"
                % ", ".join(missing or sorted(self._fields)))
        self._attempts += 1
        context = self._retry_context() if retry else self._context
        fields  = {field: (defaults.get(field, ""), field in self.SECRET_FIELDS)
                  for field in self._fields}
        entered = self._prompt(context, fields)
        values  = dict(defaults)
        values.update(entered)
        self._prompted |= set(self._fields)
        return values

    def _retry_context(self):
        tail = "wrong credentials, attempt %d of %d" % (self._attempts, self.MAX_ATTEMPTS)
        return "%s -- %s" % (self._context, tail) if self._context else tail


# ---------------------------------------------------------------------------
# Public: probe()
# ---------------------------------------------------------------------------

# apt has no machine-readable signal for a bad credential, so seine
# checks it itself against the feed's Release file first.
class _NoCrossHostAuthRedirect(urllib.request.HTTPRedirectHandler):
    """Drops 'Authorization' on a redirect to a different host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new_req = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new_req is not None:
            if urlsplit(newurl).hostname != urlsplit(req.full_url).hostname:
                new_req.headers.pop("Authorization", None)
        return new_req


def _basic_auth_header(login, password):
    token = base64.b64encode(("%s:%s" % (login, password)).encode()).decode()
    return "Basic %s" % token


def _status(url, login, password, proxies, cafile, timeout):
    """GET *url* with preemptive Basic auth; return the HTTP status.

    :raises CredentialError: the request never got a response at all
        (DNS, TLS, connection refused, timeout).
    """
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", _basic_auth_header(login, password))
    handlers = [_NoCrossHostAuthRedirect()]
    if proxies is not None:
        handlers.append(urllib.request.ProxyHandler(proxies))
    if cafile is not None:
        handlers.append(urllib.request.HTTPSHandler(
            context=ssl.create_default_context(cafile=cafile)))
    opener = urllib.request.build_opener(*handlers)
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code
    except urllib.error.URLError as e:
        raise CredentialError("could not reach %r: %s" % (url, e.reason)) from e


def probe(url, suite, login, password, proxies=None, cafile=None, timeout=15):
    """Check *login*/*password* against the feed's own server.

    Tries ``<url>/dists/<suite>/InRelease``, then ``.../Release`` for a
    suite-shaped feed, falling back to ``<url>/InRelease`` and
    ``<url>/Release`` for a flat repository -- only on a 404, since that
    is the only status that means "wrong path form", not "wrong
    credential" or "server trouble".

    :param proxies: ``{'http': ..., 'https': ...}``, matching whatever
        ``Acquire::http[s]::Proxy`` apt itself would use; ``None`` falls
        back to the ``http_proxy``/``https_proxy``/``no_proxy``
        environment, the same as apt.
    :param cafile: a custom CA bundle, if the feed needs one.
    :returns: ``True`` on a 200 (the credential is good), ``False`` on a
        401/403 (wrong credential -- re-prompt).
    :raises CredentialError: anything else (DNS, TLS, 5xx, or a 404 at
        every path form) -- a real fault, surfaced as-is, never retried.
    """
    base = url.rstrip("/")
    candidates = [
        "%s/dists/%s/InRelease" % (base, suite),
        "%s/dists/%s/Release" % (base, suite),
        "%s/InRelease" % base,
        "%s/Release" % base,
    ]
    for i, candidate in enumerate(candidates):
        status = _status(candidate, login, password, proxies, cafile, timeout)
        if status == 200:
            return True
        if status in (401, 403):
            return False
        if status == 404:
            if i == len(candidates) - 1:
                raise CredentialError(
                    "%s: no Release file found at dists/%s/ or the "
                    "repository root (404)" % (base, suite))
            continue
        raise CredentialError("%s: unexpected HTTP %d" % (candidate, status))
