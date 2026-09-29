import unittest

from tools.l4s.analyze_camera_priority import (
    median_band, validate_decisions, validate_history, validate_object_coverage,
)


class CameraPriorityAnalyzerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = {"tiles": [{"id": "tile", "refinements": [{"index": 0,
            "objects": [{"id": 0, "bytes": 32, "sha256": "abc",
                         "publisher_priority": 0}]}]}]}
        self.delivery = {"tile_id": "tile", "refinement": "0", "object_id": "0",
            "request_id": "7", "sha256": "abc", "actual_sha256": "abc",
            "payload_bytes": "32", "publisher_priority": "0", "status": "delivered",
            "subscriber_priority": "8", "subscriber_epoch": "1", "monotonic_us": "200"}

    def test_object_coverage_and_terminal_history_join(self) -> None:
        validate_object_coverage(self.manifest, [self.delivery])
        validate_history([self.delivery], [{"tile_id": "tile", "refinement": "0",
            "object_id": "0", "kind": "initial", "priority": "8", "epoch": "0",
            "monotonic_us": "100", "status": "pending"}, {"tile_id": "tile",
            "refinement": "0", "object_id": "0", "kind": "terminal",
            "priority": "8", "epoch": "1", "monotonic_us": "200",
            "status": "delivered"}])

    def test_duplicate_or_hash_mismatch_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "duplicate"):
            validate_object_coverage(self.manifest, [self.delivery, self.delivery])
        wrong = {**self.delivery, "actual_sha256": "bad"}
        with self.assertRaisesRegex(ValueError, "hash"):
            validate_object_coverage(self.manifest, [wrong])

    def test_three_repetition_band_uses_median_and_extrema(self) -> None:
        self.assertEqual(median_band([9, 1, 5]), {"median": 5, "min": 1, "max": 9})

    def test_decisions_cover_empty_lods_even_though_fetches_do_not(self) -> None:
        manifest = {"tiles": [{"id": "tile", "refinements": [
            {"index": 0, "object_count": 1}, {"index": 1, "object_count": 0}]}]}
        rows = [{"epoch": "0", "tile_id": "tile", "refinement": "0", "priority": "8",
                 "viewport": "center", "depth": "middle"},
                {"epoch": "0", "tile_id": "tile", "refinement": "1", "priority": "48",
                 "viewport": "center", "depth": "middle"}]
        validate_decisions(manifest, rows, 1)


if __name__ == "__main__":
    unittest.main()
