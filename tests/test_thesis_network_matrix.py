import importlib.util
from pathlib import Path
import sys
import unittest


MODULE_PATH = Path(__file__).parents[1] / "tools/l4s/thesis_network_matrix.py"
SPEC = importlib.util.spec_from_file_location("thesis_network_matrix", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class ThesisNetworkMatrixTest(unittest.TestCase):
    def test_percentile_interpolates(self):
        self.assertEqual(MODULE.percentile([0.0, 10.0], 50.0), 5.0)
        self.assertAlmostEqual(MODULE.percentile([0.0, 10.0], 95.0), 9.5)

    def test_percentile_rejects_empty_sample(self):
        with self.assertRaises(ValueError):
            MODULE.percentile([], 95.0)


if __name__ == "__main__":
    unittest.main()
