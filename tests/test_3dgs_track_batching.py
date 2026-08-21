from __future__ import annotations

import sys
import unittest
from pathlib import Path

import torch

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools" / "l4s"
sys.path.insert(0, str(TOOLS))

from render_3dgs_reordering import activate_render_dependency

activate_render_dependency(ROOT / "deps" / "3dgs_over_moq")

from streaming.transport.client.pipeline import RenderPipeline
from streaming.transport.client.rasterizer import composite_depth_layers


class TrackBatchingTests(unittest.TestCase):
    def test_batches_keep_tracks_whole_and_retain_every_key(self):
        candidates = [
            {"key": ("a", "0", 0, 0), "n": 40},
            {"key": ("b", "0", 0, 0), "n": 30},
            {"key": ("a", "1", 0, 0), "n": 20},
            {"key": ("c", "0", 0, 0), "n": 50},
        ]
        selected = {candidate["key"] for candidate in candidates}

        batches = RenderPipeline._track_batches(candidates, selected, 80)

        self.assertEqual(set().union(*batches), selected)
        self.assertEqual(sum(len(batch) for batch in batches), len(selected))
        key_to_batch = {
            key: index for index, batch in enumerate(batches) for key in batch
        }
        self.assertEqual(key_to_batch[("a", "0", 0, 0)], key_to_batch[("a", "1", 0, 0)])

    def test_depth_compositor_places_near_layer_in_front(self):
        red = torch.tensor([[[0.5, 0.0, 0.0]]])
        blue = torch.tensor([[[0.0, 0.0, 1.0]]])
        half = torch.tensor([[[0.5]]])
        opaque = torch.tensor([[[1.0]]])
        near = torch.tensor([[[1.0]]])
        far = torch.tensor([[[2.0]]])

        image = composite_depth_layers([
            (blue, opaque, far),
            (red, half, near),
        ])

        self.assertTrue(torch.equal(image, torch.tensor([[[127, 0, 127]]], dtype=torch.uint8)))


if __name__ == "__main__":
    unittest.main()
