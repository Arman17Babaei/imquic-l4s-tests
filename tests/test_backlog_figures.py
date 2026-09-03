import unittest

import numpy as np

from tools.l4s.plot_thesis_figures_12_15_fa import (
    BDP_BYTES,
    aggregate_events,
    bound_ms,
    weighted_completion_curve,
)
from tools.l4s.run_backlog_probe import BACKLOG_GRID, LEAD_GRID


class BacklogFigureTests(unittest.TestCase):
    def test_grid_and_normalization(self):
        self.assertEqual(BACKLOG_GRID, (0.0, 0.5, 1.0, 2.0, 4.0))
        self.assertEqual(LEAD_GRID, (0.0, 0.5, 1.0, 2.0, 4.0))
        self.assertAlmostEqual(BDP_BYTES, 250000.0)

    def test_analytical_bound_uses_remaining_serialization_budget(self):
        actual = bound_ms(
            np.array([2.0, 2.0, 1.0]),
            np.array([0.0, 1.0, 2.0]),
        )
        np.testing.assert_allclose(actual, np.array([40.0, 20.0, 0.0]))

    def test_completion_curve_keeps_missing_objects_in_denominator(self):
        important = {1: 100, 2: 300}
        run = {"arrivals": {1: 15.0}, "releases": {1: 5.0, 2: 5.0}}
        grid = np.array([9.0, 10.0, 30.0])
        np.testing.assert_allclose(
            weighted_completion_curve(important, run, grid, relative=True),
            np.array([0.0, 25.0, 25.0]),
        )

    def test_event_aggregation_uses_replication_medians(self):
        rows = [
            {
                "event_key": "frame|0|0|0",
                "rank": 7,
                "release_ms": 100.0,
                "payload_bytes": 57407,
                "repetition": repetition,
                "residual_bdp": residual,
                "age_rtt": age,
            }
            for repetition, residual, age in (
                (1, 1.0, 0.5),
                (2, 3.0, 1.0),
                (3, 2.0, 1.5),
            )
        ]
        event = aggregate_events(rows)[0]
        self.assertEqual(event["repetitions"], 3)
        self.assertEqual(event["residual_bdp_median"], 2.0)
        self.assertEqual(event["age_rtt_median"], 1.0)
        self.assertEqual(event["inferred_initial_bdp"], 3.0)
        self.assertEqual(event["upper_bound_gain_ms"], 40.0)
        self.assertTrue(event["inside_control_grid"])


if __name__ == "__main__":
    unittest.main()
