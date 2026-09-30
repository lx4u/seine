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
        recipe_path = f"{key}.recipe"
        if os.path.isfile(recipe_path):
            return utils.read_recipe_file(recipe_path)
        return None

    def ensure_bucket(self):
        pass
