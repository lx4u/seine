# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import os
from .base import StorageProvider
from seine import utils


class LocalStorageProvider(StorageProvider):
    """Local storage provider for standalone builds without remote cache."""

    def pull(self, wanted=None):
        return None

    def push(self, kind, key, path, spec=None, recipe=None):
        return True

    def touch(self, kind, key):
        pass

    def explain(self, kind, key):
        if os.path.isfile(f"{key}.recipe"):
            return utils.read_recipe_file(f"{key}.recipe")
        if os.path.isfile(key):
            return utils.read_recipe_file(key)
        if kind == "packages":
            from seine.cache import CACHES
            from seine.packages import STAMPS_SPEC
            parts = key.split("/")
            if len(parts) == 3:
                release, arch, pkg = parts
                stamps_spec = os.path.join(CACHES["packages"](), release, STAMPS_SPEC)
                if os.path.isdir(stamps_spec):
                    matches = [
                        os.path.join(stamps_spec, f)
                        for f in os.listdir(stamps_spec)
                        if f.startswith(f"{pkg}_{arch}_") and f.endswith(".recipe")
                    ]
                    if matches:
                        return utils.read_recipe_file(max(matches, key=os.path.getmtime))
        return None

    def ensure_bucket(self):
        pass
