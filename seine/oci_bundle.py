# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier Apache-2.0

import os
import shutil

BUNDLE_DIR = "/usr/share/seine/oci"

_attempted = False

# 'vault' ships seine-vault, not a debian release; every other
# directory name is a debian:<release> bundle from build-oci.py.
def _marker_tag(release):
    if release == "vault":
        from seine.vault.dev import CUSTOM_IMAGE
        return CUSTOM_IMAGE
    return "debian:%s" % release

def import_bundled():
    global _attempted
    if _attempted:
        return
    _attempted = True
    if os.path.isdir(BUNDLE_DIR) == False:
        return
    from seine.container import ContainerEngine
    for release in sorted(os.listdir(BUNDLE_DIR)):
        release_dir = os.path.join(BUNDLE_DIR, release)
        images = os.path.join(release_dir, "images.tar.gz")
        marker = _marker_tag(release)
        # Skip reload if already loaded (marker is the bundle's first tag).
        if os.path.isfile(images) and not ContainerEngine.hasImage(marker):
            ContainerEngine.run(["load", "-i", images])
        chroots = os.path.join(release_dir, "chroots")
        if os.path.isdir(chroots):
            shutil.copytree(chroots, ContainerEngine.cache("chroots"),
                            dirs_exist_ok=True)
