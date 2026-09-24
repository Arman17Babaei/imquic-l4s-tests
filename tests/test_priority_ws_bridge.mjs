import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";

const bridge = fs.readFileSync(path.join(import.meta.dirname, "../tools/l4s/priority_ws_bridge.mjs"), "utf8");

test("bridge keeps consuming the native sidecar while WebSocket output drains", () => {
    assert.doesNotMatch(bridge, /native\.stdout\.pause\(\)/);
    assert.match(bridge, /const MAX_BROWSER_BUFFER = 128 \* 1024 \* 1024;/);
});
