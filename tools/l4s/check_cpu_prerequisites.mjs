#!/usr/bin/env node
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';

const root = fileURLToPath(new URL('../../', import.meta.url));
function requireFile(relative, instruction) {
    if (!fs.existsSync(path.join(root, relative))) throw new Error(instruction);
}
try {
    if (spawnSync('ffmpeg', ['-version'], { stdio: 'ignore' }).status !== 0)
        throw new Error('Install FFmpeg for video and CPU image-similarity checks');
    requireFile('node_modules/playwright/package.json', 'Install local browser dependencies with: npm ci');
    requireFile('deps/gaussian-player/node_modules/typescript/package.json',
        'Install player dependencies with: npm --prefix deps/gaussian-player ci');
    const { chromium } = await import('playwright');
    if (!fs.existsSync(chromium.executablePath()))
        throw new Error('Install Chromium with: npx --no-install playwright install chromium');
    const browser = await chromium.launch({ headless: true,
        args: ['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'] });
    await browser.close();
} catch (error) {
    console.error(`CPU check prerequisites: ${error.message}`);
    process.exitCode = 1;
}
