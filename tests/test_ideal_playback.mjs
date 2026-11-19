import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import fs from 'node:fs';
import net from 'node:net';
import path from 'node:path';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import test from 'node:test';
import { chromium } from 'playwright';
import { installRenderLoopCapture, captureRenderLoop, renderReference } from '../tools/l4s/player_quality.mjs';

const root = path.resolve(import.meta.dirname, '..');
const hash = file => crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
async function port() {
    const server = net.createServer(); server.listen(0, '127.0.0.1'); await once(server, 'listening');
    const value = server.address().port; await new Promise(resolve => server.close(resolve)); return value;
}

for (const encoding of ['gsp2','splat']) test(`real IMQUIC ${encoding} offline frames and pixels survive host waits`, { timeout: 120000 }, async () => {
    const directory = fs.mkdtempSync(path.join(root, 'results/ideal-playback-'));
    const setup = spawn('python3', ['-c', 'from tests.test_ideal_imquic import player_fixture; import sys; player_fixture(sys.argv[1],sys.argv[2])', directory, encoding], { cwd: root });
    assert.equal((await once(setup, 'close'))[0], 0);
    const manifest = path.join(directory, 'manifest-v2.json'), trace = path.join(directory, 'trace.json');
    const processes = [], logs = [];
    const start = (name, command, args) => {
        const fd = fs.openSync(path.join(directory, `${name}.log`), 'w'); logs.push(fd);
        const child = spawn(command, args, { cwd: root, stdio: ['ignore', fd, fd] }); processes.push(child); return child;
    };
    const webPort = await port();
    start('web', 'python3', ['tools/l4s/camera_trace_player_server.py', '--dist', 'deps/gaussian-player/dist',
        '--manifest', manifest, '--trace', trace, '--port', String(webPort), '--metadata-only']);
    let browser;
    try {
        for (let i = 0; ; i++) {
            try { if ((await fetch(`http://127.0.0.1:${webPort}/`)).ok) break; } catch {}
            if (i === 100) throw new Error('fixture web server did not start');
            await new Promise(resolve => setTimeout(resolve, 20));
        }
        browser = await chromium.launch({ headless: true,
            args: ['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'] });
        const runs = [];
        for (const waitMs of [0, 80]) {
            const output = path.join(directory, `wait-${waitMs}`); fs.mkdirSync(output);
            const config = path.join(output, 'ideal-link.json');
            fs.writeFileSync(config, JSON.stringify({ manifest, output, bandwidth_mbps: encoding==='splat' ? .0016 : .008, data_delay_ms: 50 }));
            const bridgePort = await port();
            start(`bridge-${waitMs}`, 'node', ['tools/l4s/priority_ws_bridge.mjs', '--ideal-config', config,
                '--listen', String(bridgePort), '--origin', `http://127.0.0.1:${webPort}`]);
            for (let i = 0; ; i++) {
                try { if ((await fetch(`http://127.0.0.1:${bridgePort}/audit`, {headers:{Origin:`http://127.0.0.1:${webPort}`}})).status === 202) break; } catch {}
                if (i === 100) throw new Error('fixture bridge did not start');
                await new Promise(resolve => setTimeout(resolve, 20));
            }
            const page = await browser.newPage({ viewport: { width: 320, height: 180 } });
            // Perturb host timing only; there is no production delay/test option.
            await page.addInitScript(waitMs => {
                const send = WebSocket.prototype.send;
                WebSocket.prototype.send = function(data) {
                    if (typeof data === 'string' && JSON.parse(data).type === 'advance')
                        setTimeout(() => send.call(this, data), waitMs);
                    else send.call(this, data);
                };
            }, waitMs);
            const capture = await installRenderLoopCapture(page, path.join(output, 'frames'), 10, 'fixed');
            await page.goto(`http://127.0.0.1:${webPort}/?transport=ideal&sidecar=ws://127.0.0.1:${bridgePort}&trace=/camera-trace.json&autoplay=1&fixedHorizon=1&record=0&recordFps=10&overlays=0&dye=0`);
            const frames = await captureRenderLoop(page, capture, 60000);
            assert.equal(frames.complete, true, JSON.stringify(frames));
            const telemetry = await page.evaluate(() => window.__priorityMoqMvp);
            fs.writeFileSync(path.join(output, 'telemetry.json'), JSON.stringify(telemetry, null, 2));
            assert.equal(telemetry.failure, null);
            assert.deepEqual(frames.frames.map(f => f.timestampMs), [0,100,200,300,400,500,600]);
            assert.deepEqual(frames.frames.map(f => f.appliedGaussians), encoding==='gsp2' ? [0,0,0,0,1,1,1] : [0,0,0,0,2,2,2]);
            const summary = JSON.parse(fs.readFileSync(path.join(output, 'ideal-summary.json')));
            assert.equal(summary.status, 'PASS');
            assert.equal(summary.simulated_time_ms, 700);
            assert.equal(telemetry.trace.gaussiansAtHorizon, 2);
            assert.equal(telemetry.trace.gaussiansAtVideoEnd, encoding==='gsp2' ? 1 : 2);
            const schedule = fs.readFileSync(path.join(output, 'ideal-events.jsonl'), 'utf8').trim().split('\n')
                .map(JSON.parse).filter(e => ['serialization', 'arrival', 'dispatch'].includes(e.event));
            const membership = telemetry.events.filter(e => e.type === 'render-frame');
            const pixels = frames.frames.map(f => hash(path.join(capture.directory, f.file)));
            assert.notEqual(pixels[0], pixels.at(-1), 'delivered geometry must change rendered pixels');
            runs.push({ schedule, membership, pixels });
            if (!waitMs) {
                const reference = await browser.newPage({ viewport: { width: 320, height: 180 } });
                // References need the original payload files, served by a separate full-scene server.
                const referencePort = await port();
                start('reference-web', 'python3', ['tools/l4s/camera_trace_player_server.py', '--dist', 'deps/gaussian-player/dist',
                    '--manifest', manifest, '--trace', trace, '--port', String(referencePort)]);
                for (let i = 0; ; i++) {
                    try { if ((await fetch(`http://127.0.0.1:${referencePort}/`)).ok) break; } catch {}
                    if (i === 100) throw new Error('reference server did not start');
                    await new Promise(resolve => setTimeout(resolve, 20));
                }
                await reference.goto(`http://127.0.0.1:${referencePort}/?transport=http&trace=/camera-trace.json&fixedFrame=1&overlays=0&record=0`);
                const target = path.join(output, 'reference');
                await renderReference(reference, path.join(capture.directory, 'frames.json'), trace, target, 30000);
                const references = JSON.parse(fs.readFileSync(path.join(target, 'frames.json')));
                assert.deepEqual(references.frames.map(f => f.cameraPose), frames.frames.map(f => f.cameraPose));
                if (encoding === 'splat') assert.equal(pixels[4],
                    hash(path.join(target, references.frames[4].file)),
                    'first complete SPLAT frame must finish mesh initialization before capture');
                await reference.close();
            }
            await page.close();
        }
        assert.deepEqual(runs[0], runs[1]);
        fs.writeFileSync(path.join(directory, 'repeatability.json'), JSON.stringify({status:'PASS',
            hostWaitMs:80, encoding, frames:7, identicalSchedule:true, identicalMembership:true, identicalPixels:true}));
        console.log(`Retained ideal repeatability evidence: ${directory}`);
    } finally {
        if (browser) await browser.close();
        for (const child of processes.reverse()) {
            if (child.exitCode === null) { child.kill('SIGTERM'); await once(child, 'close'); }
        }
        for (const fd of logs) fs.closeSync(fd);
    }
});
