"""Content-addressed, locked publication of offline QEMU setup overlays."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import time


def cache_key(configuration):
    return hashlib.sha256(json.dumps(configuration, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def archive_digest(path: Path):
    """Fingerprint exactly staged dependency payloads, excluding tar timestamps."""
    digest = hashlib.sha256()
    with tarfile.open(path, 'r:gz') as archive:
        for member in sorted(archive.getmembers(), key=lambda member: member.name):
            if member.isdir() or '.git' in Path(member.name).parts:
                continue
            digest.update(json.dumps([member.name, member.mode, member.type.decode(),
                                      member.linkname], separators=(',', ':')).encode() + b'\0')
            if member.isfile():
                with archive.extractfile(member) as stream:
                    for block in iter(lambda: stream.read(1024 * 1024), b''):
                        digest.update(block)
            digest.update(b'\0')
    return digest.hexdigest()


def base_identity(base: Path):
    """Include every backing file's identity/version without rereading huge VMs."""
    chain = json.loads(subprocess.check_output(
        ['qemu-img', 'info', '--backing-chain', '--output=json', str(base.resolve())], text=True))
    result = []
    for info in chain:
        path = Path(info['filename']).resolve()
        stat = path.stat()
        result.append({'path': str(path), 'format': info['format'],
                       'virtual_size': info['virtual-size'], 'size': stat.st_size,
                       'mtime_ns': stat.st_mtime_ns, 'ctime_ns': stat.st_ctime_ns,
                       'device': stat.st_dev, 'inode': stat.st_ino})
    return result


class SetupCache:
    def __init__(self, root: Path, configuration):
        self.root = root.resolve()
        self.configuration = configuration
        self.key = cache_key(configuration)
        self.entry = self.root / self.key

    @contextmanager
    def locked(self):
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / f'{self.key}.lock').open('a') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            yield

    def valid(self):
        try:
            metadata = json.loads((self.entry / 'metadata.json').read_text())
            image = self.entry / 'setup.qcow2'
            stat = image.stat()
            return (metadata['key'] == self.key and metadata['configuration'] == self.configuration
                    and metadata['image_size'] == stat.st_size
                    and metadata['image_mtime_ns'] == stat.st_mtime_ns)
        except (OSError, ValueError, KeyError, TypeError):
            return False

    def obtain(self, prepare):
        """Caller holds locked(); prepare must return only after clean poweroff.

        Publication swaps a complete directory into view. Failed preparation
        leaves no reusable image; even a killed preparer has no metadata entry.
        """
        if self.valid():
            return self.entry / 'setup.qcow2', True
        if self.entry.exists():
            # An existing published image may back a running overlay. Never
            # rename/replace it, even when corruption makes it unusable here.
            raise RuntimeError(f'invalid setup cache entry: {self.entry}; use --no-setup-cache or another cache directory')
        with tempfile.TemporaryDirectory(prefix=f'.{self.key}-', dir=self.root) as temporary:
            staging = Path(temporary) / 'entry'
            staging.mkdir()
            image = staging / 'setup.qcow2'
            prepare(image)
            image.chmod(0o444)
            stat = image.stat()
            metadata = {'schema_version': 1, 'key': self.key,
                        'configuration': self.configuration, 'created_unix_ns': time.time_ns(),
                        'image_size': stat.st_size, 'image_mtime_ns': stat.st_mtime_ns}
            (staging / 'metadata.json').write_text(json.dumps(metadata, indent=2, sort_keys=True) + '\n')
            os.replace(staging, self.entry)
        return self.entry / 'setup.qcow2', False
