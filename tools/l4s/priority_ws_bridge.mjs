#!/usr/bin/env node
import crypto from "node:crypto";
import http from "node:http";
import { spawn } from "node:child_process";

const MAX_MESSAGE = 1024 * 1024;
const MAX_BROWSER_BUFFER = 128 * 1024 * 1024;

function argsOf(argv) {
    const out = { listen: 8787, relayHost: "127.0.0.1", relayPort: 4443, native: "./priority_moq_sidecar", origins: [] };
    for (let i = 0; i < argv.length; i++) {
        if (argv[i] === "--listen") out.listen = Number(argv[++i]);
        else if (argv[i] === "--relay-host") out.relayHost = argv[++i];
        else if (argv[i] === "--relay-port") out.relayPort = Number(argv[++i]);
        else if (argv[i] === "--native") out.native = argv[++i];
        else if (argv[i] === "--origin") out.origins.push(argv[++i]);
        else throw new Error(`unknown argument ${argv[i]}`);
    }
    if (!out.origins.length) out.origins = ["http://127.0.0.1:5173", "http://localhost:5173"];
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
const native = spawn(options.native, [options.relayHost, String(options.relayPort)], { stdio: ["pipe", "pipe", "inherit"] });
let client = null, nativeReady = false, nativeBytes = Buffer.alloc(0), browserBytes = Buffer.alloc(0), pending = [];

function sendBrowser(opcode, payload) {
    if (!client || client.destroyed) return;
    if (client.writableLength + payload.length > MAX_BROWSER_BUFFER) {
        close(client, 1011, "browser output buffer exceeded");
        native.kill("SIGTERM");
        return;
    }
    client.write(websocketFrame(opcode, payload));
}

function handleNativeFrame(headerBytes, payload) {
    let header;
    try { header = JSON.parse(headerBytes.toString("utf8")); }
    catch { native.kill("SIGTERM"); return; }
    if (header.wire === "control") {
        if (header.message?.type === "ready") {
            nativeReady = true;
            for (const command of pending) native.stdin.write(`${command}\n`);
            pending = [];
        }
        sendBrowser(1, Buffer.from(JSON.stringify(header.message)));
    }
}

native.stdout.on("data", chunk => {
    nativeBytes = Buffer.concat([nativeBytes, chunk]);
    while (nativeBytes.length >= 4) {
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
        if (header.wire === "control") handleNativeFrame(headerBytes, payload);
        else {
            const envelope = Buffer.alloc(4 + headerLength + payloadLength);
            envelope.writeUInt32BE(headerLength); headerBytes.copy(envelope, 4); payload.copy(envelope, 4 + headerLength);
            sendBrowser(2, envelope);
        }
    }
});
native.on("exit", code => {
    if (client) close(client, 1011, `native sidecar exited (${code})`);
    server.close(() => process.exit(code ?? 1));
});

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
        if (command.type !== "open" && command.type !== "priorities") { close(client, 1008, "unsupported message type"); return; }
        const line = JSON.stringify(command);
        if (nativeReady) native.stdin.write(`${line}\n`); else pending.push(line);
    }
}

server = http.createServer((_request, response) => { response.writeHead(426).end("WebSocket required\n"); });
server.on("upgrade", (request, socket) => {
    const remote = socket.remoteAddress?.replace(/^::ffff:/, "");
    const protocols = (request.headers["sec-websocket-protocol"] ?? "").split(",").map(value => value.trim());
    if ((remote !== "127.0.0.1" && remote !== "::1") || !options.origins.includes(request.headers.origin ?? "") ||
        request.headers.upgrade?.toLowerCase() !== "websocket" || !protocols.includes("3dgs-moqt-v1") || client) {
        socket.end("HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n"); return;
    }
    const key = request.headers["sec-websocket-key"];
    if (typeof key !== "string") { socket.destroy(); return; }
    const accept = crypto.createHash("sha1").update(`${key}258EAFA5-E914-47DA-95CA-C5AB0DC85B11`).digest("base64");
    socket.write(`HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: ${accept}\r\nSec-WebSocket-Protocol: 3dgs-moqt-v1\r\n\r\n`);
    client = socket;
    socket.on("data", handleBrowserData);
    socket.on("close", () => { client = null; server.close(); native.kill("SIGTERM"); });
});
server.listen(options.listen, "127.0.0.1", () => console.error(`3DGS MOQT bridge ws://127.0.0.1:${options.listen}`));
process.on("SIGINT", () => { server.close(); native.kill("SIGTERM"); });
