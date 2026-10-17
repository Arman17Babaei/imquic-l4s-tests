#!/usr/bin/env node
import crypto from "node:crypto";
import http from "node:http";
import { spawn } from "node:child_process";

const MAX_MESSAGE = 1024 * 1024;
const MAX_BROWSER_BUFFER = 128 * 1024 * 1024;

function argsOf(argv) {
    const out = { listen: 8787, relayHost: "127.0.0.1", relayPort: 4443,
        native: "./priority_moq_sidecar", transportMode: "reno", objectWindow: 16, origins: [] };
    for (let i = 0; i < argv.length; i++) {
        if (argv[i] === "--listen") out.listen = Number(argv[++i]);
        else if (argv[i] === "--relay-host") out.relayHost = argv[++i];
        else if (argv[i] === "--relay-port") out.relayPort = Number(argv[++i]);
        else if (argv[i] === "--native") out.native = argv[++i];
        else if (argv[i] === "--transport-mode") out.transportMode = argv[++i];
        else if (argv[i] === "--congestion-options") out.congestionOptions = argv[++i];
        else if (argv[i] === "--origin") out.origins.push(argv[++i]);
        else if (argv[i] === "--object-window") out.objectWindow = Number(argv[++i]);
        else if (argv[i] === "--lazy-native") out.lazyNative = true;
        else if (argv[i] === "--persistent") { out.persistent = true; out.lazyNative = true; }
        else throw new Error(`unknown argument ${argv[i]}`);
    }
    if (!out.origins.length) out.origins = ["http://127.0.0.1:5173", "http://localhost:5173"];
    if (out.transportMode !== "reno" && out.transportMode !== "prague") throw new Error("invalid transport mode");
    if (!Number.isInteger(out.objectWindow) || out.objectWindow < 1 || out.objectWindow > 64) throw new Error("invalid object window");
    return out;
}

function websocketFrame(opcode, payload) {
    const length = payload.length;
    const prefix = length < 126 ? Buffer.from([0x80 | opcode, length])
        : length <= 0xffff ? Buffer.from([0x80 | opcode, 126, length >> 8, length & 255])
            : (() => { const b = Buffer.alloc(10); b[0] = 0x80 | opcode; b[1] = 127; b.writeBigUInt64BE(BigInt(length), 2); return b; })();
    return Buffer.concat([prefix, payload]);
}

function close(socket, code, reason) {
    const text = Buffer.from(reason);
    const payload = Buffer.alloc(2 + Math.min(text.length, 123));
    payload.writeUInt16BE(code); text.copy(payload, 2, 0, payload.length - 2);
    socket.end(websocketFrame(8, payload));
}

const options = argsOf(process.argv.slice(2));
let server;
let native = null;
let client = null, nativeReady = false, nativeBytes = Buffer.alloc(0), browserBytes = Buffer.alloc(0);
let gracefulClosing = false;
let auditComplete = null;
let auditExitTimer = null;
let nativeInputBlocked = false;
let nativeCommands = [];
let outstandingObjects = 0;
let outstandingPayloads = [];
let peakOutstandingObjects = 0, peakOutstandingBytes = 0, peakBrowserWriteBytes = 0, peakNativeBytes = 0;
let windowFullSince = null, windowFullMs = 0;
function bufferMetrics() {
    return { type: "adapter-buffers", client_started: Boolean(native), object_window: options.objectWindow,
        outstanding_objects: outstandingObjects, outstanding_payload_bytes: outstandingPayloads.reduce((a, b) => a + b, 0),
        native_parser_bytes: nativeBytes.length, native_readable_bytes: native?.stdout.readableLength ?? 0,
        native_paused: native?.stdout.isPaused() ?? false, browser_write_bytes: client?.writableLength ?? 0,
        peak_outstanding_objects: peakOutstandingObjects, peak_outstanding_bytes: peakOutstandingBytes,
        peak_browser_write_bytes: peakBrowserWriteBytes, peak_native_parser_bytes: peakNativeBytes,
        window_full_ms: windowFullMs + (windowFullSince === null ? 0 : performance.now() - windowFullSince) };
}
function updateBufferPeaks() {
    peakOutstandingObjects = Math.max(peakOutstandingObjects, outstandingObjects);
    peakOutstandingBytes = Math.max(peakOutstandingBytes, outstandingPayloads.reduce((a, b) => a + b, 0));
    peakBrowserWriteBytes = Math.max(peakBrowserWriteBytes, client?.writableLength ?? 0);
    peakNativeBytes = Math.max(peakNativeBytes, nativeBytes.length);
    if (outstandingObjects >= options.objectWindow && windowFullSince === null) windowFullSince = performance.now();
    if (outstandingObjects < options.objectWindow && windowFullSince !== null) {
        windowFullMs += performance.now() - windowFullSince; windowFullSince = null;
    }
}

function pumpNativeCommands() {
    if (!nativeReady || nativeInputBlocked) return;
    while (nativeCommands.length) {
        const command = nativeCommands.shift();
        if (!native.stdin.write(`${command.line}\n`)) {
            nativeInputBlocked = true;
            return;
        }
    }
}

function queueNativeCommand(command, line) {
    if (command.type === "close") {
        const queuedPriorities = nativeCommands.filter(item => item.type === "priorities").length;
        nativeCommands = nativeCommands.filter(item => item.type !== "priorities");
        console.error(`graceful close queued; discarded ${queuedPriorities} undispatched priority commands`);
    }
    nativeCommands.push({ type: command.type, line });
    pumpNativeCommands();
}

function sendBrowser(opcode, payload) {
    if (!client || client.destroyed || client.writableEnded) return true;
    if (client.writableLength + payload.length > MAX_BROWSER_BUFFER) {
        close(client, 1011, "browser output buffer exceeded");
        native.kill("SIGTERM");
        return false;
    }
    const accepted = client.write(websocketFrame(opcode, payload));
    updateBufferPeaks();
    return accepted;
}

function handleNativeFrame(headerBytes, payload) {
    let header;
    try { header = JSON.parse(headerBytes.toString("utf8")); }
    catch { native.kill("SIGTERM"); return; }
    if (header.wire === "control") {
        if (header.message?.type === "audit-complete") {
            auditComplete = header.message;
            console.error("native audit complete");
        }
        if (header.message?.type === "ready") {
            nativeReady = true;
            pumpNativeCommands();
        }
        sendBrowser(1, Buffer.from(JSON.stringify(header.message)));
    }
}

let parsingNative = false;

function processNativeBytes() {
    if (parsingNative) return;
    parsingNative = true;
    try {
        while (nativeBytes.length >= 4) {
            if (!gracefulClosing && outstandingObjects >= options.objectWindow) { native.stdout.pause(); return; }
            const headerLength = nativeBytes.readUInt32BE(0);
            if (headerLength > 65536) { native.kill("SIGTERM"); return; }
            if (nativeBytes.length < 4 + headerLength) return;
            let header;
            try { header = JSON.parse(nativeBytes.subarray(4, 4 + headerLength).toString("utf8")); }
            catch { native.kill("SIGTERM"); return; }
            const payloadLength = Number(header.payload_bytes ?? 0);
            if (!Number.isSafeInteger(payloadLength) || payloadLength < 0 || payloadLength > MAX_MESSAGE || nativeBytes.length < 4 + headerLength + payloadLength) return;
            const headerBytes = nativeBytes.subarray(4, 4 + headerLength);
            const payload = nativeBytes.subarray(4 + headerLength, 4 + headerLength + payloadLength);
            nativeBytes = nativeBytes.subarray(4 + headerLength + payloadLength);
            if (header.wire === "control") {
                handleNativeFrame(headerBytes, payload);
            } else if (!gracefulClosing) {
                const envelope = Buffer.alloc(4 + headerLength + payloadLength);
                envelope.writeUInt32BE(headerLength); headerBytes.copy(envelope, 4); payload.copy(envelope, 4 + headerLength);
                sendBrowser(2, envelope);
                outstandingObjects++; outstandingPayloads.push(payloadLength); updateBufferPeaks();
                if (outstandingObjects >= options.objectWindow) { native.stdout.pause(); return; }
            }
        }
    } finally {
        parsingNative = false;
    }
}

function startNative() {
    nativeReady = false; nativeBytes = Buffer.alloc(0); browserBytes = Buffer.alloc(0);
    gracefulClosing = false; auditComplete = null; nativeInputBlocked = false;
    nativeCommands = []; outstandingObjects = 0; outstandingPayloads = [];
    peakOutstandingObjects = peakOutstandingBytes = peakBrowserWriteBytes = peakNativeBytes = 0;
    windowFullSince = null; windowFullMs = 0;
    const child = spawn(options.native, [options.relayHost, String(options.relayPort), options.transportMode,
        ...(options.congestionOptions ? [options.congestionOptions] : [])],
        { stdio: ["pipe", "pipe", "inherit"] });
    native = child;
    child.stdin.on("drain", () => { nativeInputBlocked = false; pumpNativeCommands(); });
    child.on("error", error => {
        sendBrowser(1, Buffer.from(JSON.stringify({ type: "error", message: `native client failed: ${error.message}` })));
        native = null; nativeReady = false;
        if (client) close(client, 1011, "native client failed");
    });
    child.stdout.on("data", chunk => {
        nativeBytes = Buffer.concat([nativeBytes, chunk]);
        updateBufferPeaks();
        processNativeBytes();
    });
    // 'close' follows stdout draining; 'exit' may precede its final audit frame.
    child.on("close", code => {
        if (native !== child) return;
        if (options.persistent) {
            native = null; nativeReady = false;
            if (client && !client.destroyed && !client.writableEnded)
                close(client, gracefulClosing && auditComplete ? 1000 : 1011,
                    gracefulClosing && auditComplete ? "audit complete" : `native sidecar exited (${code})`);
            return;
        }
        if (gracefulClosing && auditComplete) {
            /* Keep the independent audit endpoint alive long enough for a browser whose
             * WebSocket event queue is saturated to observe native completion. */
            auditExitTimer = setTimeout(() => {
                client?.destroy();
                server.close(() => process.exit(code ?? 1));
            }, 30000);
            return;
        }
        if (client && !client.destroyed && !client.writableEnded)
            close(client, gracefulClosing ? 1000 : 1011, gracefulClosing ? "audit complete" : `native sidecar exited (${code})`);
        server.close(() => process.exit(code ?? 1));
    });
}
if (!options.lazyNative) startNative();

function handleBrowserData(chunk) {
    browserBytes = Buffer.concat([browserBytes, chunk]);
    while (browserBytes.length >= 2) {
        const first = browserBytes[0], second = browserBytes[1];
        const opcode = first & 0x0f;
        if (!(first & 0x80) || !(second & 0x80)) { close(client, 1002, "fragmented or unmasked frame"); return; }
        let offset = 2, length = second & 0x7f;
        if (length === 126) { if (browserBytes.length < 4) return; length = browserBytes.readUInt16BE(2); offset = 4; }
        else if (length === 127) {
            if (browserBytes.length < 10) return;
            const big = browserBytes.readBigUInt64BE(2);
            if (big > BigInt(MAX_MESSAGE)) { close(client, 1009, "message too large"); return; }
            length = Number(big); offset = 10;
        }
        if (length > MAX_MESSAGE) { close(client, 1009, "message too large"); return; }
        if (browserBytes.length < offset + 4 + length) return;
        const mask = browserBytes.subarray(offset, offset + 4), payload = Buffer.from(browserBytes.subarray(offset + 4, offset + 4 + length));
        for (let i = 0; i < payload.length; i++) payload[i] ^= mask[i & 3];
        browserBytes = browserBytes.subarray(offset + 4 + length);
        if (opcode === 8) { client.end(websocketFrame(8, payload)); return; }
        if (opcode === 9) { client.write(websocketFrame(10, payload)); continue; }
        if (opcode !== 1) { close(client, 1003, "control messages must be text"); return; }
        let command;
        try { command = JSON.parse(payload.toString("utf8")); } catch { close(client, 1007, "invalid JSON"); return; }
        if (command.type === "object-ack") {
            outstandingObjects = Math.max(0, outstandingObjects - 1);
            outstandingPayloads.shift(); updateBufferPeaks();
            processNativeBytes();
            if (outstandingObjects < options.objectWindow) native?.stdout.resume();
            continue;
        }
        if (command.type !== "open" && command.type !== "priorities" && command.type !== "close") { close(client, 1008, "unsupported message type"); return; }
        if (command.type === "close") {
            gracefulClosing = true;
            outstandingObjects = 0; outstandingPayloads = []; updateBufferPeaks();
            /* Stop adding object frames behind the audit response. Backpressure keeps
             * the pre-existing socket queue small; resuming here drains the native
             * control stream while post-close object frames are discarded. */
            native?.stdout.resume();
            processNativeBytes();
        }
        const line = JSON.stringify(command);
        queueNativeCommand(command, line);
    }
}

server = http.createServer((request, response) => {
    const remote = request.socket.remoteAddress?.replace(/^::ffff:/, "");
    const origin = request.headers.origin ?? "";
    if (request.method === "GET" && request.url === "/buffers" &&
            (remote === "127.0.0.1" || remote === "::1") && options.origins.includes(origin)) {
        response.writeHead(200, { "Access-Control-Allow-Origin": origin, "Cache-Control": "no-store",
            "Content-Type": "application/json" }).end(JSON.stringify(bufferMetrics()));
        return;
    }
    if (request.method === "GET" && request.url === "/audit" &&
            (remote === "127.0.0.1" || remote === "::1") && options.origins.includes(origin)) {
        const body = JSON.stringify({ ...(auditComplete ?? { type: "audit-pending" }), client_started: Boolean(native) });
        response.writeHead(auditComplete ? 200 : 202, {
            "Access-Control-Allow-Origin": origin,
            "Cache-Control": "no-store",
            "Content-Type": "application/json",
        }).end(body);
        if (auditComplete && auditExitTimer) {
            clearTimeout(auditExitTimer);
            auditExitTimer = null;
        }
        return;
    }
    response.writeHead(426, { "Cache-Control": "no-store" }).end("WebSocket required\n");
});
server.on("upgrade", (request, socket) => {
    const remote = socket.remoteAddress?.replace(/^::ffff:/, "");
    const protocols = (request.headers["sec-websocket-protocol"] ?? "").split(",").map(value => value.trim());
    if ((remote !== "127.0.0.1" && remote !== "::1") || !options.origins.includes(request.headers.origin ?? "") ||
        request.headers.upgrade?.toLowerCase() !== "websocket" || !protocols.includes("3dgs-moqt-v1") || client || (options.lazyNative && native)) {
        socket.end("HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n"); return;
    }
    const key = request.headers["sec-websocket-key"];
    if (typeof key !== "string") { socket.destroy(); return; }
    const accept = crypto.createHash("sha1").update(`${key}258EAFA5-E914-47DA-95CA-C5AB0DC85B11`).digest("base64");
    socket.write(`HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: ${accept}\r\nSec-WebSocket-Protocol: 3dgs-moqt-v1\r\n\r\n`);
    client = socket;
    if (options.lazyNative) startNative();
    socket.on("drain", () => {
        processNativeBytes();
        if (outstandingObjects < options.objectWindow) native?.stdout.resume();
    });
    socket.on("data", handleBrowserData);
    socket.on("error", () => { if (!gracefulClosing) { native?.stdin.end(); native?.kill("SIGTERM"); } });
    socket.on("close", () => {
        client = null;
        if (!options.persistent) server.close();
        if (!gracefulClosing) { native?.stdin.end(); native?.kill("SIGTERM"); }
    });
});
server.listen(options.listen, "127.0.0.1", () => console.error(`3DGS MOQT bridge ws://127.0.0.1:${options.listen}`));
function shutdown() {
    if (auditExitTimer) clearTimeout(auditExitTimer);
    client?.destroy(); server.close(); native?.stdin.end(); native?.kill("SIGTERM");
}
process.on("SIGINT", shutdown);
process.on("SIGTERM", shutdown);
