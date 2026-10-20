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
import subprocess


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
    def test_fixed_horizon_accepts_only_consistent_censored_delivery(self) -> None:
        partial = {"receivedGaussians": 40, "appliedGaussians": 40}
        self.assertFalse(RUNNER.delivery_is_valid(partial, 100))
        self.assertTrue(RUNNER.delivery_is_valid(partial, 100, fixed_horizon=True))
        self.assertTrue(RUNNER.delivery_is_valid({**partial, "appliedGaussians": 39}, 100, fixed_horizon=True))
        self.assertFalse(RUNNER.delivery_is_valid({**partial, "appliedGaussians": 41}, 100, fixed_horizon=True))
        self.assertFalse(RUNNER.delivery_is_valid({"receivedGaussians": 101, "appliedGaussians": 101}, 100, fixed_horizon=True))

    def test_base_layer_buffering_is_an_explicit_recording_option(self) -> None:
        args = RUNNER.parse_args(["--manifest", "unused", "--trace", "unused",
            "--output", "results/unused", "--start-after-base"])
        self.assertTrue(args.start_after_base)

    def test_eventual_delivery_does_not_imply_complete_video(self) -> None:
        delivery = RUNNER.recording_delivery({"appliedGaussians": 100,
            "trace": {"gaussiansAtStart": 25, "gaussiansAtVideoEnd": 40}}, 100)
        self.assertEqual(delivery["gaussians_at_video_end"], 40)
        self.assertEqual(delivery["scene_fraction_at_video_end"], .4)
        self.assertFalse(delivery["complete_scene_at_video_end"])
        self.assertIsNone(RUNNER.recording_delivery({}, 100)["complete_scene_at_video_end"])

    def test_qemu_and_http_can_record_and_measure_quality(self) -> None:
        for transport, network in (("moqt", "qemu"), ("http", "loopback")):
            with self.subTest(transport=transport):
                args = RUNNER.parse_args(["--manifest", "unused", "--trace", "unused",
                    "--output", "results/unused", "--transport", transport,
                    "--network-path", network, "--quality", "--quality-fps", "2"])
                self.assertFalse(args.no_record)
                self.assertTrue(args.quality)

    def test_browser_timeout_must_be_positive(self) -> None:
        for timeout in (0, -1):
            with self.subTest(timeout=timeout), self.assertRaisesRegex(ValueError, "positive"):
                RUNNER.browser_command("http://example/", Path("out.webm"), Path("telemetry.json"),
                    timeout_ms=timeout, width=640, height=360, headless=True, record=False)

    def test_software_mode_and_requested_timeout_reach_browser(self) -> None:
        command = RUNNER.browser_command("http://example/", Path("out.webm"), Path("telemetry.json"),
            timeout_ms=1234, width=640, height=360, headless=True, record=False, gpu_mode="software")
        self.assertEqual(command[command.index("--timeout-ms") + 1], "1234")
        self.assertEqual(command[command.index("--gpu-mode") + 1], "software")

    def test_launcher_rejects_invalid_budgets_before_opening_inputs(self) -> None:
        for flag in ("--timeout-seconds", "--qemu-memory", "--qemu-cpus"):
            with self.subTest(flag=flag):
                result = subprocess.run([sys.executable, str(ROOT / "tools/l4s/run_camera_trace_player.py"),
                    "--manifest", "unused", "--trace", "unused", "--output", "results/unused", flag, "0"],
                    capture_output=True, text=True)
                self.assertEqual(result.returncode, 2)
                self.assertIn("must be positive", result.stderr)

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
            document["tiling"]["dimensions"] = [5, 0, 2]
            (root / "manifest-v2.json").write_text(json.dumps(document))
            with self.assertRaisesRegex(ValueError, "positive dimensions"):
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
