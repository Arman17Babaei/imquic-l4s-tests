import unittest

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from tools.l4s.thesis_topology import (
    draw_coexistence_topology,
    draw_dualpi2_validation_topology,
    draw_pair_topology,
    draw_spectrum_topology,
)


class ThesisTopologyTests(unittest.TestCase):
    @staticmethod
    def labels(draw, **kwargs):
        figure, axis = plt.subplots()
        try:
            draw(axis, lambda value: value, **kwargs)
            figure.canvas.draw()
            return "\n".join(item.get_text() for item in axis.texts)
        finally:
            plt.close(figure)

    def test_coexistence_includes_nodes_flows_cc_and_limit(self):
        labels = self.labels(draw_coexistence_topology)
        for expected in ("server", "s1 (OVS)", "client", "bg_src", "bg_sink",
                         "Reno", "Prague", "Cubic", "20 Mbit/s"):
            self.assertIn(expected, labels)

    def test_dualpi2_validation_topology_includes_both_backends_and_tcp_flows(self):
        labels = self.labels(draw_dualpi2_validation_topology)
        for expected in ("h1", "h2", "10.0.0.1", "10.0.0.2", "10.0.0.3",
                         "TCP 5301", "TCP 5302", "Prague", "Reno", "ECT(1)",
                         "ECT(0)", "s1", "s2", "DualPI2", "10 Mbit/s",
                         "Linux", "qdisc", "P4"):
            self.assertIn(expected, labels)

    def test_pair_shows_one_application_flow_and_one_single_background_route(self):
        labels = self.labels(draw_pair_topology)
        for expected in ("server", "client", "bg_src", "bg_sink",
                         "s1 (OVS)", "s2 (OVS)",
                         "Reno", "Prague",
                         "300 Mbit/s", "100 Mbit/s", "RTT = 20 ms"):
            self.assertIn(expected, labels)
        self.assertNotIn("400 Mbit/s", labels)
        self.assertIn("server -> s1 -> s2 -> client", labels)
        self.assertIn("bg_src -> s1 -> s2 -> bg_sink", labels)
        self.assertNotIn("bg_sink2", labels)

    def test_l4s_only_pair_does_not_claim_classic_media(self):
        labels = self.labels(draw_pair_topology, show_classic=False)
        self.assertIn("Prague / ECT(1)", labels)
        self.assertNotIn("Classic", labels)

    def test_spectrum_includes_fraction_controllers_and_rates(self):
        labels = self.labels(draw_spectrum_topology)
        for expected in ("Reno / Not-ECT", "Prague / ECT(1)",
                         "300 Mbit/s", "50 Mbit/s", "280 Mbit/s",
                         "RTT = 20 ms"):
            self.assertIn(expected, labels)


if __name__ == "__main__":
    unittest.main()
