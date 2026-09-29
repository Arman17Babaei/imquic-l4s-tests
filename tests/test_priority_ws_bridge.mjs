import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";

const bridge = fs.readFileSync(path.join(import.meta.dirname, "../tools/l4s/priority_ws_bridge.mjs"), "utf8");

test("bridge keeps consuming the native sidecar while WebSocket output drains", () => {
    assert.doesNotMatch(bridge, /native\.stdout\.pause\(\)/);
    assert.match(bridge, /const MAX_BROWSER_BUFFER = 128 \* 1024 \* 1024;/);
});

test("bridge passes the selected transport mode to the native sidecar", () => {
    assert.match(bridge, /--transport-mode/);
    assert.match(bridge, /options\.transportMode/);
});

test("bridge supports graceful audit shutdown without writing to an ended socket", () => {
    assert.match(bridge, /command\.type !== "close"/);
    assert.match(bridge, /client\.writableEnded/);
    assert.match(bridge, /gracefulClosing/);
});
