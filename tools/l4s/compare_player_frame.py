#!/usr/bin/env python3
"""Render source frame 0 with the repository gsplat path and gate browser RGB SSIM."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from PIL import Image

from prepare_player_sh3 import sha256_file


def compare(reference: Path, browser: Path, output: Path):
    from render_3dgs_reordering import image_ssim
    from three_dgs_bundle import image_psnr
    ref = np.asarray(Image.open(reference).convert('RGB'))
    got = np.asarray(Image.open(browser).convert('RGB'))
    if ref.shape != got.shape: raise ValueError('reference and browser dimensions differ')
    Image.fromarray(np.abs(ref.astype(np.int16) - got.astype(np.int16)).astype(np.uint8)).save(output / 'difference.png')
    ssim = image_ssim(reference, browser)
    psnr = image_psnr(reference, browser)
    return {'status': 'PASS' if ssim >= .90 else 'FAIL', 'rgb_ssim': ssim, 'psnr_db': psnr, 'psnr_infinite': psnr is None,
            'threshold_ssim': .90, 'reference_sha256': sha256_file(reference), 'browser_sha256': sha256_file(browser)}


def render_reference(source: Path, frame: dict, destination: Path, width: int, height: int):
    import torch
    from plyfile import PlyData
    from render_3dgs_reordering import activate_render_dependency
    activate_render_dependency(Path(__file__).resolve().parents[2] / 'deps/3dgs_over_moq')
    from streaming.transport.client.rasterizer import _render_gsplat
    # Do not silently replace the exact SH3 GPU path with the DC-only CPU fallback.
    if not torch.cuda.is_available(): raise RuntimeError('CUDA unavailable; exact SH3 gsplat reference cannot run')
    vertices = PlyData.read(source, mmap='r')['vertex'].data
    def columns(names):
        return torch.from_numpy(np.column_stack([vertices[name] for name in names]).astype(np.float32))
    means = columns(['x', 'y', 'z'])
    opacities = columns(['opacity'])
    dc = columns([f'f_dc_{i}' for i in range(3)])
    rest = columns([f'f_rest_{i}' for i in range(45)]).reshape(len(vertices), 3, 15).transpose(1, 2)
    sh = torch.cat([dc[:, None, :], rest], dim=1).reshape(len(vertices), 48)
    focal = (height / 2) / np.tan(np.radians(frame['fov']) / 2)
    intrinsic = torch.tensor([[focal, 0, width / 2], [0, focal, height / 2], [0, 0, 1]], dtype=torch.float32)
    scales, rotations = columns([f'scale_{i}' for i in range(3)]), columns([f'rot_{i}' for i in range(4)])
    view = torch.tensor(frame['view_matrix'], dtype=torch.float32)
    from gsplat.cuda._wrapper import fully_fused_projection
    effective_view = (torch.diag(torch.tensor([-1., 1., -1., 1.])) @ view).cuda()[None]
    selected = []
    # Use the SAME packed projection and defaults as gsplat.rasterization.
    # Chunk only this independent per-Gaussian operation. Rendering still uses
    # one global depth sort/composite; no approximate per-batch composition.
    for start in range(0, len(means), 200000):
        end = min(start + 200000, len(means))
        projection = fully_fused_projection(means[start:end].cuda(), None, rotations[start:end].cuda(),
            scales[start:end].cuda().exp(), effective_view, intrinsic.cuda()[None], width, height,
            packed=True, opacities=opacities[start:end].cuda().sigmoid().squeeze(-1))
        selected.append(projection[2].cpu().long() + start)
        del projection
    indices = torch.cat(selected)
    torch.cuda.empty_cache()
    image = _render_gsplat(means[indices], opacities[indices], sh[indices], scales[indices],
        rotations[indices], view,
        intrinsic, width, height, torch.zeros(3), 'cuda:0')
    Image.fromarray(image.numpy()).save(destination)
    return {'source_gaussians': len(means), 'projected_gaussians': len(indices),
        'culling': 'gsplat fully_fused_projection packed=True, identical rasterization defaults',
        'torch_version': torch.__version__, 'cuda_version': torch.version.cuda,
        'gpu': torch.cuda.get_device_name(0)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--trace', type=Path, required=True)
    parser.add_argument('--run', type=Path, required=True)
    args = parser.parse_args()
    output = args.run.resolve()
    configuration = json.loads((output / 'provenance.json').read_text())['configuration']
    source_hash = sha256_file(args.source)
    frames = json.loads(args.trace.read_text())
    frame = frames[0]
    contract = {'source': str(args.source.resolve()), 'source_sha256': source_hash, 'frame_index': 0,
        'pose': frame, 'width': configuration['width'], 'height': configuration['height'],
        'background': 'black', 'sh_degree': 3, 'renderer': 'repository rasterizer._render_gsplat',
        'threshold_ssim': .90, 'overlays': False, 'dye': False}
    try:
        if source_hash != configuration['source_sha256']: raise ValueError('browser/source PLY hash mismatch')
        if frame != configuration['fixed_pose']: raise ValueError('browser/reference source pose mismatch')
        telemetry = json.loads((output / 'player-telemetry.json').read_text())
        camera = telemetry['cameraPose']
        expected_view = np.diag([-1., -1., 1., 1.]) @ np.asarray(frame['view_matrix'])
        actual_view = np.asarray(camera['viewMatrix']).reshape(4, 4).T
        view_error = float(np.max(np.abs(actual_view - expected_view)))
        contract['browser_view_max_abs_error'] = view_error
        contract['browser_camera'] = camera
        # Source matrices are rounded; Three.js orthonormalizes their basis.
        if view_error > 1e-3: raise ValueError(f'browser effective view mismatch: {view_error}')
        if abs(camera['fov'] - frame['fov']) > 1e-6: raise ValueError('browser/reference FOV mismatch')
        if abs(camera['aspect'] - configuration['width'] / configuration['height']) > 1e-6:
            raise ValueError('browser/reference aspect mismatch')
        contract['reference_runtime'] = render_reference(args.source, frame, output / 'reference.png', configuration['width'], configuration['height'])
        result = compare(output / 'reference.png', output / 'browser.png', output)
    except RuntimeError as error:
        result = {'status': 'BLOCKED', 'reason': str(error), 'rgb_ssim': None, 'psnr_db': None}
    except Exception as error:
        result = {'status': 'FAIL', 'reason': f'{type(error).__name__}: {error}'}
    (output / 'comparison.json').write_text(json.dumps({'configuration': contract, **result}, indent=2) + '\n')
    print(json.dumps(result))
    raise SystemExit(0 if result['status'] == 'PASS' else 1)


if __name__ == '__main__':
    main()
