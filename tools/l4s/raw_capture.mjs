import { deflateSync } from 'node:zlib';

function crc32(bytes) {
    let crc = 0xffffffff;
    for (const byte of bytes) {
        crc ^= byte;
        for (let bit = 0; bit < 8; bit++) crc = (crc >>> 1) ^ ((crc & 1) ? 0xedb88320 : 0);
    }
    return (crc ^ 0xffffffff) >>> 0;
}

function chunk(type, data) {
    const name = Buffer.from(type), size = Buffer.alloc(4), crc = Buffer.alloc(4);
    size.writeUInt32BE(data.length);
    crc.writeUInt32BE(crc32(Buffer.concat([name, data])));
    return Buffer.concat([size, name, data, crc]);
}

export function captureImageBytes(image) {
    if (!image.startsWith('data:application/x-rgba;')) return Buffer.from(image.split(',')[1], 'base64');
    const match = /^data:application\/x-rgba;width=(\d+);height=(\d+);base64,(.*)$/.exec(image);
    if (!match) throw new Error('invalid raw capture');
    const width = Number(match[1]), height = Number(match[2]), pixels = Buffer.from(match[3], 'base64');
    if (width * height * 4 !== pixels.length) throw new Error('raw capture size mismatch');
    const rows = Buffer.alloc((width * 4 + 1) * height);
    for (let y = 0; y < height; y++) pixels.copy(rows, y * (width * 4 + 1) + 1,
        (height - y - 1) * width * 4, (height - y) * width * 4);
    const header = Buffer.alloc(13);
    header.writeUInt32BE(width); header.writeUInt32BE(height, 4); header[8] = 8; header[9] = 6;
    return Buffer.concat([Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]), chunk('IHDR', header),
        chunk('IDAT', deflateSync(rows, { level: 1 })), chunk('IEND', Buffer.alloc(0))]);
}

export function installRawCapture() {
    const original = HTMLCanvasElement.prototype.toDataURL;
    HTMLCanvasElement.prototype.toDataURL = function(type, ...args) {
        if (type !== 'image/png') return original.call(this, type, ...args);
        const gl = this.getContext('webgl2');
        if (!gl) return original.call(this, type, ...args);
        const pixels = new Uint8Array(this.width * this.height * 4);
        const framebuffer = gl.getParameter(gl.READ_FRAMEBUFFER_BINDING);
        gl.bindFramebuffer(gl.READ_FRAMEBUFFER, null);
        try { gl.readPixels(0, 0, this.width, this.height, gl.RGBA, gl.UNSIGNED_BYTE, pixels); }
        finally { gl.bindFramebuffer(gl.READ_FRAMEBUFFER, framebuffer); }
        let binary = '';
        for (let i = 0; i < pixels.length; i += 32768)
            binary += String.fromCharCode(...pixels.subarray(i, i + 32768));
        return `data:application/x-rgba;width=${this.width};height=${this.height};base64,${btoa(binary)}`;
    };
}
