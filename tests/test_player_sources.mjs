import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import test from 'node:test';
import { chromium } from 'playwright';

const root = path.resolve(import.meta.dirname, '..');
const dist = path.join(root, 'deps/gaussian-player/dist');
const hash = bytes => crypto.createHash('sha256').update(bytes).digest('hex');
const splat = Buffer.alloc(32);
[0, 0, -3, .1, .1, .1].forEach((value, i) => splat.writeFloatLE(value, i * 4));
splat.fill(255, 24, 28); splat[28] = 255; splat.fill(128, 29);
const properties = ['x', 'y', 'z', 'f_dc_0', 'f_dc_1', 'f_dc_2', 'opacity',
    'scale_0', 'scale_1', 'scale_2', 'rot_0', 'rot_1', 'rot_2', 'rot_3'];
const values = [0, 0, -3, 1, 0, 0, 4, -2, -2, -2, 1, 0, 0, 0];
const vertices = Buffer.alloc(values.length * 4);
values.forEach((value, i) => vertices.writeFloatLE(value, i * 4));
const ply = Buffer.concat([Buffer.from(`ply\nformat binary_little_endian 1.0\nelement vertex 1\n${properties.map(p => `property float ${p}\n`).join('')}end_header\n`), vertices]);
const refinements = Array.from({ length: 5 }, (_, index) => ({ index, name: `r${index}`,
    file: `r${index}.splat`, track: `tile/r${index}`, gaussian_count: 1, bytes: 32,
    object_count: 1, object_bytes: 32, fraction: .2, sha256: hash(splat),
    objects: [{ id: 0, offset: 0, bytes: 32, publisher_priority: 0, sha256: hash(splat) }] }));
const manifest = { schema_version: 2,
    scene: { id: 'fixture', gaussian_count: 5, splat_size: 32, source_sha256: '',
        min: [-1,-1,-4], max: [1,1,-2], center: [0,0,-3], radius: 2 },
    tiling: { type: 'uniform-aabb-grid', dimensions: [1,1,1], occupied_tiles: 1 },
    progression: { ordering: 'source', layer_fractions: [.2,.2,.2,.2,.2], cumulative_fractions: [.2,.4,.6,.8,1] },
    tiles: [{ id: 'tile', min: [-1,-1,-4], max: [1,1,-2], centroid: [0,0,-3],
        grid_index: [0,0,0], gaussian_count: 5, refinements }] };
const trace = [0, 300].map(timestamp_ms => ({ timestamp_ms, camera_position: [0,0,0],
    camera_forward: [0,0,-1], camera_up: [0,1,0], yaw_degrees: 0, fov: 60, aspect_ratio: 16/9 }));

test('HTTP sources load without tile clicks or bicycle assets, with independent overlay controls', { timeout: 60000 }, async t => {
    const requests = [];
    const server = http.createServer((request, response) => {
        const pathname = new URL(request.url, 'http://localhost').pathname;
        requests.push(pathname);
        const json = pathname === '/scene/manifest.json' ? manifest : pathname === '/trace.json' ? trace
            : pathname === '/moving-trace.json' ? [trace[0], { ...trace[1], timestamp_ms: 1500, camera_position: [1,0,0] }] : null;
        if (json) { response.setHeader('Content-Type', 'application/json'); response.end(JSON.stringify(json)); return; }
        if (/^\/scene\/r[0-4]\.splat$/.test(pathname) || pathname === '/point.splat') { response.end(splat); return; }
        if (pathname === '/point.ply') { response.end(ply); return; }
        const file = path.join(dist, pathname === '/' ? 'index.html' : pathname);
        if (!file.startsWith(dist + '/') || !fs.existsSync(file)) { response.writeHead(404); response.end(); return; }
        response.setHeader('Content-Type', file.endsWith('.js') ? 'text/javascript' : 'text/html');
        response.end(fs.readFileSync(file));
    });
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    const browser = await chromium.launch({ headless: true,
        args: ['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'] });
    try {
        const base = `http://127.0.0.1:${server.address().port}`;
        for (const source of ['ply', 'splat', 'manifest']) await t.test(source, async () => {
            requests.length = 0;
            const page = await browser.newPage({ viewport: { width: 640, height: 360 } });
            const errors = [];
            page.on('pageerror', error => errors.push(error.message));
            try {
                const query = source === 'manifest' ? 'manifest=/scene/manifest.json&trace=/trace.json&autoplay=1&dye=1'
                    : `source=${source}&${source}=/point.${source}`;
                await page.goto(`${base}/?transport=http&overlays=0&${query}`);
                await page.waitForFunction(() => window.__priorityMoqMvp?.failure || window.__priorityMoqMvp?.renderReady, null, { timeout: 10000 });
                const telemetry = await page.evaluate(() => window.__priorityMoqMvp);
                assert.equal(telemetry.failure, null);
                assert.equal(telemetry.appliedGaussians, source === 'manifest' ? 5 : 1);
                assert.equal(telemetry.receivedGaussians, telemetry.appliedGaussians);
                assert.deepEqual(errors, []);
                assert.equal(requests.some(url => url.includes('bicycle') || url.includes('3dgs_over_moq')), false);
                if (source === 'manifest') {
                    await page.waitForFunction(() => window.__priorityMoqMvp.trace.state === 'complete', null, { timeout: 10000 });
                    assert.equal((await page.evaluate(() => window.__priorityMoqMvp)).receivedPayloadBytes, 160);
                } else {
                    await page.locator('canvas').first().click();
                    await page.waitForFunction(() => document.pointerLockElement !== null, null, { timeout: 2000 });
                    const before = await page.evaluate(() => window.__priorityMoqMvp.cameraPose.position);
                    await page.keyboard.down('w'); await page.waitForTimeout(150); await page.keyboard.up('w');
                    const after = await page.evaluate(() => window.__priorityMoqMvp.cameraPose.position);
                    assert.notDeepEqual(after, before);
                }
            } finally { await page.close(); }
        });
        for (const source of ['ply', 'splat']) await t.test(`raw ${source} replay keeps moving while Spark re-sorts the camera`, async () => {
            const page = await browser.newPage({ viewport: { width: 640, height: 360 } });
            try {
                await page.goto(`${base}/?source=${source}&${source}=/point.${source}&trace=/moving-trace.json&autoplay=1&overlays=0`);
                await page.waitForFunction(() => window.__priorityMoqMvp?.trace.state === 'playing' &&
                    window.__priorityMoqMvp.trace.elapsedMs > 0, null, { timeout: 10000 });
                const before = await page.evaluate(() => window.__priorityMoqMvp.trace.elapsedMs);
                await page.waitForTimeout(250);
                const after = await page.evaluate(() => window.__priorityMoqMvp.trace.elapsedMs);
                assert.ok(after - before > 150, `trace stalled: ${before} -> ${after} ms`);
                await page.waitForFunction(() => window.__priorityMoqMvp.trace.state === 'complete', null, { timeout: 10000 });
            } finally { await page.close(); }
        });
    } finally { await browser.close(); await new Promise(resolve => server.close(resolve)); }
});
