from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1] / "tools" / "l4s"
sys.path.insert(0, str(TOOLS))

from object_timeline import (
    prefix_object_count,
    render_arrival_comparison,
    summarize_timeline,
    timeline_series,
    transport_metric_series,
)


HEADER = (
    "arrival_time_us,bundle_record_index,payload_bytes,cumulative_bytes,"
    "num_gaussians,cumulative_gaussians,subgroup_id,object_id\n"
)


class ObjectTimelineTests(unittest.TestCase):
    def write(self, directory: str, rows: str) -> Path:
        path = Path(directory) / "arrival-timeline.csv"
        path.write_text(HEADER + rows, encoding="utf-8")
        return path

    def test_summary_validates_monotonic_object_timeline(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(
                directory,
                "1200,0,100,100,1024,1024,0,0\n"
                "2500,1,80,180,512,1536,1,0\n",
            )
            summary = summarize_timeline(path)
        self.assertEqual(summary["objects"], 2)
        self.assertEqual(summary["payload_bytes"], 180)
        self.assertEqual(summary["gaussians"], 1536)
        self.assertEqual(summary["first_arrival_us"], 1200)
        self.assertEqual(summary["last_arrival_us"], 2500)
        self.assertEqual(len(summary["sha256"]), 64)

    def test_non_monotonic_arrival_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(
                directory,
                "2500,0,100,100,1024,1024,0,0\n"
                "1200,1,80,180,512,1536,1,0\n",
            )
            with self.assertRaisesRegex(ValueError, "not monotonic"):
                summarize_timeline(path)

    def test_cumulative_counts_are_validated(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(
                directory,
                "1200,0,100,99,1024,1024,0,0\n",
            )
            with self.assertRaisesRegex(ValueError, "cumulative byte count mismatch"):
                summarize_timeline(path)

    def test_record_indices_must_match_bundle_order(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(
                directory,
                "1200,1,100,100,1024,1024,0,0\n",
            )
            with self.assertRaisesRegex(ValueError, "record index"):
                summarize_timeline(path)

    def test_prefix_count_reconstructs_presentation_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(
                directory,
                "1000,0,100,100,1024,1024,0,0\n"
                "2000,1,100,200,1024,2048,0,1\n"
                "3500,2,100,300,1024,3072,1,0\n",
            )
            self.assertEqual(prefix_object_count(path, 999), 0)
            self.assertEqual(prefix_object_count(path, 1000), 1)
            self.assertEqual(prefix_object_count(path, 2000), 2)
            self.assertEqual(prefix_object_count(path, 9999), 3)

    def test_series_bins_splats_and_renders_comparison(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(
                directory,
                "100000,0,100,100,1000,1000,0,0\n"
                "1200000,1,100,200,2000,3000,1,0\n",
            )
            series = timeline_series(path)
            self.assertEqual(series["bins"], [1000, 2000])
            self.assertEqual(series["byte_bins"], [100, 100])
            self.assertEqual(series["total_splats"], 3000)
            self.assertEqual(series["total_payload_bytes"], 200)
            metrics = Path(directory) / "transport-metrics.csv"
            metrics.write_text(
                "time_us,rtt_us,cwnd_bytes\n"
                "0,1000,12000\n"
                "1000000,2500,24000\n",
                encoding="utf-8",
            )
            metric_series = transport_metric_series(metrics)
            self.assertEqual(metric_series["minimum_rtt_us"], 1000)
            self.assertEqual(metric_series["queue_delay_ms"], [(0, 0.0), (1000000, 1.5)])
            output = Path(directory) / "comparison.svg"
            render_arrival_comparison(
                {
                    "50% Reno": path,
                    "50% Prague": path,
                    "100% Reno": path,
                    "100% Prague": path,
                },
                output,
                duration_us=2_000_000,
                transport_metrics={
                    "50% Reno": metrics,
                    "50% Prague": metrics,
                    "100% Reno": metrics,
                    "100% Prague": metrics,
                },
            )
            rendered = output.read_text(encoding="utf-8")
            self.assertIn("Cumulative received splats", rendered)
            self.assertIn("Splat arrival rate", rendered)
            self.assertIn("Foreground payload throughput", rendered)
            self.assertIn("Estimated queue delay", rendered)
            self.assertIn("smoothed RTT − run minimum RTT", rendered)
            self.assertIn('y="85">100% Prague</text>', rendered)
            self.assertIn('width="1200" height="983"', rendered)
            self.assertNotIn("nan", rendered.lower())


if __name__ == "__main__":
    unittest.main()
