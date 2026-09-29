from pathlib import Path
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]


class RelayTransportModeTests(unittest.TestCase):
    def test_relay_exposes_explicit_transport_mode(self) -> None:
        relay = ROOT / "deps/imquic/examples/imquic-moq-relay"
        result = subprocess.run([relay, "--help"], check=True, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.assertIn("--congestion-control", result.stdout)
        self.assertIn("--transport-metrics", result.stdout)


if __name__ == "__main__":
    unittest.main()
