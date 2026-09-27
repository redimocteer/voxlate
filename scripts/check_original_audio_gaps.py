"""Verify an export mix in a separate, project-local directory."""
import argparse
from pathlib import Path
import sys
import time
import wave
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from voxlate.common import load_config, read_json, write_json
from voxlate.media import Media, original_audio_intervals
from voxlate.project_storage import validate_project_directory

parser = argparse.ArgumentParser()
parser.add_argument('project', type=Path)
parser.add_argument('--config', type=Path, required=True)
parser.add_argument('--start', type=float, required=True)
parser.add_argument('--end', type=float, required=True)
args = parser.parse_args()
project = read_json(args.project)
root = args.project.resolve().parent
validate_project_directory(project['input'], root)
cfg = load_config(args.config)
media = Media(cfg)
directory = root/'.temp'/('original-gap-check-'+time.strftime('%Y%m%d-%H%M%S'))
directory.mkdir(parents=True)
original = root/'original.wav'
background = root/'separated'/project['separator_model']/'original/no_vocals.wav'
output = directory/'final_audio.wav'
intervals = original_audio_intervals(project['segments'], project['duration'])
if not any(start <= args.start < args.end <= end for start, end in intervals):
    raise ValueError('检查范围并非完全位于未配音空档')
media.mix(background, root/'dubbing.wav', output, project['duration'],
          cfg['audio']['background_gain'], cfg['audio']['dubbing_gain'],
          original=original, original_intervals=intervals)
with wave.open(str(original)) as source, wave.open(str(output)) as result:
    first, last = round(args.start*source.getframerate()), round(args.end*source.getframerate())
    source.setpos(first); result.setpos(first)
    exact = source.readframes(last-first) == result.readframes(last-first)
    same_length = source.getnframes() == result.getnframes()
if not exact or not same_length:
    raise AssertionError('原声还原验证失败')
for label, audio in [('before', root/'final_audio.wav'), ('after', output), ('original', original)]:
    media.trim(audio, directory/(label+'.wav'), args.start, args.end-args.start)
write_json(directory/'report.json', dict(start=args.start, end=args.end,
           restored_samples_match_original=exact, same_length=same_length, preserved_intervals=intervals))
print('原声采样完全一致；总时长不变。'+str(directory), flush=True)
