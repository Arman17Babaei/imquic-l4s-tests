import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import fs from 'node:fs';
import http from 'node:http';
import net from 'node:net';
import path from 'node:path';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import test from 'node:test';
import { chromium } from 'playwright';

const root = path.resolve(import.meta.dirname, '..');
const artifacts = path.join(root, 'results/cpu-check');
fs.mkdirSync(artifacts, { recursive: true });
const hash = bytes => crypto.createHash('sha256').update(bytes).digest('hex');
function object(id) {
    const arrays = [new Uint32Array([id]), new Float32Array([id ? .2 : -.2, 0, -3]),
        new Float32Array([0]), new Float32Array(Array.from({ length: 48 }, (_, i) => (id + 1) * .01 * (i % 3 + 1))), new Float32Array([-2, -2, -2]), new Float32Array([1, 0, 0, 0])];
    const sections = arrays.map(array => {
        const bytes = Buffer.from(array.buffer), size = Buffer.alloc(4); size.writeUInt32LE(bytes.length);
        return Buffer.concat([size, bytes]);
    });
    const payload = Buffer.concat([Buffer.from('tile0'), Buffer.from('0'), ...sections]);
    const header = Buffer.alloc(40);
    [0x47535033, 2, 0, 5, 1, 0, id, 1, 3, payload.length].forEach((value, i) => header.writeUInt32LE(value, i * 4));
    return Buffer.concat([header, payload]);
}

for (const [playRequired, autoplay, dyeEnabled] of [[false, true, true], [true, true, true], [true, false, true], [true, false, false]]) test(`MoQ loading (Play: ${playRequired}, autoplay: ${autoplay}, dye: ${dyeEnabled})`, { timeout: 30000 }, async () => {
    const resultFile = path.join(artifacts, `play-${playRequired}-auto-${autoplay}-dye-${dyeEnabled}.json`);
    fs.rmSync(resultFile, { force: true });
    const objects = [object(0), object(1)], combined = Buffer.concat(objects);
    const manifest = { schema_version: 2, payload_encoding: 'gsp2', coordinate_system: 'source',
        scene: { id: 'late-objects', source_sha256: hash(combined), gaussian_count: 2, splat_size: 0,
            min: [-1, -1, -4], max: [1, 1, -2], center: [0, 0, -3], radius: 2 },
        tiling: { type: 'uniform-aabb-grid', dimensions: [1, 1, 1], occupied_tiles: 1 },
        progression: { ordering: 'source', layer_fractions: [1], cumulative_fractions: [1] },
        tiles: [{ id: 'tile0', grid_index: [0, 0, 0], min: [-1, -1, -4], max: [1, 1, -2], centroid: [0, 0, -3], gaussian_count: 2,
            refinements: [{ index: 0, name: 'base', file: 'not-served.gsp2', track: 'tile0/layer0', gaussian_count: 2,
                bytes: combined.length, object_count: 2, object_bytes: objects[0].length, fraction: 1, sha256: hash(combined),
                objects: objects.map((bytes, id) => ({ id, offset: id * bytes.length, bytes: bytes.length,
                    gaussian_count: 1, publisher_priority: 0, sha256: hash(bytes) })) }] }] };
    const trace = [0, 100].map(timestamp_ms => ({ timestamp_ms, camera_position: [0, 0, 0],
        camera_forward: [0, 0, -1], camera_up: [0, 1, 0], yaw_degrees: 0, fov: 60, aspect_ratio: 16 / 9 }));
    const server = http.createServer((request, response) => {
        const pathname = new URL(request.url, 'http://localhost').pathname;
        if (pathname === '/manifest-v2.json' || pathname === '/trace.json') {
            response.setHeader('Content-Type', 'application/json');
            response.end(JSON.stringify(pathname === '/trace.json' ? trace : manifest)); return;
        }
        if (pathname === '/not-served.gsp2') {
            response.setHeader('Content-Type', 'application/octet-stream'); response.end(combined); return;
        }
        const file = path.join(root, 'deps/gaussian-player/dist', pathname === '/' ? 'index.html' : pathname);
        if (!file.startsWith(path.join(root, 'deps/gaussian-player/dist') + '/') || !fs.existsSync(file)) {
            response.writeHead(404); response.end(); return;
        }
        response.setHeader('Content-Type', file.endsWith('.js') ? 'text/javascript' : 'text/html');
        response.end(fs.readFileSync(file));
    });
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    const port = server.address().port;
    const temporary = fs.mkdtempSync(path.join(root, 'results/playback-test-'));
    const native = path.join(temporary, 'native.mjs'), timingFile = path.join(temporary, 'timings.json');
    const startedFile = path.join(temporary, 'started.json');
    fs.writeFileSync(native, `#!/usr/bin/env node
import fs from 'node:fs';
import readline from 'node:readline';
fs.writeFileSync(${JSON.stringify(startedFile)}, JSON.stringify({ startedAt: Date.now() }));
const objects = ${JSON.stringify(objects.map(bytes => bytes.toString('base64')))}.map(value => Buffer.from(value, 'base64'));
let openedAt = 0, lastObjectAt = 0;
function emit(header, payload = Buffer.alloc(0)) {
    const bytes = Buffer.from(JSON.stringify(header)), size = Buffer.alloc(4); size.writeUInt32BE(bytes.length);
    process.stdout.write(Buffer.concat([size, bytes, payload]));
}
emit({ wire: 'control', message: { type: 'ready' } });
readline.createInterface({ input: process.stdin }).on('line', line => {
    const message = JSON.parse(line);
    if (message.type === 'open') {
        openedAt = Date.now();
        emit({ wire: 'control', message: { type: 'fetch-opened', tile_id: 'tile0', refinement: 0, request_id: 1, initial_priority: 0 } });
        objects.forEach((payload, id) => setTimeout(() => {
            if (id === 1) lastObjectAt = Date.now();
            emit({ wire: 'object', tile_id: 'tile0', refinement: 0, object_id: id, request_id: 1, payload_bytes: payload.length }, payload);
        }, id === 0 ? 250 : 2000));
    } else if (message.type === 'close') {
        fs.writeFileSync(${JSON.stringify(timingFile)}, JSON.stringify({ openedAt, lastObjectAt, closedAt: Date.now() }));
        emit({ wire: 'control', message: { type: 'audit-complete', unresolved_updates: 0 } });
        process.stdout.end(() => process.exit(0));
    }
});
`);
    fs.chmodSync(native, 0o700);
    const reservation = net.createServer(); reservation.listen(0, '127.0.0.1'); await once(reservation, 'listening');
    const bridgePort = reservation.address().port; await new Promise(resolve => reservation.close(resolve));
    const bridge = spawn(process.execPath, [path.join(root, 'tools/l4s/priority_ws_bridge.mjs'), '--native', native,
        '--listen', String(bridgePort), '--origin', `http://127.0.0.1:${port}`,
        ...(playRequired ? ['--persistent'] : [])], { stdio: ['ignore', 'ignore', 'pipe'] });
    let logs = ''; bridge.stderr.on('data', chunk => { logs += chunk; });
    let browser;
    try {
        const deadline = Date.now() + 5000;
        while (!logs.includes('bridge ws://')) {
            if (bridge.exitCode !== null || Date.now() > deadline) throw new Error(logs);
            await new Promise(resolve => setTimeout(resolve, 20));
        }
        browser = await chromium.launch({ headless: true,
            args: ['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'] });
        const page = await browser.newPage({ viewport: { width: 640, height: 360 } });
        const requests = [], sockets = [];
        page.on('request', request => requests.push(new URL(request.url()).pathname));
        page.on('websocket', socket => sockets.push(socket.url()));
        await page.goto(`http://127.0.0.1:${port}/?transport=moqt&sidecar=ws://127.0.0.1:${bridgePort}&trace=/trace.json&autoplay=${Number(autoplay)}&overlays=${autoplay ? 0 : 1}&dye=${Number(dyeEnabled)}&play=${Number(playRequired)}`);
        if (playRequired) {
            await page.getByRole('button', { name: 'Play — start loading' }).waitFor();
            await page.waitForTimeout(350);
            assert.equal(requests.includes('/manifest-v2.json'), false);
            assert.equal(requests.includes('/trace.json'), false);
            assert.equal(sockets.length, 0);
            assert.equal(fs.existsSync(startedFile), false, 'native client must not start before Play');
            await page.getByRole('button', { name: 'Play — start loading' }).click();
        }
        await page.waitForFunction(() => window.__priorityMoqMvp.appliedGaussians === 1 &&
            window.__priorityMoqMvp.rendererState.activeGaussians >= 1);
        assert.equal(await page.evaluate(() => window.__priorityMoqMvp.renderedMeshes), 0,
            'first validated object must render before its complete refinement');
        if (autoplay) {
            await page.waitForFunction(() => window.__priorityMoqMvp?.trace.state === 'draining');
            assert.equal(fs.existsSync(timingFile), false, 'camera completion must leave the data connection open');
            await page.waitForFunction(() => ['complete', 'failed'].includes(window.__priorityMoqMvp?.trace.state));
        } else {
            await page.waitForFunction(() => window.__priorityMoqMvp?.transportState === 'ready');
            await page.evaluate(() => window.__priorityMoqTurn());
            await page.waitForFunction(() => window.__priorityMoqMvp.events.some(event =>
                event.type === 'priorities-sent' && event.updates.some(update => update.priority >= 240)));
            await page.waitForFunction(() => window.__priorityMoqMvp.appliedGaussians === 2);
            await page.evaluate(() => window.__priorityMoqClose());
        }
        const state = await page.evaluate(() => window.__priorityMoqMvp);
        const graphics = await page.evaluate(() => {
            const gl = document.querySelector('canvas').getContext('webgl2');
            const debug = gl.getExtension('WEBGL_debug_renderer_info');
            return { renderer: gl.getParameter(debug ? debug.UNMASKED_RENDERER_WEBGL : gl.RENDERER) };
        });
        assert.match(graphics.renderer, /SwiftShader/i, 'CPU smoke must use software rendering');
        assert.equal(state.failure, null);
        if (autoplay) assert.equal(state.trace.state, 'complete');
        assert.equal(state.receivedGaussians, 2); assert.equal(state.appliedGaussians, 2);
        assert.equal(state.receivedPayloadBytes, combined.length);
        assert.equal(state.rejected, 0); assert.equal(state.events.filter(event => event.type === 'object').length, 2);
        if (dyeEnabled) assert.ok(state.trace.activeDyes > 0, 'new refinement must be dyed');
        if (!dyeEnabled) {
            await page.evaluate(async trace => window.__priorityMoqSeek(trace, 0), trace);
            const streamed = await page.evaluate(() => document.querySelector('canvas').toDataURL());
            const reference = await browser.newPage({ viewport: { width: 640, height: 360 } });
            await reference.goto(`http://127.0.0.1:${port}/?transport=http&trace=/trace.json&fixedFrame=1&overlays=0&dye=0`);
            await reference.waitForFunction(() => window.__priorityMoqMvp.renderReady);
            await reference.evaluate(() => window.__priorityMoqFreeze());
            const complete = await reference.evaluate(() => document.querySelector('canvas').toDataURL());
            assert.equal(streamed, complete, 'incremental SH3 GPU textures must match one-shot rendering');
            await reference.close();
        }
        const { openedAt, lastObjectAt, closedAt } = JSON.parse(fs.readFileSync(timingFile, 'utf8'));
        assert.ok(lastObjectAt >= openedAt + 1900); assert.ok(closedAt >= lastObjectAt);
        if (playRequired && autoplay) {
            fs.unlinkSync(startedFile); fs.unlinkSync(timingFile);
            await page.reload();
            await page.getByRole('button', { name: 'Play — start loading' }).waitFor();
            await page.waitForTimeout(350);
            assert.equal(fs.existsSync(startedFile), false);
            assert.equal(bridge.exitCode, null, 'persistent bridge must survive audit closure');
            await page.getByRole('button', { name: 'Play — start loading' }).click();
            await page.waitForFunction(() => window.__priorityMoqMvp?.trace.state === 'complete');
            assert.equal(await page.evaluate(() => window.__priorityMoqMvp.appliedGaussians), 2);
        }
        fs.writeFileSync(resultFile, JSON.stringify({
            status: 'PASS', fixture: 'two hashed SH3 objects; 100 ms motion; final object after 2000 ms',
            transport: 'simulated native delivery through the real WebSocket bridge',
            graphics, openedAt, lastObjectAt, closedAt, telemetry: state }, null, 2) + '\n');
    } finally {
        await browser?.close();
        bridge.kill('SIGTERM');
        if (bridge.exitCode === null) await once(bridge, 'exit');
        await new Promise(resolve => server.close(resolve));
        fs.rmSync(temporary, { recursive: true, force: true });
    }
});
