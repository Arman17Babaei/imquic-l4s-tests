import unittest

from tools.l4s.run_camera_priority_guest import case_order, shaping_commands, player_shaping_commands


class CameraPriorityQemuTests(unittest.TestCase):
    def test_player_separates_delay_from_dropping_aqm(self) -> None:
        host = player_shaping_commands("server-eth0", "10mbit", propagation=True)
        switch = player_shaping_commands("s1-eth1", "10mbit", propagation=False)
        self.assertEqual(len(host), 1)
        self.assertIn("40ms", host[0])
        self.assertFalse(any("dualpi2" in command for command in host))
        self.assertFalse(any("netem" in command for command in switch))
        self.assertIn("root", switch[0])
        self.assertIn("htb", switch[0])
        self.assertIn("10mbit", switch[1])
        self.assertIn("dualpi2", switch[2])
        self.assertEqual(switch[2][switch[2].index("step_thresh") + 1], "5ms")

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

    def test_interactive_ten_mbps_rate_reaches_htb_class(self) -> None:
        rendered = [" ".join(command) for command in shaping_commands("client-eth0", "10mbit")]
        self.assertTrue(any("rate 10mbit burst 32k cburst 32k" in row for row in rendered))
        self.assertFalse(any("rate 20mbit" in row for row in rendered))


if __name__ == "__main__":
    unittest.main()
