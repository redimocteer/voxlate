"""Named, project-local model lifecycle messages shared with the GUI."""
import json
from pathlib import Path


def model_name(kind, config):
    if kind == 'speakers':
        return 'CAMPPlus'
    if kind == 'tts':
        return 'IndexTTS 2.5'
    if kind == 'asr':
        if config.get('combined'):
            return 'Whisper large-v3 + turbo'
        if Path(config.get('model_path', '')).name == 'qwen3-asr-1.7b':
            return 'Qwen3-ASR 1.7B / ForcedAligner 0.6B'
        name = Path(config.get('model_path', '')).name
        return 'Whisper ' + name.removeprefix('faster-whisper-')
    if kind == 'translator':
        from .translation_models import MODELS
        item = next((v for v in MODELS.values() if v['kind'] == config.get('model_type')), None)
        return item['title'].split(' · ')[0] if item else config.get('model_type', '本地翻译')
    return 'Demucs ' + config.get('model', 'htdemucs')


def model_event(directory, message):
    print(message, flush=True)
    if directory is not None:
        with (Path(directory)/'model_events.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(message, ensure_ascii=True) + '\n')
