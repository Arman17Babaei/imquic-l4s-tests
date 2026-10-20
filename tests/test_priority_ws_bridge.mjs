import assert from 'node:assert/strict';
import fs from 'node:fs';
import net from 'node:net';
import path from 'node:path';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import test from 'node:test';
import { chromium } from 'playwright';

const root = path.resolve(import.meta.dirname, '..');

for (const objectWindow of [1, 4]) test(`bridge preserves objects with a bounded ${objectWindow}-object ACK window`, { timeout: 30000 }, async () => {
    fs.mkdirSync(path.join(root, 'results'), { recursive: true });
    const temporary = fs.mkdtempSync(path.join(root, 'results/bridge-test-'));
    const native = path.join(temporary, 'native.mjs');
    fs.writeFileSync(native, `#!/usr/bin/env node
import readline from 'node:readline';
function emit(header, payload = Buffer.alloc(0)) {
    const encoded = Buffer.from(JSON.stringify(header));
    const prefix = Buffer.alloc(4); prefix.writeUInt32BE(encoded.length);
    process.stdout.write(Buffer.concat([prefix, encoded, payload]));
}
emit({ wire: 'control', message: { type: 'ready' } });
readline.createInterface({ input: process.stdin }).on('line', line => {
    const command = JSON.parse(line);
    if (command.type === 'open') {
        emit({ wire: 'control', message: { type: 'fetch-opened', tile_id: 'tile', refinement: 0,
            request_id: 1, initial_priority: 255, mode: process.argv[4], congestion_options: process.argv[5] } });
        for (let object_id = 0; object_id < 6; object_id++)
            emit({ wire: 'object', tile_id: 'tile', refinement: 0, object_id, payload_bytes: 100000 }, Buffer.alloc(100000, object_id + 1));
    }
    if (command.type === 'close') {
        emit({ wire: 'control', message: { type: 'audit-complete', unresolved_updates: 0 } });
        process.stdout.end(() => process.exit(0));
    }
});
`);
    fs.chmodSync(native, 0o700);
    const reservation = net.createServer(); reservation.listen(0, '127.0.0.1'); await once(reservation, 'listening');
    const port = reservation.address().port; await new Promise(resolve => reservation.close(resolve));
    const bridge = spawn(process.execPath, [path.join(root, 'tools/l4s/priority_ws_bridge.mjs'), '--native', native,
        '--listen', String(port), '--object-window', String(objectWindow), '--transport-mode', 'prague', '--congestion-options', 'pacing_interval=1/1000', '--origin', 'null'], { stdio: ['ignore', 'ignore', 'pipe'] });
    let logs = ''; bridge.stderr.on('data', chunk => { logs += chunk; });
    let browser;
    try {
        const deadline = Date.now() + 5000;
        while (!logs.includes('bridge ws://')) {
            if (bridge.exitCode !== null || Date.now() > deadline) throw new Error(logs);
            await new Promise(resolve => setTimeout(resolve, 20));
        }
        browser = await chromium.launch({ headless: true });
        const page = await browser.newPage();
        const result = await page.evaluate(async ({port, objectWindow}) => {
            const socket = new WebSocket('ws://127.0.0.1:' + port, '3dgs-moqt-v1'); socket.binaryType = 'arraybuffer';
            const objects = [], events = [];
            socket.onmessage = event => {
                if (typeof event.data === 'string') events.push(JSON.parse(event.data));
                else {
                    const bytes = new Uint8Array(event.data), size = new DataView(event.data).getUint32(0);
                    const header = JSON.parse(new TextDecoder().decode(bytes.subarray(4, 4 + size)));
                    const payload = bytes.subarray(4 + size);
                    objects.push({ header, length: payload.length, valid: payload.every(value => value === header.object_id + 1) });
                }
            };
            await new Promise((resolve, reject) => { socket.onopen = resolve; socket.onerror = reject; });
            socket.send(JSON.stringify({ type: 'open', scene: 'test', fetches: [] }));
            const wait = async predicate => {
                const deadline = Date.now() + 5000;
                while (!predicate()) { if (Date.now() > deadline) throw new Error('timeout'); await new Promise(resolve => setTimeout(resolve, 10)); }
            };
            await wait(() => objects.length === objectWindow);
            await new Promise(resolve => setTimeout(resolve, 150));
            const heldCount = objects.length;
            const heldBuffers = await (await fetch("http://127.0.0.1:" + port + "/buffers")).json();
            for (let count = objectWindow; count < 6; count++) {
                socket.send(JSON.stringify({ type: 'object-ack' }));
                await wait(() => objects.length === count + 1);
            }
            for (let count = 0; count < objectWindow; count++) socket.send(JSON.stringify({ type: 'object-ack' }));
            socket.send(JSON.stringify({ type: 'close' }));
            await wait(() => events.some(event => event.type === 'audit-complete'));
            socket.close();
            return { objects, heldCount, heldBuffers, events };
        }, {port, objectWindow});
        assert.equal(result.heldCount, objectWindow);
        assert.equal(result.heldBuffers.outstanding_objects, objectWindow);
        assert.equal(result.heldBuffers.outstanding_payload_bytes, objectWindow * 100000);
        assert.equal(result.heldBuffers.peak_outstanding_objects, objectWindow);
        assert.equal(result.heldBuffers.native_paused, true);
        assert.ok(result.heldBuffers.window_full_ms >= 100);
        assert.equal(result.objects.length, 6);
        assert.ok(result.objects.every(object => object.valid && object.length === 100000));
        assert.equal(result.events.find(event => event.type === 'fetch-opened').mode, 'prague');
        assert.equal(result.events.find(event => event.type === 'fetch-opened').congestion_options, 'pacing_interval=1/1000');
        assert.ok(result.events.some(event => event.type === 'audit-complete'));
    } finally {
        if (browser) await browser.close();
        bridge.kill('SIGTERM');
        if (bridge.exitCode === null) await once(bridge, 'exit');
        fs.rmSync(temporary, { recursive: true });
    }
});

for (const exitCode of [0, 1]) test(`close without native audit is a protocol failure (native exit ${exitCode})`, { timeout: 15000 }, async () => {
    const temporary = fs.mkdtempSync(path.join(root, 'results/bridge-no-audit-'));
    const native = path.join(temporary, 'native.mjs');
    fs.writeFileSync(native, `#!/usr/bin/env node
import readline from 'node:readline';
const ready = Buffer.from(JSON.stringify({wire:'control', message:{type:'ready'}}));
const prefix = Buffer.alloc(4);prefix.writeUInt32BE(ready.length);process.stdout.write(Buffer.concat([prefix,ready]));
readline.createInterface({input:process.stdin}).on('line', line => {
    if (JSON.parse(line).type === 'close') process.exit(${exitCode});
});
`);
    fs.chmodSync(native, 0o700);
    const reservation = net.createServer(); reservation.listen(0, '127.0.0.1'); await once(reservation, 'listening');
    const port = reservation.address().port; await new Promise(resolve => reservation.close(resolve));
    const bridge = spawn(process.execPath, [path.join(root, 'tools/l4s/priority_ws_bridge.mjs'),
        '--native', native, '--listen', String(port), '--origin', 'null'], { stdio: ['ignore','ignore','pipe'] });
    let log = ''; bridge.stderr.on('data', chunk => { log += chunk; });
    let browser;
    try {
        const deadline = Date.now() + 5000;
        while (!log.includes('bridge ws://')) {
            if (bridge.exitCode !== null || Date.now() > deadline) throw new Error(log);
            await new Promise(resolve => setTimeout(resolve, 10));
        }
        browser = await chromium.launch({headless:true});
        const page = await browser.newPage();
        const result = await page.evaluate(port => new Promise((resolve,reject) => {
            const socket = new WebSocket(`ws://127.0.0.1:${port}`, '3dgs-moqt-v1');
            socket.onopen = () => socket.send(JSON.stringify({type:'close'}));
            socket.onclose = event => resolve({code:event.code,reason:event.reason});
            socket.onerror = () => reject(new Error('WebSocket failed'));
        }), port);
        assert.equal(result.code, 1011, 'graceful-close request is not evidence of native audit completion');
        assert.match(result.reason, /native sidecar exited/);
        assert.doesNotMatch(result.reason, /^audit complete$/);
        if (bridge.exitCode === null) await once(bridge, 'exit');
        assert.equal(bridge.exitCode, 1, 'guest process status must also reject a missing audit');
    } finally {
        if (browser) await browser.close();
        bridge.kill('SIGTERM');
        if (bridge.exitCode === null) await once(bridge, 'exit');
        fs.rmSync(temporary,{recursive:true});
    }
});
