import assert from 'node:assert/strict';
import test from 'node:test';
import * as quality from '../tools/l4s/player_quality.mjs';

test('render timing records changed counters only during the playing window', () => {
    assert.equal(typeof quality.observeRenderTiming, 'function');
    let callback;
    let state = { renderFrames: 3, trace: { state: 'buffering', elapsedMs: 0 } };
    const result = quality.observeRenderTiming(() => state, fn => { callback = fn; });
    callback(10);
    state = { renderFrames: 4, trace: { state: 'playing', elapsedMs: 0 } };
    callback(20);
    callback(30);
    state.renderFrames = 5; state.trace.elapsedMs = 20;
    callback(40);
    state.trace.state = 'complete'; callback(50);
    assert.deepEqual(result.samples, [
        { observedAtMs: 20, timestampMs: 0, renderFrames: 4 },
        { observedAtMs: 40, timestampMs: 20, renderFrames: 5 },
    ]);
    assert.equal(result.complete, true);
});

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
                observations: [{ timestampMs: 0, capturedAtMs: 30, appliedGaussians: 0 }],
            };
            return true;
        },
    };
    try {
        const capture = await quality.installRenderLoopCapture(page, directory, 30);
        assert.deepEqual(config, { fps: 30, cameraTimestamps: "actual" });
        for (const slot of [2, 0]) await emit({}, { slot, timestampMs: slot * 34,
            nominalTimestampMs: slot * 1000 / 30, capturedAtMs: 100 + slot,
            cameraPose: {}, appliedGaussians: slot, image: 'data:image/png;base64,cG5n' });
        const result = await quality.captureRenderLoop(page, capture, 100);
        assert.equal(result.complete, false);
        assert.deepEqual(result.missingSlots, [1]);
        assert.deepEqual(result.frames.map(frame => frame.slot), [0, 2]);
        assert.equal(result.frames[0].image, undefined);
        assert.equal(fs.readFileSync(path.join(directory, 'frame-000002.png'), 'utf8'), 'png');
        assert.equal(JSON.parse(fs.readFileSync(path.join(root, 'render-timing.json'))).samples[0].observedAtMs, 30);
    } finally { fs.rmSync(root, { recursive: true }); }
});
