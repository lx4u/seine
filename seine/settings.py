# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# User settings for the TUI (seine/tui/settings.py) and CLI job
# defaults (seine/build/cli.py). Flat JSON file under XDG config dir.

import json
import os

# None means "use built-in default", e.g. llm_model=None disables AI
# chat, history_pruning=None keeps the 30-day default (see
# seine.tui.history.parse_prune_after()).
DEFAULTS = {"jobs": None, "resources": None, "theme": None,
           "startup_commands": [], "llm_model": None, "llm_api_base": None,
           "sbom2cve_program": None, "history_pruning": None,
           "default_remote": None, "auto_connect_remote": False,
           "remote_insecure": False, "remote_ca_cert": None}

# The two checks below back both /set and the /settings editor, so the
# same text is accepted (or refused) either way.
def parse_bool(text):
    value = text.strip().lower()
    if value in ("true", "1", "yes", "on"):
        return True
    if value in ("false", "0", "no", "off"):
        return False
    raise ValueError("expects 'true' or 'false', not '%s'" % text)

def check_ca_cert(path):
    expanded = os.path.expanduser(path)
    if not os.path.isfile(expanded):
        raise ValueError("expects a certificate file, '%s' is not one" % path)
    return expanded

def default_path():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "seine", "settings.json")

# Missing/unreadable/invalid file just means no settings, not an error.
def load(path=None):
    try:
        with open(path or default_path()) as f:
            recorded = json.load(f)
    except (OSError, ValueError):
        recorded = {}
    if not isinstance(recorded, dict):
        recorded = {}
    merged = dict(DEFAULTS)
    merged.update(recorded)
    return merged

# Write to a temp file then rename, so readers never see a half-written file.
def save(settings, path=None):
    path = path or default_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = "%s.new" % path
    with open(temporary, "w") as f:
        json.dump(settings, f, indent=1, sort_keys=True)
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
