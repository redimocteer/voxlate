"""Build a reviewable Windows release and preserve third-party source/notices.

No credentials, user projects or models are read. Downloads are official source
archives, cached in the ignored .build-deps directory. Nothing is published.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
from html import unescape
import importlib.metadata
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import ssl
import subprocess
import sys
import tarfile
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
QT = '6.8.3'
QT_URL = f'https://download.qt.io/archive/qt/6.8/{QT}/submodules/'
SOURCES = {f'{m}-everywhere-src-{QT}.tar.xz':
           QT_URL + f'{m}-everywhere-src-{QT}.tar.xz'
           for m in ('qtbase', 'qtdeclarative', 'qtmultimedia', 'qtsvg', 'qtimageformats')}
SOURCES[f'pyside-setup-everywhere-src-{QT}.tar.xz'] = (
    f'https://download.qt.io/official_releases/QtForPython/pyside6/PySide6-{QT}-src/'
    f'pyside-setup-everywhere-src-{QT}.tar.xz')
SOURCES['ffmpeg-7.1.tar.xz'] = 'https://ffmpeg.org/releases/ffmpeg-7.1.tar.xz'
LICENSE_PAGE = 'https://doc.qt.io/qt-6.8/qt-attribution-llvmpipe.html'
EXTRA_NOTICES = {
    'OpenSSL-3.5.8-LICENSE.txt': 'https://raw.githubusercontent.com/openssl/openssl/openssl-3.5.8/LICENSE.txt',
    'libffi-LICENSE.txt': 'https://raw.githubusercontent.com/libffi/libffi/v3.4.4/LICENSE',
    'Python-3.12-full-license.html': 'https://docs.python.org/3.12/license.html',
}


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def download(url, path):
    if path.is_file() and path.stat().st_size:
        return
    partial = path.with_name(path.name + '.part')
    print('Downloading', path.name, flush=True)
    request = urllib.request.Request(url, headers={'User-Agent': 'Voxlate-release-preparation'})
    with urllib.request.urlopen(request, timeout=120) as source, partial.open('wb') as target:
        shutil.copyfileobj(source, target)
    partial.replace(path)


def collect_notices(archive, destination):
    """Copy regular notice files only, without extracting arbitrary archive paths."""
    with tarfile.open(archive) as source:
        for item in source:
            rel = PurePosixPath(item.name)
            if not item.isfile() or rel.is_absolute() or '..' in rel.parts:
                continue
            name = rel.name.lower()
            if not (name.startswith(('license', 'licence', 'copying', 'copyright', 'notice'))
                    or 'LICENSES' in rel.parts or name == 'qt_attribution.json'):
                continue
            if item.size > 4_000_000:
                raise ValueError(f'Unexpected notice size: {item.name}')
            target = destination.joinpath(*rel.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with source.extractfile(item) as data:
                target.write_bytes(data.read())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--skip-build', action='store_true')
    args = parser.parse_args()
    metadata = json.loads((ROOT / 'docs/GITHUB_METADATA.json').read_text(encoding='utf-8'))
    tag = metadata['release_tag']
    if not re.fullmatch(r'v\d+\.\d+\.\d+(?:-[a-z0-9.]+)?', tag):
        raise ValueError('Invalid version')
    if importlib.metadata.version('PySide6-Essentials') != QT:
        raise ValueError('Update source manifest before building a different Qt version')
    build = ROOT / 'dist/beta/voxlate'
    if not args.skip_build:
        env = dict(os.environ, VOXLATE_RELEASE_BUILD='1')
        subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm',
                        '--distpath', str(ROOT / 'dist/beta'), '--workpath',
                        str(ROOT / 'build/beta'), str(ROOT / 'voxlate.spec')],
                       cwd=ROOT, env=env, check=True)
    if not (build / 'voxlate.exe').is_file():
        raise FileNotFoundError('Build the application first')
    forbidden = {'qtvirtualkeyboardplugin.dll', 'qt6virtualkeyboard.dll', 'qpdf.dll', 'qt6pdf.dll'}
    if any(p.name.lower() in forbidden for p in build.rglob('*')):
        raise ValueError('Unused Qt plugins remain; rebuild using the current spec')
    cache = ROOT / '.build-deps/release-sources'
    cache.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=3) as pool:
        jobs = [pool.submit(download, url, cache / name) for name, url in SOURCES.items()]
        for job in jobs:
            job.result()
    output = ROOT / 'dist/releases' / tag
    output.mkdir(parents=True, exist_ok=True)
    # Refuse reuse to avoid carrying stale binaries or user-added files.
    stage = output / f'Voxlate-{tag}-windows-x64'
    if stage.exists():
        raise FileExistsError(f'Existing package directory: {stage}; review it before rebuilding')
    shutil.copytree(build, stage)
    for name in ('README.md', 'LICENSE', 'THIRD_PARTY_NOTICES.md'):
        shutil.copy2(ROOT / name, stage / name)
    shutil.copytree(ROOT / 'docs', stage / 'docs')
    (stage / '开始使用.txt').write_text(
        'Voxlate ' + tag + '\n\n完整解压后双击 voxlate.exe。请保留 _internal 文件夹。\n'
        '首次进入 资源配置 → 一键准备；需 NVIDIA CUDA 显卡及数十 GB 磁盘空间。\n'
        '最低内存／显存未系统验证，请先试短片。\n'
        '拖入视频 → 选音轨和语言 → 一键导出，或按四步逐句校对。\n'
        '详情见 README.md 和 docs/DOWNLOAD.md。\n\n'
        '本应用使用 LGPL 许可的 Qt/PySide6/Shiboken，许可正文在 licenses/。\n'
        '您可以替换兼容运行库；说明与对应源码见 docs/BINARY_DISTRIBUTION.md。\n'
        '源码、更新与反馈：https://github.com/redimocteer/voxlate\n', encoding='utf-8-sig')
    notices = stage / 'licenses'
    notices.mkdir()
    shutil.copy2(Path(sys.base_prefix) / 'LICENSE.txt', notices / 'Python-LICENSE.txt')
    versions = {'Python': platform.python_version(), 'OpenSSL': ssl.OPENSSL_VERSION}
    if not ssl.OPENSSL_VERSION.startswith('OpenSSL 3.5.8 '):
        raise ValueError('Update OpenSSL notice manifest for this Python build')
    for name, url in EXTRA_NOTICES.items():
        download(url, cache / name)
        shutil.copy2(cache / name, notices / name)
    for name in ('PySide6-Essentials', 'PySide6-Addons', 'shiboken6', 'PyInstaller'):
        dist = importlib.metadata.distribution(name)
        versions[name] = dist.version
        for file in dist.files or []:
            rel = PurePosixPath(str(file).replace('\\', '/'))
            if any(p == '..' for p in rel.parts):
                continue
            if re.match(r'(license|licence|copying|copyright|notice)', rel.name, re.I):
                src = Path(dist.locate_file(file))
                if src.is_file():
                    target = notices / name / rel.name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, target)
    manifest = []
    for name, url in SOURCES.items():
        path = cache / name
        collect_notices(path, notices / path.name.removesuffix('.tar.xz'))
        manifest.append(dict(file=name, url=url, sha256=sha(path), bytes=path.stat().st_size))
    mesa = cache / 'Mesa-llvmpipe-notices.html'
    download(LICENSE_PAGE, mesa)
    # Preserve upstream license text (not page navigation/scripts).
    html = mesa.read_text(encoding='utf-8')
    blocks = re.findall(r'<pre[^>]*>(.*?)</pre>', html, flags=re.S)
    if len(blocks) < 2:
        raise ValueError('Mesa license page changed; review manually')
    text = '\n\n'.join(unescape(re.sub('<[^>]+>', '', block)) for block in blocks)
    (notices / 'Mesa-llvmpipe.txt').write_text('Source: ' + LICENSE_PAGE + '\n\n' + text, encoding='utf-8')
    write_json(stage / 'THIRD_PARTY_SOURCES.json', manifest)
    write_json(stage / 'BUILD_INFO.json', dict(version=tag, platform='windows-x64', versions=versions))
    # The full corresponding source archives are separately downloadable assets.
    source_zip = output / f'Voxlate-{tag}-third-party-sources.zip'
    with zipfile.ZipFile(source_zip, 'w', compression=zipfile.ZIP_STORED) as z:
        for item in manifest:
            z.write(cache / item['file'], item['file'])
        z.write(stage / 'THIRD_PARTY_SOURCES.json', 'THIRD_PARTY_SOURCES.json')
        z.write(ROOT / 'docs/BINARY_DISTRIBUTION.md', 'BINARY_DISTRIBUTION.md')
    files = [dict(path=p.relative_to(stage).as_posix(), bytes=p.stat().st_size, sha256=sha(p))
             for p in sorted(stage.rglob('*')) if p.is_file()]
    write_json(stage / 'FILE_MANIFEST.json', files)
    zip_path = output / (stage.name + '.zip')
    with zipfile.ZipFile(zip_path, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for path in sorted(stage.rglob('*')):
            if path.is_file():
                z.write(path, path.relative_to(output).as_posix())
    (output / 'SHA256SUMS.txt').write_text(
        ''.join(f'{sha(p)}  {p.name}\n' for p in (zip_path, source_zip)), encoding='ascii')
    print('Prepared candidate files (not published):')
    for path in (zip_path, source_zip, output / 'SHA256SUMS.txt'):
        print(path.name, path.stat().st_size)


if __name__ == '__main__':
    main()
