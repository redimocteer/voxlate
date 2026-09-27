"""Cheap UI checks for whether a sentence's saved audio matches its settings."""
from pathlib import Path
from .common import digest


def automatic_reference(project):
    project = project or {}
    return project.get('reference_auto_recommend',
                       project.get('reference_auto_longest', project.get('reference_sentence_id') is None))


def reference_input_key(project):
    return digest('recommend-v1', project.get('stages', {}).get('separate', {}).get('key'),
                  [(s['id'], s['start'], s['end']) for s in project.get('segments', [])])


def recommended_reference(project, ident):
    selected = next(s for s in project['segments'] if s['id'] == ident)
    return dict(sentence_id=ident, start=selected['start'], end=selected['end'],
                separation_key=project.get('stages', {}).get('separate', {}).get('key'),
                input_key=reference_input_key(project))


def voice_selection(project):
    segments = (project or {}).get('segments', [])
    mode = (project or {}).get('voice_mode', 'uniform')
    mode = mode if mode in ('uniform', 'individual', 'roles') else 'uniform'
    ident = (project or {}).get('reference_sentence_id')
    if automatic_reference(project):
        saved = (project or {}).get('reference_recommendation', {})
        selected = next((s for s in segments if s['id'] == saved.get('sentence_id')), None)
        # Table painting calls this per row. Validate the chosen clip cheaply;
        # the pipeline checks all segment timings before reusing analysis.
        valid = selected is not None and saved.get('start') == selected['start'] and saved.get('end') == selected['end']
        valid = valid and saved.get('separation_key') == (project or {}).get('stages', {}).get('separate', {}).get('key')
        ident = selected['id'] if valid else None
    elif segments and ident not in {s['id'] for s in segments}:
        ident = max(segments, key=lambda s: s['end']-s['start'])['id']
    return mode, ident if segments else None


def voice_key(project, cfg, segment=None):
    if 'voice_mode' in project:
        mode, ident = voice_selection(project)
        if mode == 'roles':
            from .roles import reference_segment
            if segment is None:
                return digest([voice_key(project, cfg, s) for s in project.get('segments', [])])
            role_id = segment.get('role_id')
            selected = reference_segment(project, role_id)
            return digest(cfg['tts'], mode, role_id, selected.get('voice_identity', selected['id']), selected['start'], selected['end'])
        selected = next((s for s in project.get('segments', []) if s['id'] == ident), None)
        return digest(cfg['tts'], mode,
                      (selected.get('voice_identity', ident), selected['start'], selected['end']) if mode == 'uniform' and selected else None)
    reference = project.get('speaker_reference', '')
    fingerprint = None
    if reference:
        try:
            stat = Path(reference).stat()
            fingerprint = (stat.st_size, stat.st_mtime_ns)
        except OSError:
            fingerprint = 'missing'
    return digest(cfg['tts'], reference, fingerprint, project.get('reference_range'),
                  project.get('auto_reference', not reference and not project.get('reference_range')))


def audio_exists(path):
    try:
        return bool(path) and Path(path).is_file() and Path(path).stat().st_size > 44
    except OSError:
        return False


VOICE_FIELDS = ('tts_audio', 'tts_key', 'tts_take', 'aligned_audio', 'tts_text',
                'timing_text', 'generated_duration', 'translation_too_long',
                'voice_key', 'speaker_reference_audio')


def remember_voice(segment):
    """Archive only fully generated audio, never an interrupted request."""
    key = segment.get('voice_key')
    if key and audio_exists(segment.get('tts_audio')):
        segment.setdefault('voice_versions', {})[key] = {
            field: segment[field] for field in VOICE_FIELDS if field in segment}


def select_voice_version(project, cfg):
    """Switch active pointers without deleting another voice's audio files."""
    for segment in project.get('segments', []):
        context = voice_key(project, cfg, segment)
        remember_voice(segment)
        if segment.get('voice_key') == context:
            continue
        saved = segment.get('voice_versions', {}).get(context, {})
        if not saved and not segment.get('voice_key') and not segment.get('tts_audio'):
            continue  # Legacy timing-only records are not a different voice take.
        # An unfinished request may still have reusable files; let the pipeline
        # rediscover them by its content hash, without presenting them as ready.
        for field in VOICE_FIELDS:
            segment.pop(field, None)
        segment.update(saved)


def sentence_ready(segment, context):
    return (audio_exists(segment.get('tts_audio'))
            and segment.get('tts_text') == segment.get('target_text', '').strip()
            and segment.get('voice_key') == context)


def dubbing_ready(project, cfg):
    if not project or not project.get('segments'):
        return False
    return all(sentence_ready(s, voice_key(project, cfg, s)) for s in project['segments'] if s.get('enabled', True))
