import inspect
import unittest

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from tools.l4s import plot_thesis_figures_12_15_fa as figures_12_15
from tools.l4s import plot_thesis_figures_fa as figures_06_10
from tools.l4s.validate_thesis_figure_artifacts import PDF_NAMES


class ThesisPlotLayoutTests(unittest.TestCase):
    def test_figure_07_comparison_is_bars_with_external_legend(self):
        figure, axis = plt.subplots()
        try:
            data = {
                "x": np.arange(2), "width": .32,
                "utilization": [90.0, 95.0], "fairness": [80.0, 85.0],
                "utilization_low": [89.0, 94.0], "utilization_high": [91.0, 96.0],
                "fairness_low": [79.0, 84.0], "fairness_high": [81.0, 86.0],
            }
            figures_06_10.plot_07_utilization_fairness(axis, data)
            self.assertEqual(len(axis.patches), 4)
            legend = axis.get_legend()
            self.assertIsNotNone(legend)
            self.assertGreaterEqual(legend.get_bbox_to_anchor().y0, 1.0)
        finally:
            plt.close(figure)

    def test_figure_07_throughput_identifies_each_coloured_stream_and_x_axis(self):
        figure, axis = plt.subplots()
        try:
            data = {
                "x": np.arange(2), "width": .32,
                "foreground": [9.0, 11.0], "cubic": [10.0, 8.0],
            }
            figures_06_10.plot_07_throughput(axis, data)
            labels = [text.get_text() for text in axis.get_legend().get_texts()]
            self.assertIn("Classic MoQ: Reno / Not-ECT", labels)
            self.assertIn("L4S MoQ: Prague / ECT(1)", labels)
            self.assertIn("Background: TCP Cubic / Not-ECT", labels)
            self.assertTrue(axis.get_xlabel())
        finally:
            plt.close(figure)

    def test_completion_cdf_uses_the_larger_delivered_count_as_common_scale(self):
        classic_x, classic_y = figures_06_10.completion_cdf(np.array([1.0, 2.0]), 3)
        l4s_x, l4s_y = figures_06_10.completion_cdf(np.array([1.0, 2.0, 3.0]), 3)
        np.testing.assert_array_equal(classic_x, [1.0, 2.0])
        self.assertAlmostEqual(classic_y[-1], 2 / 3)
        self.assertAlmostEqual(l4s_y[-1], 1.0)
        with self.assertRaises(ValueError):
            figures_06_10.completion_cdf(np.array([1.0, 2.0]), 1)

    def test_required_split_outputs_are_validated(self):
        for name in (
            "figure-07-a-throughput-fa.pdf",
            "figure-07-b-utilization-fairness-fa.pdf",
            "figure-07-c-queue-delay-fa.pdf",
            "figure-08-a-throughput-fa.pdf",
            "figure-08-b-queue-delay-fa.pdf",
        ):
            self.assertIn(name, PDF_NAMES)
        source = inspect.getsource(figures_12_15.figure12)
        self.assertIn("figure-12-a-release-relative-completion-fa", source)
        self.assertIn("figure-12-b-wall-clock-completion-fa", source)
        self.assertIn("درصد وزنی بایت‌های مهم تکمیل‌شده",
                      inspect.getsource(figures_12_15.plot_12_completion))
        source = inspect.getsource(figures_12_15.figure15)
        self.assertIn("figure-15-a-applicability-fa", source)
        self.assertIn("figure-15-b-residual-backlog-cdf-fa", source)

    def test_caption_writing_requires_explicit_flag(self):
        self.assertIn("--write-captions", inspect.getsource(figures_06_10.main))
        self.assertIn("--write-captions", inspect.getsource(figures_12_15.main))

    def test_secondary_and_reference_styles_are_dotted(self):
        source = inspect.getsource(figures_06_10)
        self.assertIn("ls=\":\"", source)
        self.assertIn("مهلت 45 ثانیه", source)
        source = inspect.getsource(figures_12_15)
        self.assertIn("linestyle=\":\"", source)


if __name__ == "__main__":
    unittest.main()
