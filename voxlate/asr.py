from pathlib import Path
import os
from .common import VoxlateError

_dll_handles = []


def configure_cuda(config):
    """Reuse installed CUDA libraries without importing or duplicating PyTorch."""
    if os.name != "nt" or config.get("device") != "cuda":
        return
    root = Path(config["model_path"]).resolve().parent.parent
    candidates = [root / ".venv/Lib/site-packages/torch/lib",
                  root / "third_party/index-tts/.venv/Lib/site-packages/torch/lib",
                  root / ".venv-separator/Lib/site-packages/torch/lib"]
    for directory in candidates:
        if all((directory / name).is_file() for name in ("cublas64_12.dll", "cudnn64_9.dll")):
            os.environ["PATH"] = str(directory) + os.pathsep + os.environ.get("PATH", "")
            _dll_handles.append(os.add_dll_directory(str(directory)))
            break


def timed_segments(segments, language, pause=1.2):
    """Use word boundaries and avoid stretching an utterance across long silence."""
    result = []
    previous = 0.0

    def add(start, end, text, words=None, segment=None):
        nonlocal previous
        start = max(previous, float(start))
        if text.strip() and end > start:
            row = dict(id=len(result) + 1, start=start, end=float(end), speaker="A",
                source_lang=language, target_lang="zh", source_text=text.strip(), target_text="")
            if words:
                row['words'] = [dict(start=float(w.start), end=float(w.end), word=w.word,
                    probability=float(getattr(w, 'probability', 0))) for w in words]
                row['confidence'] = sum(w['probability'] for w in row['words']) / len(words)
            for field in ('avg_logprob', 'no_speech_prob'):
                if segment is not None and hasattr(segment, field):
                    row[field] = float(getattr(segment, field))
            result.append(row)
            previous = float(end)

    for segment in segments:
        words = [w for w in (getattr(segment, "words", None) or []) if w.word.strip() and w.end >= w.start]
        if not words or all(w.end == w.start for w in words):
            add(segment.start, segment.end, segment.text, segment=segment)
            continue
        group = []
        for word in words:
            if group and word.start - group[-1].end >= pause:
                add(group[0].start, group[-1].end, "".join(w.word for w in group), group, segment)
                group = []
            group.append(word)
        if group:
            add(group[0].start, group[-1].end, "".join(w.word for w in group), group, segment)
    return result


def transcribe(audio, config, work_dir=None):
    if config.get('combined', False):
        from .combined_asr import transcribe_combined
        return transcribe_combined(audio, config, work_dir)
    if Path(config['model_path']).name == 'qwen3-asr-1.7b':
        from .qwen_recognition import transcribe as qwen_transcribe
        return qwen_transcribe(audio, config, work_dir)
    configure_cuda(config)
    from faster_whisper import WhisperModel
    from .model_lifecycle import model_event, model_name

    name = model_name('asr', config)
    model_event(work_dir, f'正在加载识别模型（{name}）')
    model = WhisperModel(str(Path(config["model_path"]).resolve()),
                         device=config["device"], compute_type=config["compute_type"],
                         cpu_threads=config["cpu_threads"], local_files_only=True)
    model_event(work_dir, f'识别模型已加载（{name}）')
    language = config.get("language", "en")
    if language == "ja" and not model.is_multilingual:
        raise VoxlateError("日文识别需要多语言模型，请下载「英日识别模型」。")
    segments, _ = model.transcribe(str(audio), language=language, task="transcribe",
                                  beam_size=config["beam_size"], vad_filter=True,
                                  condition_on_previous_text=False, word_timestamps=True)
    return timed_segments(segments, language)
