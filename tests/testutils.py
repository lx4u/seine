#!/usr/bin/env python3
# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import fcntl
import glob
import multiprocessing.util
import os
import shutil
import subprocess

# Importing litellm fetches its price list from GitHub in a thread. The
# thread's log lines can outlive the test and block on a full log pipe.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

# Plain rmtree can't touch rootless podman's storage: its files belong
# to a uid-mapped "root", and an overlay mount can still be busy.
# podman unshare owns that uid and can unmount and remove both.
def remove_tree(path):
    shutil.rmtree(path, ignore_errors=True)
    if not os.path.exists(path):
        return
    overlay = os.path.join(path, "build", "containers", "overlay")
    for cmd in (["podman", "unshare", "umount", "-R", overlay],
                ["podman", "unshare", "rm", "-rf", path]):
        try:
            subprocess.run(cmd, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
        except OSError:
            pass

# avocado runs tests in forked children that end with os._exit(), which
# skips atexit. multiprocessing finalizers still run there.
def remove_at_exit(path):
    multiprocessing.util.Finalize(None, remove_tree, (path,), exitpriority=10)

# What a passing test leaves behind should be what avocado itself always
# writes (debug.log, whiteboard, the results files) -- not a build's own
# artifacts, which only earn their keep once there is something to
# explain. self.status is already set by the time avocado calls
# tearDown(), so a test that calls this from its own tearDown is safe:
# it prunes only what it wrote, and only once nothing went wrong.
#
# self._cleanup() is avocado's own Test.tearDown() body: it removes the
# workdir's tmp_dir wrapper outright. A test that overrides tearDown()
# without chaining to super() has to call this itself, or that wrapper
# -- and everything a build left under self.workdir, images and
# container storage alike -- never goes away, pass or fail.
def prune_on_pass(test):
    if test.status != "PASS":
        return
    for path in glob.glob(os.path.join(test.outputdir, "*.log")):
        os.remove(path)
    test._cleanup()

# Loading a spec with a vault() lookup starts the dev vault, which builds
# its container image: minutes, in every test process. Answer kv_read from
# the spec's defaults, then the throwaway ones, as the dev vault does on a
# miss, and leave anything else (signing keys) to a real vault, started
# only if asked. For tests that load specs and do not test the vault itself.
def offline_vault():
    from unittest import mock
    from seine import vault
    from seine.vault.base import VaultNotFound
    from seine.vault.dev import DEV_DEFAULTS

    for_build = vault.for_build
    cache_vault_image()

    class OfflineVault:
        def __init__(self, defaults=None):
            self._defaults = defaults if defaults is not None else {}

        def kv_read(self, ref):
            for known in (self._defaults, DEV_DEFAULTS):
                if ref in known:
                    return known[ref]
            raise VaultNotFound("no throwaway default for '%s'" % ref)

        def __getattr__(self, name):
            return getattr(for_build(self._defaults), name)

    mock.patch.object(vault, "for_build", OfflineVault).start()


# Each test is its own process with its own, empty container store, so
# the dev vault image would be built again by every test that needs it
# (about two minutes). Build it once per sources digest instead, keep it
# in the user's cache and load it from there. Returns the replacement
# for seine.vault.dev.ensure_image(), which is also what a DevVault calls.
def cache_vault_image():
    import subprocess
    from unittest import mock
    from seine.container import ContainerEngine
    from seine.utils import HOST_ARCH
    from seine.vault import dev

    original = dev.ensure_image
    primed = []

    def prime():
        digest = dev._sources_digest()
        cache = os.environ.get("XDG_CACHE_HOME") \
            or os.path.join(os.path.expanduser("~"), ".cache")
        where = os.path.join(cache, "seine-tests")
        os.makedirs(where, exist_ok=True)
        prefix = os.path.join(where, "vault-%s-" % HOST_ARCH)
        archive = "%s%s.tar" % (prefix, digest)
        # One builder at a time, the others wait for the archive.
        with open(prefix + "lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if os.path.isfile(archive) and dev._image_label() != digest:
                try:
                    ContainerEngine.run(["load", "-i", archive], check=True)
                except subprocess.CalledProcessError:
                    os.remove(archive)
            original()
            if not os.path.isfile(archive) and dev._image_label() == digest:
                partial = "%s.%d.partial" % (archive, os.getpid())
                ContainerEngine.run(
                    ["save", "-o", partial, dev.CUSTOM_IMAGE], check=True)
                os.replace(partial, archive)
                for old in glob.glob(prefix + "*.tar"):
                    if old != archive:
                        os.remove(old)

    def ensure_image():
        if not primed:
            prime()
            primed.append(True)
        original()

    mock.patch.object(dev, "ensure_image", ensure_image).start()
    return ensure_image
