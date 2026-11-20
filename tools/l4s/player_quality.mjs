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

// The last camera keyframe defines duration; the video grid excludes that endpoint.
export function createTraceFrameGrid(trace, fps = 10) {
    if (!Number.isFinite(fps) || fps <= 0) throw new Error('reference FPS must be finite and positive');
    if (!Array.isArray(trace) || trace.length < 2 || trace[0]?.timestamp_ms !== 0 ||
        trace.some((frame, index) => !Number.isFinite(frame?.timestamp_ms) ||
            (index > 0 && frame.timestamp_ms <= trace[index - 1].timestamp_ms)))
        throw new Error('reference trace requires common zero and strictly increasing finite timestamps');
    const durationMs = trace.at(-1).timestamp_ms;
    if (!(durationMs > 0)) throw new Error('reference duration must be positive');
    const count = Math.ceil(durationMs * fps / 1000);
    if (!Number.isSafeInteger(count) || count > 10000000) throw new Error('reference grid is too large');
    return Array.from({ length: count }, (_, index) => ({ index,
        nominalTimestampMs: index * 1000 / fps, timestampMs: index * 1000 / fps }));
}

export function referenceSampleReady(state) {
    const renderer = state.rendererState;
    return Boolean(state.renderReady || (renderer &&
        state.sourceGaussians > 0 && state.appliedGaussians === state.sourceGaussians &&
        renderer.activeGaussians === state.sourceGaussians &&
        !renderer.sorting && !renderer.pendingSort));
}

export async function renderReference(page, candidateManifest, tracePath, directory, timeoutMs, options = {}) {
    const trace = JSON.parse(fs.readFileSync(tracePath, 'utf8'));
    const fps = options.fps ?? 10;
    const gridMode = options.traceGrid === true;
    const candidate = gridMode ? { frames: createTraceFrameGrid(trace, fps) }
        : JSON.parse(fs.readFileSync(candidateManifest, 'utf8'));
    const frameStart = options.frameStart ?? 0, frameEnd = options.frameEnd ?? candidate.frames.length;
    if (!Number.isInteger(frameStart) || !Number.isInteger(frameEnd) || frameStart < 0 ||
        frameEnd > candidate.frames.length || frameEnd <= frameStart)
        throw new Error('invalid reference frame range');
    if (gridMode) candidate.frames = candidate.frames.slice(frameStart, frameEnd);
    if (!gridMode && ((!candidate.complete && !options.allowPartial) || !candidate.frames?.length))
        throw new Error('candidate frame capture is incomplete');
    if (!gridMode && candidate.frames.some(frame => !Number.isFinite(frame.timestampMs) || !frame.cameraPose))
        throw new Error('candidate frames require finite timestamps and camera poses');
    fs.mkdirSync(directory);
    await page.waitForFunction(() => {
        const state = window.__priorityMoqMvp;
        const renderer = state?.rendererState;
        return state?.failure || state?.renderReady || (renderer && state.sourceGaussians > 0 &&
            state.appliedGaussians === state.sourceGaussians && renderer.activeGaussians === state.sourceGaussians &&
            !renderer.sorting && !renderer.pendingSort);
    },
        null, { timeout: timeoutMs });
    const frames = [];
    let complete = false;
    try {
        for (const candidateFrame of candidate.frames) {
            const frameStarted = Date.now();
            if (candidateFrame.index % 20 === 0)
                console.log(`REFERENCE frame ${candidateFrame.index}/${candidate.frames.length}`);
            const deadline = Date.now() + timeoutMs;
            let previous = null, settled = null;
            let seek = true;
            let probes = 0;
            while (Date.now() < deadline) {
                if (seek && candidateFrame.index === 0) console.log('REFERENCE first seek started');
                const sample = await page.evaluate(async ({ trace, timestampMs, seek, timeoutMs }) => {
                    if (window.__priorityMoqMvp.failure) throw new Error(window.__priorityMoqMvp.failure);
                    if (seek) {
                        const pending = window.__priorityMoqSeek(trace, timestampMs);
                        // Seeking stops requestAnimationFrame. Submit pending
                        // software WebGL work while its async readback resolves.
                        const gl = document.querySelector('canvas').getContext('webgl2');
                        gl?.flush();
                        let timer;
                        try { await Promise.race([pending, new Promise((_, reject) => {
                            timer = setTimeout(() => reject(new Error(`reference seek timed out at ${timestampMs} ms`)), timeoutMs);
                        })]); }
                        finally { clearTimeout(timer); }
                    }
                    // The capture hook already serializes the canvas. Reuse
                    // that PNG rather than encoding it a second time below.
                    const captured = seek ? null : await window.__priorityMoqCapture();
                    const state = window.__priorityMoqMvp;
                    // renderReady includes a wall-clock debounce intended for live
                    // animation. A seek stops that animation. Check the underlying
                    // sort state as well as identical pixels instead of waiting
                    // for the live debounce to advance after every seek.
                    return { ready: state.renderReady, rendererState: state.rendererState,
                        sourceGaussians: state.sourceGaussians, timestampMs, cameraPose: state.cameraPose,
                        appliedGaussians: state.appliedGaussians,
                        image: captured?.image ?? document.querySelector('canvas').toDataURL('image/png') };
                }, { trace: seek ? trace : null, timestampMs: candidateFrame.timestampMs, seek, timeoutMs });
                if (seek && candidateFrame.index === 0) console.log('REFERENCE first seek returned');
                if (++probes % 100 === 0) console.log(JSON.stringify({
                    referenceFrame: candidateFrame.index, probes, ready: sample.ready,
                    renderer: sample.rendererState, samePixels: sample.image === previous,
                }));
                // Re-seeking forces Spark to regenerate and sort even for the
                // same pose, invalidating readiness on every stability probe.
                seek = false;
                if (referenceSampleReady({ ...sample, renderReady: sample.ready }) && sample.image === previous) { settled = sample; break; }
                previous = sample.image;
                await page.waitForTimeout(50);
            }
            if (gridMode) console.log(JSON.stringify({ referenceFrame: candidateFrame.index,
                timestampMs: candidateFrame.timestampMs, frameWallMs: Date.now() - frameStarted, probes }));
            if (!settled) throw new Error(`reference frame ${candidateFrame.index} did not settle`);
            if (!gridMode) {
                const actual = settled.cameraPose, expected = candidateFrame.cameraPose;
                for (const field of ['viewMatrix', 'position', 'up']) {
                    if (!actual[field] || !expected[field] || actual[field].length !== expected[field].length ||
                        actual[field].some((value, i) => !Number.isFinite(value) || !Number.isFinite(expected[field][i]) ||
                            Math.abs(value - expected[field][i]) > 1e-4))
                        throw new Error(`reference camera ${field} mismatch at frame ${candidateFrame.index}`);
                }
                for (const field of ['fov', 'aspect'])
                    if (!Number.isFinite(actual[field]) || !Number.isFinite(expected[field]) ||
                        Math.abs(actual[field] - expected[field]) > 1e-6)
                        throw new Error(`reference camera ${field} mismatch at frame ${candidateFrame.index}`);
            }
            if (gridMode) settled.nominalTimestampMs = candidateFrame.nominalTimestampMs;
            delete settled.ready;
            delete settled.rendererState;
            delete settled.sourceGaussians;
            writeFrame(directory, frames, settled);
        }
        complete = true;
    } finally {
        fs.writeFileSync(path.join(directory, 'frames.json'), JSON.stringify({
            complete, ...(gridMode ? {
                captureMode: 'trace-grid-reference', requestedFps: fps, expectedFrames: candidate.frames.length,
                frameStart, frameEnd,
                tracePath: path.resolve(tracePath), traceOriginMs: 0,
                durationMs: trace.at(-1).timestamp_ms, gridInterval: '[0,durationMs)',
                timestampSource: 'camera-trace-common-zero',
            } : { candidateManifest: path.resolve(candidateManifest) }), frames,
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
        return manifest;
    }
}
