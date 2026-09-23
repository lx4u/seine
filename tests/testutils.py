#!/usr/bin/env python3
# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import glob
import os
import shutil
import subprocess

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
