import unittest

from tools.l4s.run_camera_priority_guest import case_order, shaping_commands


class CameraPriorityQemuTests(unittest.TestCase):
    def test_counterbalanced_case_order(self) -> None:
        self.assertEqual(case_order(), [
            ("prague", 1), ("reno", 1), ("reno", 2),
            ("prague", 2), ("prague", 3), ("reno", 3),
        ])

    def test_every_egress_gets_exact_delay_rate_and_dualpi2_tuple(self) -> None:
        commands = shaping_commands("client-eth0")
        rendered = [" ".join(command) for command in commands]
        self.assertTrue(any("rate 20mbit burst 32k cburst 32k" in row for row in rendered))
        self.assertTrue(any("netem delay 20ms" in row for row in rendered))
        self.assertTrue(any("dualpi2 target 15ms tupdate 16ms step_thresh 1ms" in row
                            for row in rendered))


if __name__ == "__main__":
    unittest.main()
