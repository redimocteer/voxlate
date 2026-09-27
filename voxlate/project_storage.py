"""Locations for input-derived data, independent of the application data folder."""
from pathlib import Path
import os
import shutil
import stat

from .common import VoxlateError
from .recognition_models import MODELS, selected_key
from .audio_tracks import selected_track, track_suffix


def project_root(video):
    video = Path(video).resolve()
    return video.parent / (video.name + '.voxlate')


def default_project_directory(video, cfg):
    selected = selected_key(cfg)
    profile = 'combined' if cfg['asr'].get('combined', False) else (
        'qwen3-asr-1.7b' if selected == 'asr_qwen_model' else 'whisper-' + MODELS[selected]['size_name'])
    suffix = track_suffix(cfg, '-')
    return project_root(video) / (cfg.get('source_lang', 'en') + '-' + profile + suffix)


def existing_project_directory(video, cfg):
    directory = default_project_directory(video, cfg)
    # Old audio-1 projects contain absolute cache paths; reuse them in place.
    if selected_track(cfg) == 0 and track_suffix(cfg) and not (directory/'project.json').is_file() and not (directory/'.lock').exists():
        legacy = default_project_directory(video, dict(cfg, audio_track_count=1))
        if (legacy/'project.json').is_file():
            return legacy
    if selected_track(cfg) == 0 and not cfg.get('audio_track_count') and not (directory/'project.json').is_file() and not (directory/'.lock').exists():
        numbered = default_project_directory(video, dict(cfg, audio_track_count=2))
        if (numbered/'project.json').is_file():
            return numbered
    return directory


def validate_project_directory(video, directory):
    if not Path(directory).resolve().is_relative_to(project_root(video).resolve()):
        raise VoxlateError('项目须放在视频旁的 ' + project_root(video).name + ' 文件夹内。请新建项目。')


def clear_video_project(video):
    """Delete only the video's own project tree; reject redirects and active jobs."""
    from .pipeline import project_lock
    root = project_root(video)
    if not root.exists():
        return
    def checked_tree():
        if root.resolve() != root or not root.is_dir():
            raise VoxlateError('项目目录位置异常，未清空。')
        for directory, dirs, files in os.walk(root, followlinks=False):
            for path in (Path(directory), *(Path(directory)/name for name in dirs + files)):
                info = path.lstat()
                if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                    raise VoxlateError('项目含目录链接或重解析点，未清空。')
                if path.name == '.lock' and path.parent != root:
                    raise VoxlateError('项目正在使用，请先停止处理。')
    checked_tree()
    with project_lock(root):
        checked_tree()
        shutil.rmtree(root)


def worker_environment(directory, env):
    directory = Path(directory).resolve()
    scratch = directory / '.temp' / 'workers'
    scratch.mkdir(parents=True, exist_ok=True)
    return dict(env, TEMP=str(scratch), TMP=str(scratch), TMPDIR=str(scratch))
