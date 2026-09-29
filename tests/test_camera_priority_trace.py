import importlib.util
import json
import math
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "tools/l4s/generate_camera_priority_trace.py"
SPEC = importlib.util.spec_from_file_location("generate_camera_priority_trace", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class CameraPriorityTraceTest(unittest.TestCase):
    def test_generates_exact_approved_trace_from_first_bicycle_frame(self) -> None:
        source = json.loads((Path(__file__).resolve().parents[1] /
            "deps/3dgs_over_moq/assets/user102_bicycle_500.json").read_text())

        frames = MODULE.generate_trace(source)

        self.assertEqual(len(frames), 501)
        self.assertEqual([frames[index]["timestamp_ms"] for index in (0, 150, 250, 500)],
                         [0, 15000, 25000, 50000])
        self.assertEqual([frames[index]["yaw_degrees"] for index in (0, 150, 250, 500)],
                         [0.0, -180.0, -90.0, -360.0])
        self.assertEqual(frames[0]["camera_position"], [
            -3.1984000000000004, -0.06699586588800002, 0.576640814336])
        self.assertTrue(all(frame["fov"] == 40.0 for frame in frames))
        first = frames[0]["camera_forward"]
        last = frames[-1]["camera_forward"]
        self.assertAlmostEqual(first[1], 0.0)
        self.assertAlmostEqual(math.sqrt(sum(value * value for value in first)), 1.0)
        self.assertGreater(sum(a * b for a, b in zip(first, last)), 1.0 - 1e-12)

    def test_rejects_a_vertical_first_heading(self) -> None:
        source = [{
            "timestamp_ms": 0,
            "camera_position": [0.0, 0.0, 0.0],
            "view_matrix": [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0],
                            [0.0, 0.0, 1.0, 0.0], [0.0, 0.0, 0.0, 1.0]],
            "fov": 60.0,
        }]

        with self.assertRaisesRegex(ValueError, "horizontal heading"):
            MODULE.generate_trace(source)


if __name__ == "__main__":
    unittest.main()
