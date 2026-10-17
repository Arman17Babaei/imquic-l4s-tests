#!/usr/bin/env python3
"""Serve one Gaussian Player run with explicit no-store semantics."""
from __future__ import annotations

import argparse
import json
import mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit


def route_map(dist: Path, manifest: Path, trace: Path, *, metadata_only: bool = False) -> dict[str, Path]:
    document = json.loads(manifest.read_text(encoding="utf-8"))
    routes = {"/": dist / "index.html", "/index.html": dist / "index.html",
              "/manifest-v2.json": manifest, "/camera-trace.json": trace}
    for asset in dist.rglob("*"):
        if asset.is_file() and asset.suffix not in (".splat", ".ply", ".gsp2"):
            routes[f"/{asset.relative_to(dist).as_posix()}"] = asset
    root = manifest.parent.resolve()
    for tile in document.get("tiles", []):
        for refinement in tile.get("refinements", []):
            name = refinement.get("file")
            if not isinstance(name, str) or not name or name.startswith("/"):
                raise ValueError("manifest refinement files must be non-empty relative paths")
            target = (root / name).resolve()
            if not target.is_relative_to(root):
                raise ValueError(f"manifest refinement file escapes its directory: {name}")
            if not metadata_only:
                routes[f"/{name}"] = target
    return routes


def handler_for(routes: dict[str, Path]):
    class NoStoreHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            path = unquote(urlsplit(self.path).path)
            target = routes.get(path)
            if target is None or not target.is_file():
                self.send_error(404)
                return
            content = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(target.name)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, format: str, *args: object) -> None:
            print(f"http: {format % args}", flush=True)

    return NoStoreHandler


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--trace", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--metadata-only", action="store_true")
    args = parser.parse_args()
    routes = route_map(args.dist.resolve(), args.manifest.resolve(), args.trace.resolve(), metadata_only=args.metadata_only)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler_for(routes))
    print(f"camera trace player http://127.0.0.1:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
