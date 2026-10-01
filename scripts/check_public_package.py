"""Reject private content and external model/tool bundles before a release."""
from pathlib import Path
import re

FORBIDDEN_SUFFIXES = {'.gguf', '.safetensors', '.pth', '.pt', '.th', '.onnx', '.bin', '.ckpt',
                      '.wav', '.mp3', '.mp4', '.mkv', '.mov', '.avi', '.webm', '.flac', '.m4a', '.log'}
TOKEN = re.compile(rb'(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|hf_[A-Za-z0-9]{30,})')


def audit_release_tree(directory, *, private_roots=None):
    directory = Path(directory)
    roots = private_roots if private_roots is not None else [Path(__file__).resolve().parents[1], Path.home()]
    patterns = []
    for root in roots:
        value = str(root).lower()
        for variant in {value, value.replace('\\', '/'), value.replace('/', '\\'),
                        value.replace('\\', '\\\\')}:
            patterns.extend(variant.encode(encoding) for encoding in ('utf-8', 'utf-16le'))

    def inspect(label, data):
        if any(p in data.lower() for p in patterns) or TOKEN.search(data):
            # Report only the package-relative location, never echo the secret.
            raise ValueError(f'Potential private information in release: {label}')

    for path in directory.rglob('*'):
        relative = path.relative_to(directory).as_posix()
        if path.is_symlink() or path.is_junction():
            raise ValueError(f'Redirected path in release: {relative}')
        if not path.is_file():
            continue
        if (path.suffix.lower() in FORBIDDEN_SUFFIXES or path.name.lower() in
                {'project.json', 'config.local.json', 'player-settings.json', '.env', 'ffmpeg.exe', 'ffprobe.exe'}
                or path.name.lower().startswith('.env.')
                or any(p.lower().endswith('.voxlate') for p in path.parts)):
            raise ValueError(f'External resource or user data must not ship: {relative}')
        inspect(relative, path.read_bytes())
        if path.name == 'voxlate.exe':
            from PyInstaller.archive.readers import CArchiveReader
            archive = CArchiveReader(str(path))
            for name, entry in archive.toc.items():
                if entry[-1] == 'z':
                    pyz = archive.open_embedded_archive(name)
                    for module in pyz.toc:
                        data = pyz.extract(module, raw=True)
                        if data:
                            inspect(f'{relative}:PYZ:{module}', data)
                else:
                    inspect(f'{relative}:{name}', archive.extract(name))
