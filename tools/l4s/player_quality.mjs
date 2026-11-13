import fs from 'node:fs';
import path from 'node:path';

function writeFrame(directory, frames, sample) {
    const file = `frame-${String(frames.length).padStart(6, '0')}.png`;
    fs.writeFileSync(path.join(directory, file), Buffer.from(sample.image.split(',')[1], 'base64'));
    const { image, ...metadata } = sample;
    frames.push({ index: frames.length, file, ...metadata });
}

// Browser RAF observations of completed player renders, not GPU presentation times.
export function observeRenderTiming(getState, schedule) {
    const result = { complete: false, samples: [] };
    let lastCounter = -1;
    const observe = now => {
        const state = getState();
        if (state?.trace?.state === 'playing' && state.renderFrames !== lastCounter) {
            result.samples.push({ observedAtMs: now, timestampMs: state.trace.elapsedMs,
                renderFrames: state.renderFrames });
            lastCounter = state.renderFrames;
        }
        if (['complete', 'failed'].includes(state?.trace?.state)) result.complete = true;
        else schedule(observe);
    };
    schedule(observe);
    return result;
}

export async function capturePlayback(page, directory, fps, timeoutMs) {
    fs.mkdirSync(directory);
    const frames = [];
    await page.evaluate(source => {
        const observe = (0, eval)(`(${source})`);
        window.__renderTiming = observe(() => window.__priorityMoqMvp,
            callback => requestAnimationFrame(callback));
    }, observeRenderTiming.toString());
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
        const timing = await page.evaluate(() => window.__renderTiming).catch(() => null);
        fs.writeFileSync(path.join(directory, '..', 'render-timing.json'), JSON.stringify(timing, null, 2) + '\n');
        fs.writeFileSync(path.join(directory, 'frames.json'), JSON.stringify({
            complete, requestedFps: fps, frames,
        }, null, 2) + '\n');
    }
    return frames;
}

export async function renderReference(page, candidateManifest, tracePath, directory, timeoutMs, options = {}) {
    const candidate = JSON.parse(fs.readFileSync(candidateManifest, 'utf8'));
    if ((!candidate.complete && !options.allowPartial) || !candidate.frames.length) throw new Error('candidate frame capture is incomplete');
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

// Install before navigation so the first (empty) rendered frame can be exported.
export async function installRenderLoopCapture(page, directory, fps, cameraTimestamps = "actual") {
    fs.mkdirSync(directory);
    const frames = [];
    const exportErrors = [];
    await page.exposeBinding('__priorityMoqQualityEmit', async (_source, sample) => {
        try {
            const { image, ...metadata } = sample;
            if (!Number.isInteger(sample.slot) || sample.slot < 0 ||
                !image?.startsWith('data:image/png;base64,')) throw new Error('invalid render-loop PNG sample');
            if (frames.some(frame => frame.slot === sample.slot)) throw new Error(`duplicate slot ${sample.slot}`);
            const file = `frame-${String(sample.slot).padStart(6, '0')}.png`;
            await fs.promises.writeFile(path.join(directory, file), Buffer.from(image.split(',')[1], 'base64'));
            frames.push({ file, ...metadata });
        } catch (error) {
            exportErrors.push(String(error));
            throw error;
        }
    });
    await page.addInitScript(({ fps, cameraTimestamps }) => {
        window.__priorityMoqQualityConfig = { fps, cameraTimestamps, emitBinding: '__priorityMoqQualityEmit' };
    }, { fps, cameraTimestamps });
    return { directory, fps, cameraTimestamps, frames, exportErrors };
}

export async function captureRenderLoop(page, capture, timeoutMs) {
    const { directory, fps, frames, exportErrors } = capture;
    let summary = null;
    let playbackComplete = false;
    try {
        await page.waitForFunction(() => ['complete', 'failed'].includes(window.__priorityMoqMvp?.trace?.state),
            null, { timeout: timeoutMs });
        playbackComplete = await page.evaluate(() => window.__priorityMoqMvp?.trace?.state === 'complete');
    } catch (error) {
        exportErrors.push(String(error));
    } finally {
        summary = await page.evaluate(async () => {
            if (!window.__priorityMoqQualityFinish) return null;
            return await window.__priorityMoqQualityFinish();
        }).catch(error => { exportErrors.push(String(error)); return null; });
        frames.sort((a, b) => a.slot - b.slot);
        frames.forEach((frame, index) => { frame.index = index; });
        if (!summary) exportErrors.push('render-loop quality capture hook unavailable');
        const errors = [...(summary?.errors ?? []), ...exportErrors];
        if (frames.some((frame, index) => index && frame.timestampMs <= frames[index - 1].timestampMs))
            errors.push('render-loop frame timestamps are not strictly increasing');
        const manifest = { ...summary, complete: playbackComplete && !!summary?.complete && !errors.length,
            playbackComplete, requestedFps: fps, captureMode: 'render-loop', cameraTimestamps: capture.cameraTimestamps, errors, frames };
        fs.writeFileSync(path.join(directory, 'frames.json'), JSON.stringify(manifest, null, 2) + '\n');
        fs.writeFileSync(path.join(directory, '..', 'render-timing.json'), JSON.stringify({
            complete: playbackComplete, source: 'render-loop',
            samples: (summary?.observations ?? []).map((sample, index) => ({ ...sample,
                observedAtMs: sample.capturedAtMs, renderFrames: index + 1 })),
        }, null, 2) + '\n');
        return manifest;
    }
}
