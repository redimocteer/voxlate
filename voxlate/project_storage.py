"""Locations for input-derived data, independent of the application data folder."""
from pathlib import Path
import copy
import os
import shutil
import stat

from .common import VoxlateError, digest, file_hash, read_json, write_json
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


def relocated_project(project, path):
    """Rebase known asset fields when a video and its project tree move together.

    Never rewrite dialogue, model settings, checkpoints or external references.
    The video digest prevents attaching an old project to a different same-name file.
    Callers persist the returned copy under their existing project lock.
    """
    old_video = Path(project['input']).resolve()
    old_root = project_root(old_video)
    path = Path(path).resolve()
    root = next((p for p in path.parents if p.name == old_root.name), None)
    if root is None or root == old_root:
        return project
    video = root.parent / old_video.name
    if not video.is_file():
        raise VoxlateError('项目已移动，请将同名视频与它的 .voxlate 文件夹放在一起后重新打开。')
    expected = project.get('input_hash')
    if not expected or file_hash(video) != expected:
        raise VoxlateError('当前位置的视频与项目不匹配，未修改项目。请放回对应的视频。')
    result = copy.deepcopy(project)

    def rebase(value):
        if not isinstance(value, str) or not value or not Path(value).is_absolute():
            return value
        original = Path(value).resolve()
        if not original.is_relative_to(old_root):
            return value
        destination = root / original.relative_to(old_root)
        if not destination.resolve().is_relative_to(root):
            raise VoxlateError('项目中的文件指向目录外，未更新路径。')
        return str(destination)

    fields = ('source_audio', 'tts_audio', 'aligned_audio', 'speaker_reference_audio')
    for segment in result.get('segments', []):
        for record in (segment, *segment.get('voice_versions', {}).values()):
            for field in fields:
                if field in record:
                    record[field] = rebase(record[field])
    for field in ('speaker_reference', 'output'):
        if field in result:
            result[field] = rebase(result[field])
    if isinstance(result.get('external_voice'), dict) and 'audio' in result['external_voice']:
        result['external_voice']['audio'] = rebase(result['external_voice']['audio'])
    for field in ('manual_edits', 'recognition_history', 'translation_history'):
        for record in result.get(field, []):
            if 'backup' in record:
                record['backup'] = rebase(record['backup'])
    # Only the standard adjacent export follows the video; custom destinations stay put.
    def default_output(video, cfg):
        return video.with_name(video.stem + track_suffix(cfg) + ".zh.mp4")
    if result.get('output') and Path(result['output']).resolve() == default_output(old_video, project):
        result['output'] = str(default_output(video, project))
    result['input'] = str(video)
    return result


def relocate_saved_project(project, path, *, expected_hash=None):
    """Apply an opening-time relocation without clobbering concurrent edits."""
    result = relocated_project(project, path)
    if result is project:
        return project
    from .pipeline import project_lock
    with project_lock(Path(path).resolve().parent):
        if digest(read_json(path)) != (expected_hash or digest(project)):
            raise VoxlateError('项目已被其他操作更新，请重新打开。')
        write_json(path, result)
    return result


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
