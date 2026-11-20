import assert from 'node:assert/strict';
import test from 'node:test';
import * as quality from '../tools/l4s/player_quality.mjs';

test('render-loop export keeps PNGs out of metadata and preserves failed cadence', async () => {
    const fs = await import('node:fs');
    const os = await import('node:os');
    const path = await import('node:path');
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'player-render-quality-'));
    const directory = path.join(root, 'candidate-frames');
    let emit, config;
    const page = {
        async exposeBinding(_name, callback) { emit = callback; },
        async addInitScript(_script, argument) { config = argument; },
        async waitForFunction() {},
        async evaluate(callback) {
            if (callback.toString().includes('__priorityMoqQualityFinish')) return {
                complete: false, expectedFrames: 3, capturedFrames: 2, missingSlots: [1], errors: [],
            };
            return true;
        },
    };
    try {
        const capture = await quality.installRenderLoopCapture(page, directory, 30);
        assert.deepEqual(config, { fps: 30, cameraTimestamps: "actual" });
        for (const slot of [2, 0]) await emit({}, { slot, timestampMs: slot * 34,
            nominalTimestampMs: slot * 1000 / 30,
            cameraPose: {}, appliedGaussians: slot, image: 'data:image/png;base64,cG5n' });
        const result = await quality.captureRenderLoop(page, capture, 100);
        assert.equal(result.complete, false);
        assert.deepEqual(result.missingSlots, [1]);
        assert.deepEqual(result.frames.map(frame => frame.slot), [0, 2]);
        assert.equal(result.frames[0].image, undefined);
        assert.equal(fs.readFileSync(path.join(directory, 'frame-000002.png'), 'utf8'), 'png');
        assert.equal(fs.existsSync(path.join(root, 'render-timing.json')), false);
    } finally { fs.rmSync(root, { recursive: true }); }
});

test('reference trace grid uses common zero and a half-open duration', () => {
    const grid = quality.createTraceFrameGrid([{ timestamp_ms: 0 }, { timestamp_ms: 250 }], 10);
    assert.deepEqual(grid.map(frame => frame.timestampMs), [0, 100, 200]);
    assert.deepEqual(grid.map(frame => frame.nominalTimestampMs), [0, 100, 200]);
    assert.deepEqual(quality.createTraceFrameGrid([{ timestamp_ms: 0 }, { timestamp_ms: 6000 }], 10).length, 60);
});

test('reference trace grid rejects invalid duration, origin, timestamps and rate', () => {
    for (const trace of [[], [{ timestamp_ms: 0 }], [{ timestamp_ms: 1 }, { timestamp_ms: 100 }],
        [{ timestamp_ms: 0 }, { timestamp_ms: NaN }], [{ timestamp_ms: 0 }, { timestamp_ms: Infinity }],
        [{ timestamp_ms: 0 }, { timestamp_ms: 0 }], [{ timestamp_ms: 0 }, { timestamp_ms: 50 }, { timestamp_ms: 40 }]])
        assert.throws(() => quality.createTraceFrameGrid(trace, 10));
    for (const fps of [0, -1, NaN, Infinity])
        assert.throws(() => quality.createTraceFrameGrid([{ timestamp_ms: 0 }, { timestamp_ms: 100 }], fps));
});

test('reference readiness accepts a settled complete sort without live debounce', () => {
    const state = { renderReady: false, sourceGaussians: 42, appliedGaussians: 42,
        rendererState: { activeGaussians: 42, sorting: false, pendingSort: false } };
    assert.equal(quality.referenceSampleReady(state), true);
    assert.equal(quality.referenceSampleReady({ ...state, appliedGaussians: 41 }), false);
    assert.equal(quality.referenceSampleReady({ ...state, rendererState: { ...state.rendererState, sorting: true } }), false);
    assert.equal(quality.referenceSampleReady({ ...state, rendererState: { ...state.rendererState, pendingSort: true } }), false);
    assert.equal(quality.referenceSampleReady({ ...state, rendererState: { ...state.rendererState, activeGaussians: 41 } }), false);
});

test('reference browser callback seeks once and reuses capture PNG', async () => {
    const fs = await import('node:fs');
    const os = await import('node:os');
    const path = await import('node:path');
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'reference-callback-'));
    const trace = path.join(root, 'trace.json');
    fs.writeFileSync(trace, JSON.stringify([{ timestamp_ms: 0 }, { timestamp_ms: 100 }]));
    const previousWindow = globalThis.window, previousDocument = globalThis.document;
    let seeks = 0, captures = 0, encodes = 0;
    const image = 'data:image/png;base64,cG5n';
    globalThis.window = {
        __priorityMoqMvp: { renderReady: true, sourceGaussians: 42, appliedGaussians: 42,
            cameraPose: {}, rendererState: { activeGaussians: 42, sorting: false, pendingSort: false } },
        async __priorityMoqSeek() { seeks++; },
        __priorityMoqCapture() { captures++; return { image }; },
    };
    globalThis.document = { querySelector() { return {
        getContext() { return { flush() {} }; },
        toDataURL() { encodes++; return image; },
    }; } };
    const page = { async waitForFunction() {}, async waitForTimeout() {},
        async evaluate(callback, argument) { return callback(argument); } };
    try {
        await quality.renderReference(page, null, trace, path.join(root, 'frames'), 1000,
            { traceGrid: true, fps: 10 });
        assert.equal(seeks, 1);
        assert.equal(captures, 1);
        assert.equal(encodes, 1);
    } finally {
        globalThis.window = previousWindow;
        globalThis.document = previousDocument;
        fs.rmSync(root, { recursive: true });
    }
});

test('reference shard keeps original grid timestamps and local output indices', async () => {
    const fs = await import('node:fs');
    const os = await import('node:os');
    const path = await import('node:path');
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'reference-shard-'));
    const trace = path.join(root, 'trace.json');
    fs.writeFileSync(trace, JSON.stringify([{ timestamp_ms: 0 }, { timestamp_ms: 400 }]));
    const page = { async waitForFunction() {}, async waitForTimeout() {},
        async evaluate(_callback, { timestampMs }) { return {
            ready: true, timestampMs, cameraPose: {}, appliedGaussians: 42,
            image: 'data:image/png;base64,cG5n',
        }; } };
    try {
        await quality.renderReference(page, null, trace, path.join(root, 'frames'), 1000,
            { traceGrid: true, fps: 10, frameStart: 2, frameEnd: 4 });
        const record = JSON.parse(fs.readFileSync(path.join(root, 'frames/frames.json')));
        assert.equal(record.complete, true);
        assert.equal(record.frameStart, 2);
        assert.deepEqual(record.frames.map(frame => frame.timestampMs), [200, 300]);
        assert.deepEqual(record.frames.map(frame => frame.index), [0, 1]);
        await assert.rejects(quality.renderReference(page, null, trace, path.join(root, 'bad'), 1000,
            { traceGrid: true, fps: 10, frameStart: 4, frameEnd: 5 }), /invalid reference frame range/);
    } finally { fs.rmSync(root, { recursive: true }); }
});

test('independent reference seeks every grid pose and records actual poses without candidate data', async () => {
    const fs = await import('node:fs');
    const os = await import('node:os');
    const path = await import('node:path');
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'trace-reference-'));
    const trace = path.join(root, 'trace.json');
    fs.writeFileSync(trace, JSON.stringify([{ timestamp_ms: 0 }, { timestamp_ms: 250 }]));
    const sought = [];
    const seekFlags = [];
    const tracePayloads = [];
    const page = {
        async waitForFunction() {},
        async waitForTimeout() {},
        async evaluate(_callback, { timestampMs, seek, trace }) {
            sought.push(timestampMs);
            seekFlags.push(seek);
            tracePayloads.push(trace);
            return { ready: true, timestampMs, cameraPose: { actualSeek: timestampMs },
                appliedGaussians: 42, image: 'data:image/png;base64,cG5n' };
        },
    };
    try {
        const directory = path.join(root, 'frames');
        await quality.renderReference(page, null, trace, directory, 1000, { traceGrid: true, fps: 10 });
        const manifest = JSON.parse(fs.readFileSync(path.join(directory, 'frames.json')));
        assert.deepEqual(sought, [0, 0, 100, 100, 200, 200]);
        assert.deepEqual(seekFlags, [true, false, true, false, true, false]);
        assert.deepEqual(tracePayloads.map(value => value === null), [false, true, false, true, false, true]);
        assert.equal(manifest.complete, true);
        assert.equal(manifest.requestedFps, 10);
        assert.equal(manifest.expectedFrames, 3);
        assert.equal(manifest.traceOriginMs, 0);
        assert.equal(manifest.durationMs, 250);
        assert.deepEqual(manifest.frames.map(frame => frame.cameraPose.actualSeek), [0, 100, 200]);
        assert.equal(manifest.frames[1].nominalTimestampMs, 100);
        const candidate = path.join(root, 'candidate.json');
        fs.writeFileSync(candidate, JSON.stringify({ complete: true, frames: [{ timestampMs: NaN }] }));
        await assert.rejects(quality.renderReference(page, candidate, trace, path.join(root, 'bad'), 100),
            /finite timestamps and camera poses/);
        assert.equal(fs.existsSync(path.join(root, 'bad')), false);
    } finally { fs.rmSync(root, { recursive: true }); }
});

test('candidate-driven reference still enforces exact candidate camera poses', async () => {
    const fs = await import('node:fs');
    const os = await import('node:os');
    const path = await import('node:path');
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'pose-reference-'));
    const trace = path.join(root, 'trace.json');
    const candidate = path.join(root, 'candidate.json');
    const pose = { viewMatrix: [1, 0, 0, 1], position: [0, 1, 2], up: [0, 1, 0], fov: 50, aspect: 1.5 };
    fs.writeFileSync(trace, JSON.stringify([{ timestamp_ms: 0 }, { timestamp_ms: 100 }]));
    const page = { async waitForFunction() {}, async waitForTimeout() {},
        async evaluate(_callback, { timestampMs }) { return { ready: true, timestampMs,
            cameraPose: pose, image: 'data:image/png;base64,cG5n' }; } };
    try {
        fs.writeFileSync(candidate, JSON.stringify({ complete: true,
            frames: [{ index: 0, timestampMs: 75, cameraPose: pose }] }));
        await quality.renderReference(page, candidate, trace, path.join(root, 'valid'), 100);
        const altered = { ...pose, position: [1, 1, 2] };
        fs.writeFileSync(candidate, JSON.stringify({ complete: true,
            frames: [{ index: 0, timestampMs: 75, cameraPose: altered }] }));
        await assert.rejects(quality.renderReference(page, candidate, trace, path.join(root, 'mismatch'), 100),
            /reference camera position mismatch/);
        const failed = JSON.parse(fs.readFileSync(path.join(root, 'mismatch/frames.json')));
        assert.equal(failed.complete, false);
        assert.equal(failed.frames.length, 0);
    } finally { fs.rmSync(root, { recursive: true }); }
});
