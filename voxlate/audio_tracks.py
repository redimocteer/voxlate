"""Audio stream selection; indices are zero-based among audio streams only."""
import json
import subprocess

from .common import VoxlateError
from .runtime import run_external


def selected_track(cfg):
    if not isinstance(cfg, dict):
        raise VoxlateError('音轨设置无效')
    value = cfg.get('audio_track', 0)
    if type(value) is not int or value < 0:
        raise VoxlateError('音轨编号无效')
    return value


def track_suffix(cfg, separator='.'):
    track = selected_track(cfg)
    count = cfg.get('audio_track_count', 0)
    multiple = type(count) is int and count > 1
    return f'{separator}audio-{track+1}' if track or multiple else ''


def track_label(index, stream):
    tags = {k.lower(): str(v) for k, v in stream.get('tags', {}).items()}
    language = tags.get('language', '')
    language = {'eng':'英语', 'en':'英语', 'jpn':'日语', 'ja':'日语',
                'zho':'中文', 'chi':'中文', 'zh':'中文', 'cmn':'普通话',
                'yue':'粤语', 'und':''}.get(language, language)
    channels = stream.get('channels')
    layout = {1:'单声道', 2:'立体声'}.get(channels, stream.get('channel_layout') or (f'{channels} 声道' if channels else ''))
    title = ' '.join(tags.get('title', '').split())
    return ' · '.join(filter(None, [f'音轨 {index+1}', language, title, layout]))


def read_audio_tracks(video, cfg):
    try:
        result = run_external([cfg['ffprobe'], '-v', 'error', '-select_streams', 'a',
            '-show_streams', '-of', 'json', str(video)], timeout=8)
        if result.returncode:
            raise VoxlateError('无法读取音轨，请检查视频和 FFprobe。')
        streams = json.loads(result.stdout.decode('utf-8', errors='replace'))['streams']
        return [dict(index=i, label=track_label(i, s)) for i, s in enumerate(streams)]
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError) as exc:
        raise VoxlateError('音轨读取失败，请检查视频和 FFprobe。') from exc
