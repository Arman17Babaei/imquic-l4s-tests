import fs from 'node:fs';
import path from 'node:path';

function writeFrame(directory, frames, sample) {
    const file = `frame-${String(frames.length).padStart(6, '0')}.png`;
    fs.writeFileSync(path.join(directory, file), Buffer.from(sample.image.split(',')[1], 'base64'));
    const { image, ...metadata } = sample;
    frames.push({ index: frames.length, file, ...metadata });
}

export async function capturePlayback(page, directory, fps, timeoutMs) {
    fs.mkdirSync(directory);
    const frames = [];
    const deadline = Date.now() + timeoutMs;
    let complete = false, lastTimestamp = -1, nextTimestamp = 0;
    try {
        while (Date.now() < deadline) {
            const sample = await page.evaluate(({ nextTimestamp, lastTimestamp }) => {
                const state = window.__priorityMoqMvp;
                if (!state) return { state: 'loading' };
                if (state.failure || state.trace?.state === 'failed')
                    return { failure: state.failure || state.trace.failure };
                const timestampMs = state.trace.elapsedMs;
                const terminal = ['stopping', 'draining', 'complete'].includes(state.trace.state);
                const capture = (state.trace.state === 'playing' || terminal) &&
                    state.renderFrames > 0 && timestampMs > lastTimestamp &&
                    (timestampMs >= nextTimestamp || terminal);
                return { state: state.trace.state, ...(capture ? window.__priorityMoqCapture() : {}) };
            }, { nextTimestamp, lastTimestamp });
            if (sample.failure) throw new Error(sample.failure);
            if (sample.image) {
                writeFrame(directory, frames, sample);
                lastTimestamp = sample.timestampMs;
                nextTimestamp = lastTimestamp + 1000 / fps;
            }
            if (sample.state === 'complete') { complete = true; break; }
            await page.waitForTimeout(Math.min(50, 1000 / fps));
        }
        if (!complete || !frames.length) throw new Error('playback frame capture did not complete before timeout');
    } finally {
        fs.writeFileSync(path.join(directory, 'frames.json'), JSON.stringify({
            complete, requestedFps: fps, frames,
        }, null, 2) + '\n');
    }
    return frames;
}

export async function renderReference(page, candidateManifest, tracePath, directory, timeoutMs) {
    const candidate = JSON.parse(fs.readFileSync(candidateManifest, 'utf8'));
    if (!candidate.complete || !candidate.frames.length) throw new Error('candidate frame capture is incomplete');
    const trace = JSON.parse(fs.readFileSync(tracePath, 'utf8'));
    fs.mkdirSync(directory);
    await page.waitForFunction(() => window.__priorityMoqMvp?.renderReady || window.__priorityMoqMvp?.failure,
        null, { timeout: timeoutMs });
    const frames = [];
    let complete = false;
    try {
        for (const candidateFrame of candidate.frames) {
            const deadline = Date.now() + timeoutMs;
            let previous = null, settled = null;
            while (Date.now() < deadline) {
                const sample = await page.evaluate(async ({ trace, timestampMs }) => {
                    if (window.__priorityMoqMvp.failure) throw new Error(window.__priorityMoqMvp.failure);
                    await window.__priorityMoqSeek(trace, timestampMs);
                    const state = window.__priorityMoqMvp;
                    return { ready: state.renderReady, timestampMs, cameraPose: state.cameraPose,
                        appliedGaussians: state.appliedGaussians,
                        image: document.querySelector('canvas').toDataURL('image/png') };
                }, { trace, timestampMs: candidateFrame.timestampMs });
                if (sample.ready && sample.image === previous) { settled = sample; break; }
                previous = sample.image;
                await page.waitForTimeout(50);
            }
            if (!settled) throw new Error(`reference frame ${candidateFrame.index} did not settle`);
            const actual = settled.cameraPose, expected = candidateFrame.cameraPose;
            for (const field of ['viewMatrix', 'position', 'up']) {
                if (!actual[field] || !expected[field] || actual[field].length !== expected[field].length ||
                    actual[field].some((value, i) => Math.abs(value - expected[field][i]) > 1e-4))
                    throw new Error(`reference camera ${field} mismatch at frame ${candidateFrame.index}`);
            }
            for (const field of ['fov', 'aspect'])
                if (Math.abs(actual[field] - expected[field]) > 1e-6)
                    throw new Error(`reference camera ${field} mismatch at frame ${candidateFrame.index}`);
            delete settled.ready;
            writeFrame(directory, frames, settled);
        }
        complete = true;
    } finally {
        fs.writeFileSync(path.join(directory, 'frames.json'), JSON.stringify({
            complete, candidateManifest: path.resolve(candidateManifest), frames,
        }, null, 2) + '\n');
    }
}
