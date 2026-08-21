from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1] / "tools" / "l4s"
sys.path.insert(0, str(TOOLS))

from plot_3dgs_rtt_throughput import payload_goodput, smoothed_rtt


class Plot3dgsRttThroughputTests(unittest.TestCase):
    def test_payload_goodput_uses_fixed_receive_bins(self):
        with tempfile.TemporaryDirectory() as directory:
            timeline = Path(directory) / "arrival.csv"
            timeline.write_text(
                "arrival_time_us,payload_bytes\n"
                "100000,1000000\n"
                "900000,500000\n"
                "1100000,250000\n",
                encoding="utf-8",
            )
            self.assertEqual(payload_goodput(timeline), [12.0, 2.0])

    def test_smoothed_rtt_converts_units_without_subtracting_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            metrics = Path(directory) / "metrics.csv"
            metrics.write_text(
                "time_us,rtt_us\n0,20000\n500000,25000\n",
                encoding="utf-8",
            )
            self.assertEqual(smoothed_rtt(metrics), [(0.0, 20.0), (0.5, 25.0)])


if __name__ == "__main__":
    unittest.main()
