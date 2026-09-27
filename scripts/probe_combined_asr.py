"""Local experiment; all input-derived files stay beside the input video."""
import argparse
import dataclasses
import os
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from voxlate.common import read_json, write_json, enable_offline

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('video', type=Path)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--mode', choices=['prepare', 'v3', 'turbo'], required=True)
    args = parser.parse_args()
    video = args.video.resolve()
    work = video.parent / (video.name + '.voxlate') / 'combined-experiment'
    work.mkdir(parents=True, exist_ok=True)
    scratch = work / '.temp'
    scratch.mkdir(exist_ok=True)
    os.environ.update(TEMP=str(scratch), TMP=str(scratch), TMPDIR=str(scratch))
    os.chdir(work)
    enable_offline()
    cfg = read_json(args.config)
    start = time.perf_counter()
    if args.mode == 'prepare':
        from voxlate.media import Media
        media = Media(cfg)
        info = media.probe(video)
        media.extract(video, work/'original.wav', float(info['format']['duration']))
        from voxlate.separator import separate
        separate(work/'original.wav', work/'separated', cfg['separator'])
    else:
        from voxlate.asr import configure_cuda
        from faster_whisper import WhisperModel
        from faster_whisper.audio import decode_audio
        from faster_whisper.vad import get_speech_timestamps, VadOptions
        config = dict(cfg['asr'])
        config['model_path'] = str(Path(cfg['resource_root'])/'models'/(
            'faster-whisper-large-v3' if args.mode == 'v3' else 'faster-whisper-large-v3-turbo'))
        configure_cuda(config)
        audio = decode_audio(str(work/'separated'/cfg['separator']['model']/'original/vocals.wav'))
        model = WhisperModel(config['model_path'], device=config['device'],
            compute_type=config['compute_type'], cpu_threads=4, local_files_only=True)
        segments, _ = model.transcribe(audio, language='en', beam_size=5, vad_filter=True,
            condition_on_previous_text=False, word_timestamps=True)
        result = [dataclasses.asdict(segment) for segment in segments]
        write_json(work/(args.mode+'_detailed.json'), result)
        print(args.mode, 'full seconds', round(time.perf_counter()-start,2), flush=True)
        for threshold in [.5,.25,.1]:
            intervals = get_speech_timestamps(audio, VadOptions(threshold=threshold,
                min_silence_duration_ms=300, speech_pad_ms=200, min_speech_duration_ms=100))
            write_json(work/f'vad-{threshold}.json', [{k:v/16000 for k,v in row.items()} for row in intervals])
        # Fixed diagnostic window for the known omission; not a production heuristic.
        for lo, hi in [(24,33)]:
            segs,_ = model.transcribe(audio[int(lo*16000):int(hi*16000)], language='en',
                beam_size=5, vad_filter=False, condition_on_previous_text=False, word_timestamps=True)
            detail=[dataclasses.asdict(s) for s in segs]
            write_json(work/f'{args.mode}_gap.json', detail)
            print(args.mode,'gap',[(s['text'],round(s['avg_logprob'],2),
                [(w['word'],round(w['probability'],2)) for w in s['words']]) for s in detail],flush=True)
    print('TOTAL',round(time.perf_counter()-start,2),flush=True)

if __name__ == '__main__':
    main()
