#!/usr/bin/env python3

import avocado
import os
import sys
import tempfile
from unittest.mock import MagicMock, patch

path_to_self = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.insert(0, path_to_sources)

from seine import utils
from seine.image import Image
from seine.partition import PartitionHandler


class RecipeFileManagement(avocado.Test):
    def test_recipe_file_for_derivation(self):
        self.assertEqual(utils.recipe_file_for("image.img.digest"), "image.img.recipe")
        self.assertEqual(utils.recipe_file_for("/tmp/rootfs.digest"), "/tmp/rootfs.recipe")
        self.assertEqual(utils.recipe_file_for("artifact"), "artifact.recipe")

    def test_write_and_read_recipe_file(self):
        recipe_path = os.path.join(self.workdir, "test.recipe")
        sample_recipe = [
            ("revision", "1"),
            ("spec", "abcdef123456"),
            ("base_image", "sha256:11223344"),
        ]
        utils.write_recipe_file(recipe_path, sample_recipe)
        loaded = utils.read_recipe_file(recipe_path)
        self.assertEqual(loaded, sample_recipe)

    def test_read_nonexistent_recipe_returns_none(self):
        self.assertIsNone(utils.read_recipe_file(os.path.join(self.workdir, "missing.recipe")))

    def test_write_digest_file_with_recipe(self):
        digest_path = os.path.join(self.workdir, "output.digest")
        recipe_path = os.path.join(self.workdir, "output.recipe")
        recipe = [("rev", "1"), ("spec", "cafe")]

        utils.write_digest_file(digest_path, "digest123", recipe=recipe)
        self.assertTrue(os.path.isfile(digest_path))
        self.assertTrue(os.path.isfile(recipe_path))

        with open(digest_path) as f:
            self.assertEqual(f.read().strip(), "digest123")
        self.assertEqual(utils.read_recipe_file(recipe_path), recipe)

        utils.invalidate_digest_file(digest_path)
        self.assertFalse(os.path.exists(digest_path))
        self.assertFalse(os.path.exists(recipe_path))


class ImageRecipeGeneration(avocado.Test):
    def setUp(self):
        self.image = Image(PartitionHandler(), {"verbose": False, "keep": False})
        self.image.spec = {
            "distribution": {"release": "trixie", "architecture": "amd64"},
            "hostname": "test-host",
            "image": {"filename": "disk.img"},
        }
        self.image._from = "debian:trixie"
        self.image._rootfs = os.path.join(self.workdir, "rootfs.tar")
        self.image._output = os.path.join(self.workdir, "disk.img")

    @patch("seine.container.ContainerEngine.imageLabel", return_value=None)
    @patch("seine.container.ContainerEngine.imageId", return_value="base-id-123")
    def test_rootfs_recipe_structure(self, mock_id, mock_label):
        recipe = self.image._rootfs_recipe("vendor-digest-456")
        labels = [label for label, _ in recipe]
        self.assertIn("revision", labels)
        self.assertIn("spec", labels)
        self.assertIn("base_image", labels)
        self.assertIn("packages", labels)
        self.assertIn("vendor", labels)
        self.assertIn("source_date_epoch", labels)

        recipe_dict = dict(recipe)
        self.assertEqual(recipe_dict["base_image"], "base-id-123")
        self.assertEqual(recipe_dict["vendor"], "vendor-digest-456")

        digest = self.image._rootfs_digest("vendor-digest-456")
        self.assertIsInstance(digest, str)
        self.assertEqual(len(digest), 64)

    @patch("seine.container.ContainerEngine.imageLabel", return_value=None)
    @patch("seine.container.ContainerEngine.imageId", return_value="base-id-123")
    def test_image_recipe_structure(self, mock_id, mock_label):
        recipe = self.image._image_recipe("vendor-digest-456")
        labels = [label for label, _ in recipe]
        self.assertEqual(labels, ["revision", "spec", "rootfs"])

        digest = self.image._image_digest("vendor-digest-456")
        self.assertIsInstance(digest, str)
        self.assertEqual(len(digest), 64)

    def test_recipe_file_paths(self):
        self.assertEqual(self.image._recipe_file(), f"{self.image._rootfs}.recipe")
        self.assertEqual(self.image._image_recipe_file(), f"{self.image._output}.recipe")

    def test_layout_file_round_trip(self):
        self.assertEqual(self.image.read_layout(), {})
        ph = self.image.partitionHandler
        ph.partitions = [{"label": "root", "_size": 96 << 20}, {"label": "x"}]
        ph.volumes = [{"label": "var", "_size": 8 << 20}]
        self.image._write_layout()
        self.assertEqual(self.image._layout_file(), f"{self.image._output}.layout")
        self.assertEqual(self.image.read_layout(),
                         {"root": 96 << 20, "var": 8 << 20})
