import importlib.util
from pathlib import Path
import sys
import unittest


PATH = Path(__file__).parents[1] / "tools/l4s/thesis_3dgs_pair.py"
SPEC = importlib.util.spec_from_file_location("thesis_3dgs_pair", PATH)
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class Thesis3DGSPairTest(unittest.TestCase):
    def test_percentile(self):
        self.assertEqual(MODULE.percentile([0.0, 10.0], 50), 5.0)
        self.assertAlmostEqual(MODULE.percentile([0.0, 10.0], 99), 9.9)

    def test_empty_percentile_fails(self):
        with self.assertRaises(ValueError):
            MODULE.percentile([], 95)


if __name__ == "__main__":
    unittest.main()
