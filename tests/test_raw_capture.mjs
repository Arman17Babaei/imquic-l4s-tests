import test from 'node:test';
import assert from 'node:assert/strict';
import { inflateSync } from 'node:zlib';
import { captureImageBytes } from '../tools/l4s/raw_capture.mjs';

test('raw capture PNG is lossless and flips WebGL rows', () => {
    const pixels = Buffer.from([255, 0, 0, 255, 0, 255, 0, 255]);
    const png = captureImageBytes(`data:application/x-rgba;width=1;height=2;base64,${pixels.toString('base64')}`);
    let offset = 8, rows;
    while (offset < png.length) {
        const length = png.readUInt32BE(offset), type = png.toString('ascii', offset + 4, offset + 8);
        if (type === 'IDAT') rows = inflateSync(png.subarray(offset + 8, offset + 8 + length));
        offset += length + 12;
    }
    assert.deepEqual(rows, Buffer.from([0, 0, 255, 0, 255, 0, 255, 0, 0, 255]));
});

test('raw capture rejects invalid dimensions and preserves PNG input', () => {
    assert.throws(() => captureImageBytes('data:application/x-rgba;width=2;height=2;base64,AA=='), /size mismatch/);
    assert.deepEqual(captureImageBytes('data:image/png;base64,cG5n'), Buffer.from('png'));
});
