import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "prepare_figure_cleanup", ROOT / "tools/l4s/prepare_figure_cleanup.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class CleanupSafetyTests(unittest.TestCase):
    def test_all_candidates_are_workspace_relative(self):
        for candidate in MODULE.INITIAL_DELETION_CANDIDATES:
            path = MODULE.contained(ROOT / candidate)
            self.assertTrue(path.is_relative_to(ROOT))
            self.assertNotEqual(path, ROOT)

    def test_completed_tables_are_protected_not_candidates(self):
        candidates = set(MODULE.INITIAL_DELETION_CANDIDATES)
        protected = {family.pattern for family in MODULE.PROTECTED_FAMILIES}
        self.assertNotIn(
            "results/l4s/qemu-3dgs-l4s-classic-step5-table-20260821T111820Z",
            candidates,
        )
        self.assertIn(
            "results/l4s/qemu-3dgs-l4s-classic-step5-table-20260821T111820Z",
            protected,
        )


if __name__ == "__main__":
    unittest.main()
