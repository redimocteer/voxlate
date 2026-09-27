"""Local paired TTS timing experiment; all outputs stay in the input project."""
import argparse
import logging
from pathlib import Path
import sys
import time
import wave
import threading
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from voxlate.common import load_config, read_json, write_json, enable_offline
from voxlate.pipeline import VideoDubPipeline
from voxlate.project_storage import validate_project_directory
from voxlate.tts_session import TTSSession
from voxlate.media import set_cancel_event


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('project', type=Path)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--ids', type=int, nargs='+', required=True)
    args = parser.parse_args()
    path = args.project.resolve()
    project = read_json(path)
    validate_project_directory(project['input'], path.parent)
    work = path.parent/'.temp'/('emotion-comparison-'+time.strftime('%Y%m%d-%H%M%S'))
    work.mkdir(parents=True)
    cfg = load_config(args.config)
    reference = project.get('speaker_reference') or str(path.parent/'speaker_A.wav')
    rows = [s for s in project['segments'] if s['id'] in args.ids]
    assert len(rows) == len(args.ids) and Path(reference).is_file()
    assert all(Path(s['source_audio']).is_file() for s in rows)
    enable_offline()
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    session = TTSSession()
    report = []
    try:
        for emotion in (False, True):
            cfg['tts']['emotion_reference'] = emotion
            pipeline = VideoDubPipeline(cfg, tts_session=session)
            pipeline.work = work
            for take in (1, 2):
                tag = f"{'on' if emotion else 'off'}-{take}"
                segments = [dict(s, tts_audio=str(work/f'{tag}-{s["id"]}.wav')) for s in rows]
                started = time.perf_counter()
                cancel = threading.Event()
                set_cancel_event(cancel)
                timeout = threading.Timer(120, cancel.set)
                timeout.start()
                try:
                    pipeline.run_worker('tts', dict(reference=reference, segments=segments, total=len(segments)))
                finally:
                    timeout.cancel()
                    set_cancel_event(None)
                metrics = read_json(work/'tts_metrics.json')
                metrics.update(emotion=emotion, take=take, wall_seconds=time.perf_counter()-started, audio=[])
                for s in segments:
                    with wave.open(s['tts_audio'], 'rb') as audio:
                        seconds = audio.getnframes()/audio.getframerate()
                    metrics['audio'].append(dict(id=s['id'], path=s['tts_audio'], seconds=seconds,
                        source_seconds=s['end']-s['start']))
                report.append(metrics)
                write_json(work/'comparison.json', report)
                print(f"TEST {tag}: {metrics}", flush=True)
    finally:
        session.close()
    print('REPORT '+str(work/'comparison.json'))


if __name__ == '__main__':
    main()
