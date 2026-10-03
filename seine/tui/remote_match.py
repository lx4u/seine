# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# What the connected server's project already holds for the active
# specification, keyed by spec digest. Filled by refresh(), read by render.

from seine import analyze

MATCHES = {}

def status(build):
    return MATCHES.get(analyze.spec_digest(build.spec))

def _lookup(session, digest, arch):
    path = "/api/v1/projects/%s/builds/match" % session.active_project
    try:
        resp = session.request("get", path, params={"spec_digest": digest, "target_arch": arch})
        return resp.json() if resp.status_code == 200 else None
    except Exception:
        return None

def _wanted(app):
    try:
        return [(analyze.spec_digest(b.spec), b.spec["distribution"]["architecture"])
                for b in app.context.builds]
    except (TypeError, KeyError):
        return []

# A pushed event: ask again only when it is about an active spec, or when
# the connection was just (re)established and events may have been missed.
def on_event(app, event):
    if event.get("type") == "subscribed" or \
            event.get("spec_digest") in [digest for digest, _ in _wanted(app)]:
        refresh(app)

# Asks for every active group in a thread, then redraws the screen.
def refresh(app):
    session = app.remote_session
    if not (session.connected and session.active_project):
        MATCHES.clear()
        return
    wanted = _wanted(app)
    if not wanted:
        return

    def work():
        found = {digest: _lookup(session, digest, arch) for digest, arch in wanted}
        app.call_from_thread(apply, app, found)

    app.run_worker(work, thread=True, exclusive=True, group="remote-match")

def apply(app, found):
    from seine.tui.base import BaseScreen
    MATCHES.clear()
    MATCHES.update({digest: info for digest, info in found.items() if info})
    if isinstance(app.screen, BaseScreen):
        app.screen.refresh_data()
