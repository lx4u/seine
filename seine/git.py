# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# Shared bare-repo cache for every 'git://' fetch, so a rev already
# pinned by an earlier build needs no network.

import hashlib
import os
import shlex

from seine.container import ContainerEngine

# Mount point for the host-side bare cache, alongside WORKDIR.
GIT_CACHE = "/git-cache"

# One bare repo per remote location; 'location' excludes scheme, so
# https and ssh of the same repo share a cache entry.
def _cache_dir(location):
    key = hashlib.sha256(location.encode()).hexdigest()[:16]
    name = os.path.basename(location).removesuffix(".git") or "repo"
    path = ContainerEngine.cache("git", "%s-%s" % (name, key))
    os.makedirs(path, exist_ok=True)
    return path

# Host volume mount for the cache, paired with GIT_CACHE's mountpoint.
def volume(location):
    return (_cache_dir(location), GIT_CACHE)

# Inits the bare cache if new, fetches 'rev' only if missing, then
# fetches it again, locally, into 'name'. 'rev' lands under
# refs/seine-cache/ in the cache, since 'rev' may be a tag or branch
# name, not just a commit -- and that namespace isn't one git's own
# short-name lookup searches, so every step here names the ref in full
# rather than trying to resolve 'rev' as a bare name a second time.
def clone_args(url, name, rev, branch=None):
    ref = "refs/seine-cache/%s" % rev
    want = shlex.quote(branch if branch else rev)
    cache, url_q, name_q, ref_q = (
        shlex.quote(GIT_CACHE), shlex.quote(url), shlex.quote(name), shlex.quote(ref))
    script = (
        "if [ ! -e %(cache)s/HEAD ]; then git init -q --bare %(cache)s; fi && "
        "git -C %(cache)s rev-parse -q --verify %(ref)s^{commit} >/dev/null 2>&1 || "
        "git -C %(cache)s fetch -q --no-tags %(url)s %(want)s:%(ref)s && "
        "git init -q %(name)s && "
        "git -C %(name)s fetch -q %(cache)s %(ref)s && "
        "git -C %(name)s checkout -q --detach FETCH_HEAD"
        % {"cache": cache, "url": url_q, "want": want, "ref": ref_q, "name": name_q}
    )
    return ["sh", "-c", script]
