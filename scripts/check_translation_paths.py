"""Check native translation startup with synthetic multilingual file paths."""
import argparse
import json
from pathlib import Path
import shutil
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from voxlate.runtime import run_external, external_env


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--engine', type=Path, required=True, help='Path to llama-server.exe')
    args = parser.parse_args()
    engine = args.engine.resolve(strict=True)
    scratch = ROOT / 'temp' / ('path-check-' + uuid.uuid4().hex)
    scratch.mkdir(parents=True)
    results = {}
    try:
        env = {k: v for k, v in external_env().items() if not k.startswith('LLAMA_')}
        for name in ('English movie (2026)', '中文电影(双语)', '日本語の映画【字幕】',
                     "中日English & # [字幕] '100%' + = ; !", '映像🎬 テスト éü'):
            work = scratch / (name + '.mkv.voxlate') / '.temp'
            work.mkdir(parents=True)
            key = work / 'session.key'
            key.write_text('synthetic-test-key', encoding='utf-8')
            result = run_external([str(engine), '--offline', '--api-key-file', key.name, '--help'],
                                  cwd=work, env=env, timeout=20)
            stderr = result.stderr.decode('utf-8', errors='replace')
            results[name] = result.returncode == 0 and 'failed to open file' not in stderr
        print(json.dumps(results, ensure_ascii=False, indent=2))
        if not all(results.values()):
            raise SystemExit('A native startup check failed.')
    finally:
        resolved = scratch.resolve()
        if resolved.parent != (ROOT / 'temp').resolve() or not resolved.name.startswith('path-check-'):
            raise RuntimeError('Unexpected scratch path')
        shutil.rmtree(resolved)


if __name__ == '__main__':
    main()
