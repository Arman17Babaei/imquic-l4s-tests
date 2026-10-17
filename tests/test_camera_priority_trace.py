import importlib.util
import math
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "tools/l4s/generate_camera_priority_trace.py"
SPEC = importlib.util.spec_from_file_location("generate_camera_priority_trace", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class CameraPriorityTraceTest(unittest.TestCase):
    def test_generates_yaw_trace_from_source_camera_basis(self) -> None:
        source = [{"camera_position": [1.25, -2, 3], "fov": 60,
            "view_matrix": [[1, 0, 0, 0], [0, 1, 0, 0],
                            [0, 0, 1, 0], [0, 0, 0, 1]]}]

        frames = MODULE.generate_trace(source)

        self.assertEqual(len(frames), 501)
        self.assertEqual([frames[index]["timestamp_ms"] for index in (0, 150, 250, 500)],
                         [0, 15000, 25000, 50000])
        self.assertEqual([frames[index]["yaw_degrees"] for index in (0, 150, 250, 500)],
                         [0.0, -180.0, -90.0, -360.0])
        self.assertEqual(frames[0]["camera_position"], [1.25, -2.0, 3.0])
        self.assertTrue(all(frame["fov"] == source[0]["fov"] for frame in frames))
        first = frames[0]["camera_forward"]
        last = frames[-1]["camera_forward"]
        up = frames[0]["camera_up"]
        self.assertAlmostEqual(math.sqrt(sum(value * value for value in first)), 1.0)
        self.assertAlmostEqual(math.sqrt(sum(value * value for value in up)), 1.0)
        self.assertAlmostEqual(sum(a * b for a, b in zip(first, up)), 0.0)
        self.assertTrue(all(frame["camera_up"] == up for frame in frames))
        self.assertGreater(sum(a * b for a, b in zip(first, last)), 1.0 - 1e-12)

    def test_rejects_a_zero_camera_basis(self) -> None:
        source = [{
            "timestamp_ms": 0,
            "camera_position": [0.0, 0.0, 0.0],
            "view_matrix": [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0],
                            [0.0, 0.0, 0.0, 0.0], [0.0, 0.0, 0.0, 1.0]],
            "fov": 60.0,
        }]

        with self.assertRaisesRegex(ValueError, "zero vector"):
            MODULE.generate_trace(source)


if __name__ == "__main__":
    unittest.main()
