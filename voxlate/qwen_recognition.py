"""Local Qwen ASR, followed by a separately loaded word-timestamp model."""
import gc
import math
from pathlib import Path
import re
import unicodedata
from .common import VoxlateError, write_json
from .media import check_cancelled
from .model_lifecycle import model_event


def normalized(text):
    chars, positions = [], []
    for index, char in enumerate(text):
        for value in unicodedata.normalize('NFKC', char).casefold().replace('’', "'"):
            if value.isalnum() or value == "'":
                chars.append(value); positions.append(index)
    return ''.join(chars), positions


def aligned_segments(text, words, offset, duration, language):
    """Keep ASR spelling/punctuation; attach aligner timestamps without moving text."""
    clean, positions = normalized(text)
    matched, cursor = [], 0
    for word in words:
        token = normalized(word.text)[0]
        if not token:
            continue
        found = clean.find(token, cursor)
        if found != cursor:
            raise VoxlateError('Qwen 时间对齐文字不一致，原始结果已保存在项目 recognition 目录。')
        start, end = float(word.start_time), float(word.end_time)
        if not all(math.isfinite(v) for v in (start, end)) or start < 0 or end < start or end > duration+.5:
            raise VoxlateError('Qwen 时间对齐超出音频范围，请查看项目中的原始结果。')
        matched.append((positions[found], start, min(duration, end)))
        cursor = found+len(token)
    if cursor != len(clean) or not matched:
        raise VoxlateError('Qwen 未能完整对齐识别文字，请查看项目中的原始结果。')
    result, group, previous = [], [], 0.
    uncertain = False
    def flush():
        nonlocal previous, uncertain
        if not group:
            return
        start = max(previous, group[0]['start'])
        end = max(w['end'] for w in group)
        content = ''.join(w['word'] for w in group).strip()
        if end <= start:
            # Keep the text pending until there is a usable end time. Do not
            # invent a short duration or silently drop an untimed interjection.
            uncertain = True
            return
        result.append(dict(start=offset+start, end=offset+end, speaker='A', source_lang=language,
            target_lang='zh', source_text=content, target_text='',
            timing_uncertain=uncertain,
            words=[dict(w, start=offset+w['start'], end=offset+w['end']) for w in group]))
        previous = end
        group.clear()
        uncertain = False
    for index, (position, start, end) in enumerate(matched):
        begin = 0 if index == 0 else position
        stop = matched[index+1][0] if index+1 < len(matched) else len(text)
        fragment = text[begin:stop]
        if group and start-group[-1]['end'] >= 1.0:
            flush()
        group.append(dict(start=start, end=end, word=fragment))
        # Sentence boundaries come from the ASR model's punctuation.
        ending = re.search(r'[。！？!?]|\.(?:[\s\"”’]|$)', fragment)
        abbreviation = language == 'en' and re.fullmatch(r'\s*(?:Mr|Mrs|Ms|Dr|Prof|St|Jr|Sr)\.\s*', fragment, re.I)
        if ending and not abbreviation and end-group[0]['start'] >= .25:
            flush()
    flush()
    if group:
        if not result:
            raise VoxlateError('Qwen 未能确定此段对白时间，请查看 recognition 中的结果。')
        # Untimed trailing text stays with its preceding sentence for review.
        result[-1]['source_text'] += (' ' if language == 'en' else '') + ''.join(w['word'] for w in group).strip()
        result[-1]['words'].extend(dict(w,start=offset+w['start'],end=offset+w['end']) for w in group)
        result[-1]['timing_uncertain'] = True
    return result


def transcribe(audio, config, work_dir):
    import numpy as np
    import soundfile as sf
    import librosa
    import torch
    from qwen_asr import Qwen3ASRModel, Qwen3ForcedAligner
    directory = Path(work_dir)/'recognition'
    directory.mkdir(parents=True, exist_ok=True)
    def progress(message):
        print(message, flush=True)
        write_json(Path(work_dir)/'asr_progress.json',dict(stage='processing', detail=message))
    language = {'en':'English', 'ja':'Japanese'}[config.get('language', 'en')]
    device = 'cuda:0' if config['device'] == 'cuda' else 'cpu'
    dtype = torch.bfloat16 if device.startswith('cuda') and torch.cuda.is_bf16_supported() else (
        torch.float16 if device.startswith('cuda') else torch.float32)
    torch.set_num_threads(config.get('cpu_threads', 4))
    def release():
        gc.collect()
        if device.startswith('cuda'): torch.cuda.empty_cache()
    def read(source, start, stop):
        source.seek(start)
        samples = source.read(stop-start, dtype='float32', always_2d=True).mean(axis=1)
        return librosa.resample(samples, orig_sr=source.samplerate, target_sr=16000) if source.samplerate != 16000 else samples
    chunks = []
    model = None
    try:
        model_event(work_dir, '正在加载识别模型（Qwen3-ASR 1.7B）')
        model = Qwen3ASRModel.from_pretrained(str(Path(config['model_path']).resolve()),
            dtype=dtype, device_map=device, attn_implementation='sdpa', local_files_only=True,
            max_inference_batch_size=1, max_new_tokens=1024)
        model_event(work_dir, '识别模型已加载（Qwen3-ASR 1.7B）')
        with sf.SoundFile(str(audio)) as source:
            start, rate, total = 0, source.samplerate, source.frames
            while start < total:
                check_cancelled()
                stop = min(total, start+30*rate)
                if stop < total:
                    source.seek(start+20*rate)
                    tail = source.read(stop-start-20*rate, dtype='float32', always_2d=True).mean(axis=1)
                    width = max(1, round(rate*.2))
                    energy = [float(np.mean(tail[i:i+width]**2)) for i in range(0,len(tail)-width+1,width)]
                    if energy:
                        stop = start+20*rate+int(np.argmin(energy))*width+width//2
                samples = read(source, start, stop)
                text = model.transcribe(audio=(samples,16000), language=language)[0].text.strip()
                chunks.append(dict(start=start/rate, end=stop/rate, first_frame=start, last_frame=stop, text=text))
                write_json(directory/'qwen_transcript.json', chunks)
                progress(f'Qwen 识别：{stop/rate:.0f}/{total/rate:.0f} 秒（{stop*100/total:.0f}%）')
                start = stop
    finally:
        del model
        release()
        model_event(work_dir, '已释放识别模型（Qwen3-ASR 1.7B）')
    rows, alignments = [], []
    model = None
    try:
        model_event(work_dir, '正在加载时间对齐模型（Qwen3-ForcedAligner 0.6B）')
        model = Qwen3ForcedAligner.from_pretrained(str(Path(config['model_path'])/'aligner'),
            dtype=dtype, device_map=device, attn_implementation='sdpa', local_files_only=True)
        with sf.SoundFile(str(audio)) as source:
            for index, chunk in enumerate(chunks):
                check_cancelled()
                if chunk['text']:
                    samples = read(source, chunk['first_frame'], chunk['last_frame'])
                    words = model.align(audio=(samples,16000), text=chunk['text'], language=language)[0].items
                    alignments.append(dict(chunk=chunk, words=[vars(word) for word in words]))
                    write_json(directory/'qwen_alignment.json', alignments)
                    rows.extend(aligned_segments(chunk['text'], words, chunk['start'],chunk['end']-chunk['start'],config.get('language','en')))
                progress(f'Qwen 时间对齐：{index+1}/{len(chunks)}（{(index+1)*100/len(chunks):.0f}%）')
    finally:
        del model
        release()
        model_event(work_dir, '已释放时间对齐模型（Qwen3-ForcedAligner 0.6B）')
    for number, row in enumerate(rows,1): row['id']=number
    uncertain = [row['id'] for row in rows if row.get('timing_uncertain')]
    if uncertain:
        model_event(work_dir, 'Qwen 对齐不确定，请试听第 '+ '、'.join(map(str, uncertain))+' 句。')
    return rows
