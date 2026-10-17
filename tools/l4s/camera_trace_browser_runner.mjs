#!/usr/bin/env node
import fs from "node:fs";
import { chromium } from "playwright";

function parseArgs(argv) {
    const options = { headless: false, record: true, width: 960, height: 540, gpu_mode: "hardware" };
    for (let index = 0; index < argv.length; index++) {
        const arg = argv[index];
        if (arg === "--headless") options.headless = true;
        else if (arg === "--no-record") options.record = false;
        else if (["--url", "--output", "--telemetry", "--timeout-ms", "--width", "--height", "--snapshot", "--gpu-mode", "--motion-trace", "--yaw-trace"].includes(arg))
            options[arg.slice(2).replaceAll("-", "_")] = argv[++index];
        else throw new Error(`unknown argument ${arg}`);
    }
    for (const required of ["url", "telemetry", "timeout_ms"])
        if (!options[required]) throw new Error(`--${required.replaceAll("_", "-")} is required`);
    if (options.record && !options.output) throw new Error("--output is required when recording");
    options.timeout_ms = Number(options.timeout_ms);
    options.width = Number(options.width);
    options.height = Number(options.height);
    if (!["hardware", "software"].includes(options.gpu_mode)) throw new Error("--gpu-mode must be hardware or software");
    return options;
}

const options = parseArgs(process.argv.slice(2));
const gpuArgs = options.gpu_mode === "hardware"
    ? ["--enable-gpu", "--use-gl=angle", "--use-angle=gl"]
    : ["--enable-unsafe-swiftshader"];
const browser = await chromium.launch({ headless: options.headless, args: gpuArgs });
const context = await browser.newContext({ viewport: { width: options.width, height: options.height },
    serviceWorkers: "block", acceptDownloads: true });
const page = await context.newPage();
const consoleErrors = [];
page.on("console", message => { if (message.type() === "error") { consoleErrors.push(message.text()); console.error(message.text()); } });
page.on("pageerror", error => { consoleErrors.push(String(error)); console.error(String(error)); });
const downloadPromise = options.record
    ? page.waitForEvent("download", { timeout: options.timeout_ms })
    : Promise.resolve(null);
let failure = null;
let reporting = false;
const progressTimer = setInterval(async () => {
    if (reporting) return;
    reporting = true;
    try {
        const progress = await page.evaluate(() => {
            const state = window.__priorityMoqMvp;
            return state && { received: state.receivedGaussians, applied: state.appliedGaussians,
                frames: state.renderFrames, ready: state.renderReady, renderer: state.rendererState };
        });
        if (progress) console.log(JSON.stringify(progress));
    } catch {}
    finally { reporting = false; }
}, 10000);
try {
    await page.goto(options.url, { waitUntil: "domcontentloaded", timeout: 30000 });
    if (options.snapshot) {
        await page.waitForFunction(() => window.__priorityMoqMvp?.renderReady || window.__priorityMoqMvp?.failure,
            null, { timeout: options.timeout_ms });
        if (await page.evaluate(() => window.__priorityMoqMvp?.failure)) throw new Error("player failed before capture");
        // A settled sort alone is insufficient: compare successive canvas captures
        // to ensure asynchronous accumulator/shader work has reached the screen.
        await page.evaluate(async () => { await window.__priorityMoqFreeze(); });
        const capture = async () => Buffer.from((await page.evaluate(() => document.querySelector("canvas").toDataURL("image/png"))).split(",")[1], "base64");
        let previous = await capture(), stable = false;
        for (let attempt = 0; attempt < 10; attempt++) {
            await page.waitForTimeout(250);
            const next = await capture();
            if (next.equals(previous)) { stable = true; break; }
            previous = next;
        }
        if (!stable) throw new Error("fixed frame did not settle");
        fs.writeFileSync(options.snapshot, previous);
        if (options.motion_trace || options.yaw_trace) {
            const samples = [];
            const normalize = vector => { const norm = Math.hypot(...vector); return vector.map(value => value / norm); };
            const cross = (a, b) => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
            const dot = (a, b) => a.reduce((sum, value, i) => sum + value * b[i], 0);
            const expectedView = frame => {
                if (frame.view_matrix) return frame.view_matrix.map((row, i) => row.map(value => (i < 2 ? -1 : 1) * value));
                const forward = normalize(frame.camera_forward), right = normalize(cross(forward, frame.camera_up));
                const up = cross(right, forward), rows = [right, up, forward.map(value => -value)];
                return [...rows.map(row => [...row, -dot(row, frame.camera_position)]), [0, 0, 0, 1]];
            };
            for (const [name, path] of [["recorded", options.motion_trace], ["yaw", options.yaw_trace]]) {
                if (!path) continue;
                const frames = JSON.parse(fs.readFileSync(path, "utf8"));
                const indices = name === "yaw" ? [0, 150, 250, frames.length - 1]
                    : [0, Math.floor(frames.length / 3), Math.floor(frames.length * 2 / 3), frames.length - 1];
                for (const index of indices) {
                    const frame = frames[index];
                    const pose = await page.evaluate(async ({ frames, timestamp }) =>
                        await window.__priorityMoqSeek(frames, timestamp), { frames, timestamp: frame.timestamp_ms });
                    const expected = expectedView(frame);
                    const error = Math.max(...expected.flatMap((row, r) => row.map((value, c) => Math.abs(value - pose.viewMatrix[c * 4 + r]))));
                    if (error > 1e-3) throw new Error(`${name} pose ${index} view error ${error}`);
                    fs.writeFileSync(`${options.snapshot}.${name}-${index}.png`, await capture());
                    samples.push({ sequence: name, index, timestampMs: frame.timestamp_ms, viewMaxAbsError: error, pose });
                }
            }
            fs.writeFileSync(`${options.snapshot}.camera-validation.json`, JSON.stringify({ status: "PASS", samples }, null, 2) + "\n");
        }
        await page.evaluate(async () => { if (window.__priorityMoqClose) await window.__priorityMoqClose(); });
    } else {
        await page.waitForFunction(() => ["complete", "failed"].includes(window.__priorityMoqMvp?.trace?.state),
            null, { timeout: options.timeout_ms });
    }
    const download = await downloadPromise;
    if (download) await download.saveAs(options.output);
} catch (error) {
    failure = String(error);
    if (options.snapshot) await page.locator("canvas").first().screenshot({ path: `${options.snapshot}.failed.png`, timeout: 5000 }).catch(() => {});
}
const telemetry = await page.evaluate(() => window.__priorityMoqMvp ?? null).catch(() => null);
const graphics = await page.evaluate(() => {
    const canvas = document.querySelector("canvas");
    const gl = canvas?.getContext("webgl2");
    const debug = gl?.getExtension("WEBGL_debug_renderer_info");
    return gl && { vendor: gl.getParameter(debug ? debug.UNMASKED_VENDOR_WEBGL : gl.VENDOR),
        renderer: gl.getParameter(debug ? debug.UNMASKED_RENDERER_WEBGL : gl.RENDERER) };
}).catch(() => null);
clearInterval(progressTimer);
const result = { ...telemetry, browserVersion: browser.version(), consoleErrors,
    failure: telemetry?.failure ?? failure, runnerFailure: failure,
    browserContext: { fresh: true, serviceWorkers: "block", viewport: [options.width, options.height],
        gpuMode: options.gpu_mode, graphics } };
fs.writeFileSync(options.telemetry, `${JSON.stringify(result, null, 2)}\n`);
await context.close();
await browser.close();
if (failure || result.failure || (options.snapshot ? !result.renderReady : result.trace?.state !== "complete") ||
    (options.record && !fs.existsSync(options.output))) process.exitCode = 1;
