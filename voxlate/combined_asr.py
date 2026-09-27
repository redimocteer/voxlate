"""Conservative local cross-check: preserve v3, accept corroborated local repairs."""
import copy
import bisect
import difflib
import gc
from pathlib import Path
import time
import unicodedata

from .common import VoxlateError, write_json
from .asr import configure_cuda, timed_segments

VERSION = 1


def normalized(text):
    return ''.join(c for c in unicodedata.normalize('NFKC', text).casefold() if c.isalnum())


def overlap(a, b):
    return max(0., min(a['end'], b['end']) - max(a['start'], b['start']))


def review_windows(primary, secondary, speech, duration):
    """Bound extra work; prioritize speech absent from BOTH transcripts."""
    covered = sorted((w['start'], w['end']) for row in primary + secondary
                     for w in row.get('words', [row]) if w['end'] > w['start'])
    merged = []
    for lo, hi in covered:
        if merged and lo <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(hi, merged[-1][1]))
        else:
            merged.append((lo, hi))
    ends = [hi for lo, hi in merged]
    candidates = []
    for interval in speech:
        pieces = [(interval['start'], interval['end'])]
        for lo, hi in merged[bisect.bisect_right(ends, interval['start']):]:
            if lo >= interval['end']:
                break
            pieces = [(a, b) for start, end in pieces for a, b in
                      ((start, min(end, lo)), (max(start, hi), end)) if b > a]
            if not pieces:
                break
        candidates.extend((0, start, end) for start, end in pieces if end - start >= .45)
    second_ends = [s['end'] for s in secondary]
    second_starts = [s['start'] for s in secondary]
    for row in primary:
        other = [s for s in secondary[bisect.bisect_right(second_ends, row['start']):
                    bisect.bisect_left(second_starts, row['end'])] if overlap(row, s) > .1]
        text = ''.join(s['source_text'] for s in other)
        similarity = difflib.SequenceMatcher(None, normalized(row['source_text']), normalized(text), autojunk=False).ratio()
        if similarity < .65 or row.get('confidence', 1) < .6:
            candidates.append((1, row['start'], row['end']))
    windows = []
    budget = min(600., max(16., duration * .25))
    for priority, start, end in sorted(candidates):
        lo, hi = max(0., start - 2), min(duration, end + 2, max(0., start - 2) + 12)
        if any(min(hi, b) - max(lo, a) > (hi - lo) * .5 for a, b in windows):
            continue
        if hi - lo > budget or len(windows) >= 100:
            continue
        windows.append((lo, hi))
        budget -= hi - lo
    return sorted(windows)


def corroborated_rows(left, right):
    """Only accept an entire phrase that both local decodes independently report."""
    accepted = []
    for row in left:
        if row.get('confidence', 0) < .65 or row.get('no_speech_prob', 1) > .6:
            continue
        matches = [s for s in right if normalized(s['source_text']) == normalized(row['source_text'])
                   and overlap(row, s) >= min(row['end']-row['start'], s['end']-s['start']) * .4
                   and s.get('confidence', 0) >= .65 and s.get('no_speech_prob', 1) <= .6]
        if matches:
            candidate = copy.deepcopy(row)
            candidate['recognition_evidence'] = 'v3+turbo-local'
            accepted.append(candidate)
    return accepted


def merge_repairs(primary, repairs):
    result = copy.deepcopy(primary)
    changes = []
    for row in sorted(repairs, key=lambda r: (r['start'], r['end'])):
        intersecting = [s for s in result if overlap(row, s) > 0]
        if not intersecting:
            result.append(row)
            changes.append(dict(action='insert', row=row))
        elif len(intersecting) == 1:
            old = intersecting[0]
            # Never replace a long utterance with a short fragment or move its boundary.
            if (abs(old['start']-row['start']) <= .35 and abs(old['end']-row['end']) <= .35
                    and normalized(old['source_text']) != normalized(row['source_text'])
                    and row.get('confidence', 0) >= old.get('confidence', 1) + .15
                    and not any(overlap(row, s) > 0 for s in result if s is not old)):
                result.remove(old)
                result.append(row)
                changes.append(dict(action='replace', before=old, row=row))
    result.sort(key=lambda r: r['start'])
    for i, row in enumerate(result, 1):
        row['id'] = i
    return result, changes


def transcribe_combined(audio, config, work_dir):
    from .model_lifecycle import model_event
    from faster_whisper import WhisperModel
    from faster_whisper.audio import decode_audio
    from faster_whisper.vad import get_speech_timestamps, VadOptions

    if work_dir is None:
        raise VoxlateError('综合识别需要项目目录。')
    destination = Path(work_dir).resolve() / 'recognition'
    destination.mkdir(parents=True, exist_ok=True)
    root = Path(config['model_path']).resolve().parent
    models = {'v3': root/'faster-whisper-large-v3', 'turbo': root/'faster-whisper-large-v3-turbo'}
    for path in models.values():
        if not (path/'model.bin').is_file():
            raise VoxlateError('综合识别需要下载 v3 和 turbo 两个模型。')
    configure_cuda(config)
    samples = decode_audio(str(audio))
    duration = len(samples) / 16000
    language = config.get('language', 'en')
    started = time.perf_counter()
    timings = {}
    def notify(detail):
        print(detail, flush=True)
        write_json(Path(work_dir) / 'asr_progress.json', dict(stage='processing', detail=detail))
    def load(name):
        title = 'Whisper large-v3' if name == 'v3' else 'Whisper turbo'
        model_event(work_dir, f'正在加载识别模型（{title}）')
        model = WhisperModel(str(models[name]), device=config['device'], compute_type=config['compute_type'],
            cpu_threads=config['cpu_threads'], local_files_only=True)
        model_event(work_dir, f'识别模型已加载（{title}）')
        return model
    def decode(model, signal, vad, offset=0):
        segments, _ = model.transcribe(signal, language=language, task='transcribe',
            beam_size=config['beam_size'], vad_filter=vad, condition_on_previous_text=False, word_timestamps=True)
        rows = timed_segments(segments, language)
        for row in rows:
            row['start'] += offset
            row['end'] += offset
            for word in row.get('words', []):
                word['start'] += offset
                word['end'] += offset
        return rows

    notify('综合识别 1/3：v3')
    stage = time.perf_counter()
    model = load('v3')
    primary = decode(model, samples, True)
    del model
    gc.collect()
    model_event(work_dir, '已释放识别模型（Whisper large-v3）')
    timings['v3'] = time.perf_counter() - stage
    write_json(destination/'v3.json', primary)
    notify('综合识别 2/3：turbo')
    stage = time.perf_counter()
    model = load('turbo')
    secondary = decode(model, samples, True)
    timings['turbo'] = time.perf_counter() - stage
    write_json(destination/'turbo.json', secondary)
    speech = [{key: value/16000 for key, value in item.items()} for item in get_speech_timestamps(samples,
        VadOptions(threshold=.1, min_silence_duration_ms=300, speech_pad_ms=200, min_speech_duration_ms=100))]
    windows = review_windows(primary, secondary, speech, duration)
    reviews = []
    notify(f'综合识别 3/3：复查 {len(windows)} 处疑点')
    stage = time.perf_counter()
    for lo, hi in windows:
        reviews.append(dict(start=lo, end=hi, turbo=decode(model, samples[int(lo*16000):int(hi*16000)], False, lo)))
    del model
    gc.collect()
    model_event(work_dir, '已释放识别模型（Whisper turbo）')
    if windows:
        model = load('v3')
        for item in reviews:
            lo, hi = item['start'], item['end']
            item['v3'] = decode(model, samples[int(lo*16000):int(hi*16000)], False, lo)
        del model
        gc.collect()
        model_event(work_dir, '已释放识别模型（Whisper large-v3）')
    timings['review'] = time.perf_counter() - stage
    repairs = [row for item in reviews for row in corroborated_rows(item['v3'], item['turbo'])]
    result, changes = merge_repairs(primary, repairs)
    timings['total'] = time.perf_counter() - started
    write_json(destination/'review.json', dict(version=VERSION, windows=reviews, changes=changes, seconds=timings))
    write_json(destination/'combined.json', result)
    notify(f"综合识别完成：复查 {len(windows)} 处，补充/修正 {len(changes)} 处，耗时 {timings['total']:.1f} 秒")
    return result
