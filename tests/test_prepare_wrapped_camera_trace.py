import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest


PATH = Path(__file__).parents[1] / "tools/l4s/prepare_wrapped_camera_trace.py"
SPEC = importlib.util.spec_from_file_location("prepare_wrapped_camera_trace", PATH)
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class WrappedTraceTest(unittest.TestCase):
    def test_wrap_rebases_and_scales(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); source = root / "in.json"; output = root / "out.json"
            source.write_text(json.dumps([
                {"timestamp_ms": 0, "fov": 100}, {"timestamp_ms": 10, "fov": 100},
                {"timestamp_ms": 20, "fov": 100}, {"timestamp_ms": 30, "fov": 100},
            ]))
            result = MODULE.build(source, output, start_ms=20, wrap_end_ms=10,
                                  fov_scale=0.5)
            frames = json.loads(output.read_text())
            self.assertEqual(result["wrap_frame_index"], 2)
            self.assertEqual([x["timestamp_ms"] for x in frames], [0, 10, 10, 20])
            self.assertEqual([x["fov"] for x in frames], [50, 50, 50, 50])


if __name__ == "__main__":
    unittest.main()
