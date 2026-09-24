#!/usr/bin/env node
import fs from "node:fs";
import { chromium } from "playwright";

const [url, output, expectedMeshesText, timeoutText] = process.argv.slice(2);
if (!url || !output || !expectedMeshesText) throw new Error("usage: priority_moq_browser_probe.mjs URL OUTPUT EXPECTED_MESHES [TIMEOUT_MS]");
const expectedMeshes = Number(expectedMeshesText);
const timeoutMs = timeoutText === undefined ? 180000 : Number(timeoutText);
if (!Number.isSafeInteger(expectedMeshes) || expectedMeshes < 0) throw new Error("EXPECTED_MESHES must be a non-negative integer");
if (!Number.isSafeInteger(timeoutMs) || timeoutMs <= 0) throw new Error("TIMEOUT_MS must be a positive integer");
const browser = await chromium.launch({ headless: true });
const page = await browser.newPage();
const consoleErrors = [];
page.on("console", message => { if (message.type() === "error") consoleErrors.push(message.text()); });
await page.goto(url);
let failure = null;
try {
    await page.waitForFunction(() => window.__priorityMoqMvp?.events?.some(event => event.type === "priorities-sent"), null, { timeout: timeoutMs });
    await page.evaluate(() => window.__priorityMoqTurn());
    await page.waitForFunction(expected => {
        const state = window.__priorityMoqMvp;
        return state && state.appliedGaussians === state.sourceGaussians && state.renderedMeshes === expected;
    }, expectedMeshes, { timeout: timeoutMs });
} catch (error) {
    failure = String(error);
}
const telemetry = await page.evaluate(() => window.__priorityMoqMvp);
telemetry.consoleErrors = consoleErrors;
telemetry.failure = failure;
telemetry.success = !failure && telemetry.receivedGaussians === telemetry.sourceGaussians && telemetry.appliedGaussians === telemetry.sourceGaussians &&
    telemetry.duplicates === 0 && telemetry.rejected === 0 && telemetry.renderedMeshes === expectedMeshes;
fs.writeFileSync(output, `${JSON.stringify(telemetry, null, 2)}\n`);
try {
    await page.screenshot({ path: output.replace(/\.json$/, ".png"), timeout: 5000 });
} catch (error) { console.error(String(error)); }
await browser.close();
if (!telemetry.success) process.exitCode = 1;
