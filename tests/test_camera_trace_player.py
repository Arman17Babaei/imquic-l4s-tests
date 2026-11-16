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
    def test_failure_before_time_zero_has_no_measurement_window(self):
        self.assertEqual(RUNNER.measurement_window({'startedUnixMs':None,'nominalDurationMs':19000}),(None,None))
        self.assertEqual(RUNNER.measurement_window({'startedUnixMs':1000,'nominalDurationMs':19000}),(1000,20000))

    def test_requested_quality_failure_blocks_overall_success_for_every_transport(self):
        self.assertFalse(RUNNER.quality_allows_success(True,{'status':'INCOMPLETE'}))
        self.assertFalse(RUNNER.quality_allows_success(True,None))
        self.assertTrue(RUNNER.quality_allows_success(True,{'status':'PASS','snapshot_coverage':{'complete':False,'accepted':True}}))
        self.assertTrue(RUNNER.quality_allows_success(False,None))
    def test_worker_buffer_options_and_control_measurement_window(self):
        base = ['--manifest', 'unused', '--trace', 'unused', '--output', 'results/unused', '--transport', 'ideal']
        args = RUNNER.parse_args(base + ['--receipt-thread', 'worker', '--playback-delay-ms', '4000'])
        self.assertEqual(args.receipt_thread, 'worker')
        self.assertEqual(args.playback_delay_ms, 4000)
        self.assertEqual(RUNNER.measurement_window({'startedUnixMs': 5000, 'controlStartedUnixMs': 1000,
            'nominalDurationMs': 6000, 'playbackDelayMs': 4000}), (1000, 7000))
        for extra in (['--playback-delay-ms', '4000'],
                      ['--receipt-thread', 'worker', '--playback-delay-ms', '4000', '--start-after-first-object'],
                      ['--receipt-thread', 'worker', '--playback-delay-ms', '-1']):
            with self.assertRaises(SystemExit):
                RUNNER.parse_args(base + extra)

    def test_buffered_capture_uses_one_png_per_trace_grid_frame(self):
        base = ['--manifest', 'unused', '--trace', 'unused', '--output', 'results/unused',
                '--transport', 'ideal', '--receipt-thread', 'worker', '--playback-delay-ms', '4000']
        args = RUNNER.parse_args(base)
        self.assertEqual(args.quality_fps, 10)
        self.assertEqual(args.quality_camera_timestamps, 'fixed')
        self.assertEqual(args.fps, 10)
        for extra in (['--quality-fps', '5'], ['--fps', '30']):
            with self.assertRaises(SystemExit):
                RUNNER.parse_args(base + extra)

    def test_first_object_start_and_snapshot_tolerance_options(self):
        args=RUNNER.parse_args(['--manifest','unused','--trace','unused','--output','results/unused',
            '--transport','ideal','--start-after-first-object','--max-missing-snapshot-fraction','0.05'])
        self.assertTrue(args.start_after_first_object)
        self.assertEqual(args.max_missing_snapshot_fraction,.05)
        with self.assertRaises(SystemExit):
            RUNNER.parse_args(['--manifest','unused','--trace','unused','--output','results/unused',
                '--start-after-first-object','--start-after-base'])
    def test_default_scene_path_is_qemu_prague_and_ideal_stays_local(self):
        base=['--manifest','unused','--trace','unused','--output','results/unused']
        args=RUNNER.parse_args(base)
        self.assertEqual((args.transport,args.network_path,args.transport_mode),('moqt','qemu','prague'))
        self.assertEqual(RUNNER.parse_args(base+['--transport','ideal']).network_path,'loopback')
        self.assertEqual(RUNNER.parse_args(base+['--transport','http']).network_path,'loopback')

    def test_qemu_quality_accepts_fixed_camera_grid(self):
        args = RUNNER.parse_args(['--manifest', 'unused', '--trace', 'unused',
            '--output', 'results/unused', '--transport', 'moqt', '--network-path', 'qemu',
            '--quality', '--quality-camera-timestamps', 'fixed'])
        self.assertEqual(args.quality_camera_timestamps, 'fixed')

    def test_completed_qemu_result_uses_success_log_not_historical_name_matches(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            current = root / 'camera-player-train-reno-current'
            current.mkdir()
            (root / 'camera-player-train-reno-old').mkdir()
            log = root / 'qemu.log'
            log.write_text(f'QEMU setup cache: HIT example\nQEMU player: PASS ({current})\n')
            self.assertEqual(RUNNER.completed_qemu_result(log), current)

    def test_completed_qemu_result_rejects_ambiguous_success_logs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            log = root / 'qemu.log'
            log.write_text(f'QEMU player: PASS ({root})\nQEMU player: PASS ({root})\n')
            with self.assertRaises(ValueError):
                RUNNER.completed_qemu_result(log)

    def test_fixed_horizon_accepts_only_consistent_censored_delivery(self) -> None:
        partial = {"receivedGaussians": 40, "appliedGaussians": 40}
        self.assertFalse(RUNNER.delivery_is_valid(partial, 100))
        self.assertTrue(RUNNER.delivery_is_valid(partial, 100, fixed_horizon=True))
        self.assertTrue(RUNNER.delivery_is_valid({**partial, "appliedGaussians": 39}, 100, fixed_horizon=True))
        self.assertFalse(RUNNER.delivery_is_valid({**partial, "appliedGaussians": 41}, 100, fixed_horizon=True))
        self.assertFalse(RUNNER.delivery_is_valid({"receivedGaussians": 101, "appliedGaussians": 101}, 100, fixed_horizon=True))

    def test_ideal_transport_has_explicit_link_settings_and_clean_empty_defaults(self) -> None:
        args = RUNNER.parse_args(["--manifest", "unused", "--trace", "unused", "--output", "results/unused",
            "--transport", "ideal", "--ideal-bandwidth-mbps", "20", "--ideal-data-delay-ms", "80", "--quality"])
        self.assertEqual(args.ideal_bandwidth_mbps, 20)
        self.assertEqual(args.ideal_data_delay_ms, 80)
        self.assertEqual(args.quality_fps, 30)
        self.assertFalse(args.start_after_base)

    def test_ideal_transport_rejects_invalid_or_networked_link_configuration(self) -> None:
        base = ["--manifest", "unused", "--trace", "unused", "--output", "results/unused", "--transport", "ideal"]
        for extra in (["--ideal-bandwidth-mbps", "0"], ["--ideal-data-delay-ms", "-1"],
                      ["--network-path", "qemu"], ["--start-after-base"], ["--video-hud"]):
            with self.subTest(extra=extra), self.assertRaises(SystemExit):
                RUNNER.parse_args(base + extra)

    def test_default_playback_starts_without_base_buffering(self) -> None:
        args = RUNNER.parse_args(["--manifest", "unused", "--trace", "unused",
            "--output", "results/unused"])
        self.assertFalse(args.start_after_base)

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
