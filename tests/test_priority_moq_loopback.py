import importlib.util
import unittest
from pathlib import Path


RUNNER_PATH = Path(__file__).resolve().parents[1] / "tools/l4s/run_priority_moq_loopback.py"
SPEC = importlib.util.spec_from_file_location("priority_moq_loopback", RUNNER_PATH)
assert SPEC and SPEC.loader
RUNNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNNER)


class BrowserProbeCommandTest(unittest.TestCase):
    def test_passes_the_requested_timeout_in_milliseconds(self) -> None:
        command = RUNNER.browser_probe_command(
            "http://127.0.0.1:5173/",
            Path("results/priority-moq-mvp/player-telemetry.json"),
            5,
            300,
        )

        self.assertEqual(command[-1], "300000")

    def test_rejects_non_positive_timeout(self) -> None:
        with self.assertRaises(ValueError):
            RUNNER.browser_probe_command("http://127.0.0.1:5173/", Path("telemetry.json"), 5, 0)


if __name__ == "__main__":
    unittest.main()
