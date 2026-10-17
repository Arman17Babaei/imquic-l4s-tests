from __future__ import annotations

import importlib.util
import json
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools/l4s"))


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RUNNER = load("camera_trace_player", ROOT / "tools/l4s/run_camera_trace_player.py")
SERVER = load("camera_trace_player_server", ROOT / "tools/l4s/camera_trace_player_server.py")


def write_manifest(root: Path) -> Path:
    refinements = []
    counts = [1, 2, 3, 4]
    for index, count in enumerate(counts):
        name = f"tile_000_000_000_refinement_{index}.splat"
        (root / name).write_bytes(bytes(count * 32))
        refinements.append({"index": index, "file": name, "track": f"scene/tile/refinement/{index}",
            "gaussian_count": count, "object_count": 1, "objects": []})
    manifest = {"schema_version": 2,
        "scene": {"id": "scene", "sampling": "full", "gaussian_count": sum(counts)},
        "tiling": {"dimensions": [10, 10, 3], "occupied_tiles": 1},
        "tiles": [{"id": "tile", "gaussian_count": sum(counts), "refinements": refinements}]}
    path = root / "manifest-v2.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


class CameraTracePlayerTest(unittest.TestCase):
    def test_manifest_and_trace_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = RUNNER.validate_manifest(write_manifest(root))
            self.assertEqual(manifest["tiling"]["dimensions"], [10, 10, 3])
            trace_path = root / "trace.json"
            trace_path.write_text(json.dumps([
                {"timestamp_ms": 0, "camera_position": [0, 0, 0], "camera_forward": [0, 0, -1], "camera_up": [0, 1, 0],
                 "yaw_degrees": 0, "fov": 40, "aspect_ratio": 16 / 9},
                {"timestamp_ms": 100, "camera_position": [0, 0, 0], "camera_forward": [1, 0, 0], "camera_up": [0, 1, 0],
                 "yaw_degrees": -90, "fov": 40, "aspect_ratio": 16 / 9},
            ]), encoding="utf-8")
            self.assertEqual(len(RUNNER.validate_trace(trace_path)), 2)
            document = json.loads((root / "manifest-v2.json").read_text())
            document["tiling"]["dimensions"] = [5, 5, 2]
            (root / "manifest-v2.json").write_text(json.dumps(document))
            with self.assertRaisesRegex(ValueError, "10x10x3"):
                RUNNER.validate_manifest(root / "manifest-v2.json")

    def test_browser_and_conversion_commands_obey_flags(self) -> None:
        command = RUNNER.browser_command("http://example/", Path("out.webm"), Path("telemetry.json"),
            timeout_ms=1234, width=960, height=540, headless=True, record=False)
        self.assertIn("--headless", command)
        self.assertIn("--no-record", command)
        self.assertNotIn("--output", command)
        conversions = RUNNER.conversion_commands(Path("in.webm"), Path("out"), "all", 30, 960, 5000)
        self.assertEqual(len(conversions), 2)
        self.assertIn("libx264", conversions[0])
        self.assertTrue(any("fps=15:round=up" in argument for argument in conversions[1]))
        normalization = RUNNER.normalize_webm_command(Path("raw.webm"), Path("final.webm"), 30, 5000, 5.5)
        self.assertIn("libvpx-vp9", normalization)
        self.assertIn("5.000", normalization)
        self.assertTrue(any("setpts=" in argument for argument in normalization))

    def test_no_store_server_exposes_only_explicit_run_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dist = root / "dist"; dist.mkdir()
            (dist / "index.html").write_text("player", encoding="utf-8")
            manifest = write_manifest(root)
            trace = root / "trace.json"; trace.write_text("[]", encoding="utf-8")
            routes = SERVER.route_map(dist, manifest, trace)
            server = ThreadingHTTPServer(("127.0.0.1", 0), SERVER.handler_for(routes))
            thread = threading.Thread(target=server.serve_forever); thread.start()
            try:
                connection = HTTPConnection("127.0.0.1", server.server_port)
                connection.request("GET", "/manifest-v2.json?nonce=1")
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(response.getheader("Cache-Control"), "no-store, max-age=0")
                response.read()
                connection.request("GET", "/../manifest-v2.json")
                self.assertEqual(connection.getresponse().status, 404)
            finally:
                server.shutdown(); thread.join(); server.server_close()


if __name__ == "__main__":
    unittest.main()
