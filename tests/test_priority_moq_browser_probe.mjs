import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";

const probe = fs.readFileSync(path.join(import.meta.dirname, "../tools/l4s/priority_moq_browser_probe.mjs"), "utf8");

test("optional screenshot failures do not invalidate recorded correctness telemetry", () => {
    assert.match(probe, /try \{\s*await page\.screenshot/s);
    assert.match(probe, /catch \(error\) \{ console\.error\(String\(error\)\); \}/);
});
