"""Development helper: pin public Qwen model metadata (no project inputs)."""
import json
from pathlib import Path
import pprint
import urllib.request

models = []
for repo, prefix in [('Qwen/Qwen3-ASR-1.7B', ''), ('Qwen/Qwen3-ForcedAligner-0.6B', 'aligner/')]:
    with urllib.request.urlopen('https://huggingface.co/api/models/'+repo+'?blobs=true', timeout=30) as response:
        metadata = json.load(response)
    files = {s['rfilename']: (s['size'], (s.get('lfs') or {}).get('sha256'))
             for s in metadata['siblings'] if s['rfilename'] not in ('.gitattributes', 'README.md')}
    models.append(dict(repo=repo, revision=metadata['sha'], prefix=prefix, files=files))
target = Path(__file__).resolve().parents[1]/'voxlate/qwen_manifest.py'
target.write_text('"""Pinned official Qwen ASR and timestamp model downloads."""\nPARTS = '+pprint.pformat(models, width=110, sort_dicts=False)+'\n', encoding='utf-8')
print('Pinned', sum(size for part in models for size, _ in part['files'].values()), 'bytes')
