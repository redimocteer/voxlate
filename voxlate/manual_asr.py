"""Recognize user-defined blocks without replacing their manual boundaries."""
import gc
from pathlib import Path

from .common import write_json
from .model_lifecycle import model_event


def transcribe_blocks(blocks, cfg, directory):
    def progress(index, title):
        if len(blocks) == 1:
            return
        write_json(Path(directory)/'asr_progress.json', dict(stage='processing',
            detail=f'识别（{title}）：{index}/{len(blocks)} 句'))
    if Path(cfg['model_path']).name == 'qwen3-asr-1.7b' and not cfg.get('combined'):
        return qwen_blocks(blocks, cfg, directory, progress)
    from .asr import configure_cuda
    from faster_whisper import WhisperModel
    configure_cuda(cfg)
    paths = [Path(cfg['model_path'])]
    if cfg.get('combined'):
        paths = [paths[0].parent/name for name in ('faster-whisper-large-v3', 'faster-whisper-large-v3-turbo')]
        model_event(directory, '手动切点保持不变；综合识别以 v3 文字为主，turbo 补充空块。')
    candidates = []
    for path in paths:
        name = 'Whisper '+path.name.removeprefix('faster-whisper-')
        model_event(directory, f'正在加载识别模型（{name}）')
        model = None
        try:
            model = WhisperModel(str(path), device=cfg['device'], compute_type=cfg['compute_type'],
                cpu_threads=cfg['cpu_threads'], local_files_only=True)
            results = []
            for number, block in enumerate(blocks, 1):
                progress(number, name)
                segments, _ = model.transcribe(block['audio'], language=cfg.get('language', 'en'),
                    task='transcribe', beam_size=cfg['beam_size'], vad_filter=False,
                    condition_on_previous_text=False, word_timestamps=False)
                separator = ' ' if cfg.get('language', 'en') == 'en' else ''
                results.append(separator.join(s.text.strip() for s in segments if s.text.strip()))
            candidates.append(results)
        finally:
            del model
            gc.collect()
            model_event(directory, f'已释放识别模型（{name}）')
    return [next((texts[i] for texts in candidates if texts[i]), '') for i in range(len(blocks))]


def qwen_blocks(blocks, cfg, directory, progress):
    import torch
    import soundfile as sf
    import librosa
    from qwen_asr import Qwen3ASRModel
    device = 'cuda:0' if cfg['device'] == 'cuda' else 'cpu'
    dtype = torch.bfloat16 if device.startswith('cuda') and torch.cuda.is_bf16_supported() else (
        torch.float16 if device.startswith('cuda') else torch.float32)
    torch.set_num_threads(cfg.get('cpu_threads', 4))
    model = None
    try:
        model_event(directory, '正在加载识别模型（Qwen3-ASR 1.7B）')
        model = Qwen3ASRModel.from_pretrained(str(Path(cfg['model_path']).resolve()), dtype=dtype,
            device_map=device, attn_implementation='sdpa', local_files_only=True,
            max_inference_batch_size=1, max_new_tokens=1024)
        results = []
        for number, block in enumerate(blocks, 1):
            progress(number, 'Qwen3-ASR 1.7B')
            parts = []
            with sf.SoundFile(block['audio']) as source:
                # Bound model input size without introducing new user-visible cuts.
                while source.tell() < source.frames:
                    samples = source.read(source.samplerate*30, dtype='float32', always_2d=True).mean(axis=1)
                    if source.samplerate != 16000:
                        samples = librosa.resample(samples, orig_sr=source.samplerate, target_sr=16000)
                    text = model.transcribe(audio=(samples, 16000), language={'en': 'English', 'ja': 'Japanese', 'zh': 'Chinese'}[
                        cfg.get('language', 'en')])[0].text.strip()
                    if text:
                        parts.append(text)
            results.append((' ' if cfg.get('language', 'en') == 'en' else '').join(parts))
        return results
    finally:
        del model
        gc.collect()
        if device.startswith('cuda'):
            torch.cuda.empty_cache()
        model_event(directory, '已释放识别模型（Qwen3-ASR 1.7B）')
