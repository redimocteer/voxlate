"""Language directions shared by UI, workers and project/cache naming."""
from pathlib import Path

from .common import VoxlateError

LANGUAGES = {
    'zh': {'name': '中文', 'prompt': '简体中文', 'asr': 'Chinese', 'tts': 'ZH', 'iso3': 'zho'},
    'en': {'name': '英文', 'prompt': '英语', 'asr': 'English', 'tts': 'EN', 'iso3': 'eng'},
    'ja': {'name': '日文', 'prompt': '日语', 'asr': 'Japanese', 'tts': 'JA', 'iso3': 'jpn'},
}
# Keep the two existing choices first for familiar UI ordering.
DIRECTIONS = (('en', 'zh'), ('ja', 'zh'), ('zh', 'en'), ('ja', 'en'), ('zh', 'ja'), ('en', 'ja'))


def direction(cfg):
    source, target = cfg.get('source_lang', 'en'), cfg.get('target_lang', 'zh')
    if (source, target) not in DIRECTIONS:
        raise VoxlateError('请选择中文、英文、日文之间的不同源语言和目标语言。')
    return source, target


def direction_id(cfg):
    return '-'.join(direction(cfg))


def direction_label(source, target):
    return f"{LANGUAGES[source]['name']} → {LANGUAGES[target]['name']}"


def translation_settings(cfg):
    source, target = direction(cfg)
    settings = dict(cfg['translator'], source_lang=source)
    settings.pop('target_lang', None)
    # Preserve old Chinese translation cache keys exactly.
    if target != 'zh':
        settings['target_lang'] = target
    return settings


def speech_settings(cfg):
    _, target = direction(cfg)
    settings = dict(cfg['tts'])
    settings.pop('target_lang', None)
    if target != 'zh':
        settings['target_lang'] = target
    return settings


def default_output(video, cfg):
    from .audio_tracks import track_suffix
    video = Path(video)
    return video.with_name(f'{video.stem}{track_suffix(cfg)}.{direction_id(cfg)}.mp4')
