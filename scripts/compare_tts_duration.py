"""Compare native IndexTTS pacing with post-synthesis stretching, locally."""
import argparse
import html
import os
from pathlib import Path
import random
import sys
import time
import faulthandler
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from voxlate.common import read_json, load_config, write_json, enable_offline
from voxlate.project_storage import validate_project_directory, worker_environment
from voxlate.media import Media, tempo_filter

parser = argparse.ArgumentParser()
parser.add_argument('project', type=Path)
parser.add_argument('--config', type=Path, required=True)
args = parser.parse_args()
project = read_json(args.project)
validate_project_directory(project['input'], args.project.parent)
work = args.project.parent/'.temp'/('duration-comparison-'+time.strftime('%Y%m%d-%H%M%S'))
work.mkdir(parents=True)
os.environ.update(worker_environment(work, dict(os.environ)))
os.environ.update(HF_HOME=str(work/'hf-cache'), MPLCONFIGDIR=str(work/'matplotlib'))
enable_offline()
print('载入音色克隆环境', flush=True)
faulthandler.dump_traceback_later(90, repeat=True)
import torch
import numpy as np
from voxlate.tts import TTSEngine, valid_wav
cfg = load_config(args.config)
media = Media(cfg)
rows = [s for s in project['segments'] if s.get('enabled', True) and s.get('target_text')
        and 1 <= s['end']-s['start'] <= 5 and len(s['target_text']) <= 20 and valid_wav(s.get('source_audio', ''))
        and valid_wav(s.get('speaker_reference_audio', ''))]
rows.sort(key=lambda s:s.get('generated_duration', 0)/(s['end']-s['start']), reverse=True)
rows = rows[:3]
if not rows:
    raise RuntimeError('没有合适的已配音句子')
started = time.perf_counter()
engine = TTSEngine(cfg['tts'])
torch.set_num_threads(4)
loading = time.perf_counter()-started
report = dict(loading_seconds=loading, items=[])
for row in rows:
    target = row['end']-row['start']
    outputs = {}
    for label in ('normal', 'native'):
        random.seed(2026); np.random.seed(2026); torch.manual_seed(2026)
        if torch.cuda.is_available(): torch.cuda.manual_seed_all(2026)
        factor = 1.0 if label == 'normal' else max(.5, min(1., target/outputs['normal']['seconds']))
        output = work/f"{row['id']}-{label}.wav"
        start = time.perf_counter()
        print(f"第 {row['id']} 句，{label}，duration_factor={factor:.3f}", flush=True)
        engine.generate(row['target_text'], row['speaker_reference_audio'], output, row['source_audio'], duration_factor=factor)
        outputs[label] = dict(path=output.name, seconds=media.duration(output),
                              generation_seconds=time.perf_counter()-start, factor=factor)
    for label, output in list(outputs.items()):
        aligned = work/f"{row['id']}-{label}-aligned.wav"
        ratio = max(1, output['seconds']/target)
        media.render(['-i', work/output['path'], '-af', tempo_filter(ratio), '-t', str(target),
                      '-c:a', 'pcm_s16le'], aligned)
        output['aligned'] = aligned.name
        output['remaining_acceleration'] = ratio
    report['items'].append(dict(id=row['id'], text=row['target_text'], target_seconds=target, outputs=outputs))
    write_json(work/'report.json', report)
    print(f"第 {row['id']} 句：原合成 {outputs['normal']['seconds']:.2f}s，原生调速 {outputs['native']['seconds']:.2f}s，目标 {target:.2f}s", flush=True)
blocks=[]
for row in report['items']:
    a,b=row['outputs']['normal'],row['outputs']['native']
    blocks.append(f"<section><h2>第 {row['id']} 句</h2><p>{html.escape(row['text'])}</p>"
        f"<p>原时间窗口 {row['target_seconds']:.2f} 秒；合成原长 {a['seconds']:.2f} → {b['seconds']:.2f} 秒；比例 {b['factor']:.2f}</p>"
        f"<p>A：原合成后拉伸</p><audio controls src='{a['aligned']}'></audio>"
        f"<p>B：原生语速控制后对齐</p><audio controls src='{b['aligned']}'></audio></section>")
(work/'comparison.html').write_text('<!doctype html><meta charset="utf-8"><title>配音语速对比</title><style>body{font:16px sans-serif;max-width:850px;margin:32px auto}section{padding:12px;border-bottom:1px solid #ddd}audio{width:100%}</style><h1>配音语速对比</h1><p>相同音色、逐句情绪和随机种子。仅生成对照文件，未替换项目配音。</p>'+''.join(blocks),encoding='utf-8')
print('REPORT '+str(work/'comparison.html'), flush=True)
faulthandler.cancel_dump_traceback_later()
