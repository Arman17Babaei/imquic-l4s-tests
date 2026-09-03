import importlib.util
from pathlib import Path
import sys
import unittest

import numpy as np


PATH = Path(__file__).parents[1] / "tools/l4s/analyze_thesis_quality_pair.py"
SPEC = importlib.util.spec_from_file_location("analyze_thesis_quality_pair", PATH)
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class ThesisQualityPairTest(unittest.TestCase):
    def test_lasting_cumulative_crossover(self):
        time = np.array([-1.0, 0.0, 1.0, 2.0, 3.0, 4.0])
        classic = np.zeros(len(time))
        l4s = np.array([0.0, -1.0, -1.0, 1.0, 2.0, 2.0])
        self.assertEqual(
            MODULE.cumulative_quality_crossover_ms(time, classic, l4s), 3.0
        )

    def test_no_cumulative_crossover(self):
        time = np.array([0.0, 1.0, 2.0])
        classic = np.ones(len(time))
        l4s = np.zeros(len(time))
        self.assertIsNone(
            MODULE.cumulative_quality_crossover_ms(time, classic, l4s)
        )


if __name__ == "__main__":
    unittest.main()
