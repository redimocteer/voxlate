"""Short voice references live in the destination video's project."""
import math
from pathlib import Path
import shutil
import uuid
import wave

from .common import VoxlateError, file_hash
from .media import Media, check_cancelled
from .tts import valid_wav


def reference_info(source, cfg):
    info = Media(cfg).probe(source)
    if not any(s.get('codec_type') == 'audio' for s in info.get('streams', [])):
        raise VoxlateError('所选文件没有音轨，请选择音频或带声音的视频。')
    duration = float(info.get('format', {}).get('duration', 0))
    if not math.isfinite(duration) or duration < .1:
        raise VoxlateError('无法读取有效音频时长。')
    return dict(duration=duration, video=any(s.get('codec_type') == 'video' and
        not s.get('disposition', {}).get('attached_pic') for s in info['streams']))


def import_reference(source, work, cfg, selected):
    from .pipeline import project_lock
    source, work = Path(source).resolve(), Path(work).resolve()
    if not isinstance(selected, (list, tuple)) or len(selected) != 2:
        raise VoxlateError('请选择不超过 15 秒的有效音色片段。')
    start, end = selected
    if not all(type(t) in (int, float) and math.isfinite(t) for t in selected) or not 0 <= start < end or not .1 <= end-start <= 15:
        raise VoxlateError('请选择不超过 15 秒的有效音色片段。')
    info = reference_info(source, cfg)
    if end > info['duration'] + .01:
        raise VoxlateError('音色选段超出文件范围，请重新选择。')
    folder = work/'references'
    if not folder.resolve().is_relative_to(work):
        raise VoxlateError('参考音频目录指向项目外，请移除目录链接后重试。')
    work.mkdir(parents=True, exist_ok=True)
    with project_lock(work):
        folder.mkdir(exist_ok=True)
        temporary = folder/('.import-'+uuid.uuid4().hex+'.wav')
        try:
            Media(cfg).render(['-ss', str(start), '-i', source, '-t', str(end-start),
                '-map', '0:a:0', '-vn', '-ac', '1', '-ar', str(cfg['audio']['sample_rate']), '-c:a', 'pcm_s16le'], temporary)
            if not valid_wav(temporary):
                raise VoxlateError('未能提取有效的参考音频。')
            check_cancelled()
            key = file_hash(temporary)
            target = folder/('external-'+key+'.wav')
            if not target.exists() or file_hash(target) != key:
                temporary.replace(target)
            return dict(audio=str(target), key=key, label=source.name, start=start, end=end)
        finally:
            temporary.unlink(missing_ok=True)
            temporary.with_name(temporary.stem+'.partial.wav').unlink(missing_ok=True)


def checked_reference(project, work):
    data = project.get('external_voice')
    if not isinstance(data, dict) or not data.get('audio') or not data.get('key'):
        raise VoxlateError('请先打开外部音色文件并选择参考片段。')
    audio = Path(data['audio']).resolve()
    if not audio.is_relative_to(Path(work).resolve()) or not valid_wav(audio):
        raise VoxlateError('外部音色参考缺失或损坏，请重新打开。')
    with wave.open(str(audio)) as stream:
        duration = stream.getnframes()/stream.getframerate()
    if not .09 <= duration <= 15.01 or file_hash(audio) != data['key']:
        raise VoxlateError('外部音色参考已变化，请重新打开。')
    return audio


def export_role_reference(project, work, cfg, segment, destination):
    from .pipeline import VideoDubPipeline, project_lock
    from .project_storage import project_root
    work, destination = Path(work).resolve(), Path(destination).resolve()
    if destination.suffix.lower() != '.wav':
        raise VoxlateError('请保存为 WAV 音频。')
    if (destination == Path(project['input']).resolve() or destination.is_relative_to(work)
            or destination.is_relative_to(project_root(project['input']))):
        raise VoxlateError('请将音色导出到项目文件夹之外，避免覆盖项目音频。')
    scratch = work/'.temp'/'voice-exports'
    if not scratch.resolve().is_relative_to(work):
        raise VoxlateError('临时目录指向项目外，请移除目录链接后重试。')
    with project_lock(work):
        pipeline = VideoDubPipeline(cfg)
        pipeline.work, pipeline.project = work, project
        _, vocals, _ = pipeline.cached_media()
        temporary = scratch/(uuid.uuid4().hex+'.wav')
        try:
            pipeline.media.trim(vocals, temporary, segment['start'], min(15, segment['end']-segment['start']))
            check_cancelled()
            shutil.copyfile(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
            temporary.with_name(temporary.stem+'.partial.wav').unlink(missing_ok=True)
    return str(destination)
