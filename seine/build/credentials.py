import os
import sys

# own modal in instead.
def _tty_prompt(context, fields):
    import getpass
    if context:
        print(context)
    values = {}
    for name in ("login", "password"):
        default, secret = fields[name]
        label = "%s [%s]" % (name, default) if default else name
        if secret:
            entered = getpass.getpass("%s: " % label)
        else:
            entered = input("%s: " % label)
        values[name] = entered or default
    return values

# None (never prompt, fail closed) unless stdin is a real terminal --
# a CI run or a piped invocation must not block waiting for typing that
# will never come.
def _default_prompt():
    from seine.progress import interactive
    return _tty_prompt if interactive(sys.stdin, os.environ) else None

# Resolves and checks every authenticated feed across all builds before
# any task runs. Feeds sharing the same login/password chain share one
# CredentialSource, so the value is only asked for once.
def collect_credentials(builds, prompt=None):
    from seine import credentials
    from seine.utils import feeds

    if prompt is None:
        prompt = _default_prompt()

    # 'probes' only gets the first feed seen for a given chain -- once
    # that server is checked, a sibling feed with the same credential
    # needs no fresh network round trip.
    sources = {}
    all_feeds = []
    probes = []
    # A remote build's worker is handed its credentials: no chain, no prompt.
    delegated = os.environ.get(credentials.FEED_AUTH_ENV)
    if delegated:
        delegated = credentials.load_feed_auth(delegated)
    else:
        delegated = None
    for build in builds:
        for feed in feeds(build.spec["distribution"]):
            auth = feed["auth"]
            if auth is None:
                continue
            key = feed["uri"] if delegated is not None else (auth["login"], auth["password"])
            source = sources.get(key)
            first_time = source is None
            if first_time and delegated is not None:
                source = credentials.DelegatedSource(delegated.get(feed["uri"].rstrip("/")))
                sources[key] = source
            elif first_time:
                source = credentials.CredentialSource(
                    {"login": auth["login"], "password": auth["password"]},
                    context=feed["uri"], prompt=prompt,
                    vault_reader=build._vault_lookup)
                sources[key] = source
            all_feeds.append((build, feed, source))
            if auth["probe"] and first_time:
                probes.append((build, feed, source))

    for build, feed, source in probes:
        try:
            values = source.get()
        except credentials.CredentialNotFound as e:
            raise credentials.CredentialNotFound(
                "feed '%s' (%s): %s" % (feed["suite"], feed["uri"], e)) from e
        _record_credential_secrets(build.spec, values)
        while True:
            try:
                ok = credentials.probe(feed["uri"], feed["suite"],
                                       values["login"], values["password"])
            except credentials.CredentialError as e:
                raise ValueError("feed '%s' (%s): %s"
                                 % (feed["suite"], feed["uri"], e)) from e
            if ok:
                source.commit()
                break
            values = source.failed()
            _record_credential_secrets(build.spec, values)

    # A 'probe: false' feed still needs its credential resolved for
    # apt's netrc, even though it skipped the network check above.
    for build, feed, source in all_feeds:
        try:
            values = source.get()
        except credentials.CredentialNotFound as e:
            raise credentials.CredentialNotFound(
                "feed '%s' (%s): %s" % (feed["suite"], feed["uri"], e)) from e
        _record_credential_secrets(build.spec, values)
        credentials.remember_resolved(
            feed["uri"], values["login"], values["password"])
    return sources

# Password always redacts; login only if the spec's 'redact:' asks for it.
def _record_credential_secrets(spec, values):
    from seine import vault as _vault
    from seine.utils import redactions
    _vault.record_secret(values["password"])
    _patterns, paths = redactions(spec)
    login_path = ("distribution", "feeds", "auth", "login")
    if any(login_path[:len(p)] == p for p in paths):
        _vault.record_secret(values["login"])
