"""Non-destructive, project-local manual segmentation and commit operations."""
import copy
import math
import time
import uuid
from pathlib import Path

from .common import VoxlateError, digest, read_json, write_json
from .media import check_cancelled

MIN_BLOCK = .1
MAX_SPAN = 600
MAX_BLOCKS = 600
MAX_FULL_SENTENCES = 5000


def interval_blocks(segments, start, end):
    """Keep gaps explicit instead of cutting them out of the timeline."""
    if end <= start:
        return []
    blocks, cursor = [], start
    for row in segments:
        if row['end'] <= start or row['start'] >= end:
            continue
        if row['start'] > cursor:
            blocks.append(dict(start=cursor, end=row['start'], enabled=False, text='', manual_text=False))
        blocks.append(dict(start=max(start, row['start']), end=min(end, row['end']),
            enabled=row.get('enabled', True), text=row['source_text'], manual_text=False))
        cursor = min(end, row['end'])
    if cursor < end:
        blocks.append(dict(start=cursor, end=end, enabled=False, text='', manual_text=False))
    return blocks


def validate_blocks(blocks, duration, *, full=False):
    limit = MAX_FULL_SENTENCES*2+1 if full else MAX_BLOCKS
    if not blocks or len(blocks) > limit:
        raise VoxlateError(f'一次请编辑 1～{limit} 个时间块。')
    if full and sum(not b.get('omit_row') for b in blocks) > MAX_FULL_SENTENCES:
        raise VoxlateError(f'全片最多编辑 {MAX_FULL_SENTENCES} 句。')
    previous = blocks[0]['start']
    for block in blocks:
        start, end = block['start'], block['end']
        if not all(type(t) in (int, float) and math.isfinite(t) for t in (start, end)):
            raise VoxlateError('切点时间无效。')
        if abs(start-previous) > 1e-6 or start < 0 or end <= start or end > duration+1e-6:
            raise VoxlateError('时间块须连续、无重叠且位于视频范围内。')
        if type(block.get('enabled')) is not bool or not isinstance(block.get('text', ''), str):
            raise VoxlateError('时间块内容无效。')
        if len(block.get('text', '')) > 1200:
            raise VoxlateError('单块原文最多 1200 字，请进一步分句。')
        if not isinstance(block.get('target_text',''),str) or len(block.get('target_text','')) > 1200:
            raise VoxlateError('单块译文最多 1200 字。')
        if full and not block.get('omit_row') and end-start > MAX_SPAN:
            raise VoxlateError('单句最多 10 分钟，请把这句再划短一些。')
        previous = end
    if not full and blocks[-1]['end']-blocks[0]['start'] > MAX_SPAN:
        raise VoxlateError('一次最多调整 10 分钟，请分段编辑。')


class SegmentPlan:
    def __init__(self, project, first, last, blocks=None):
        self.project = project
        rows = project['segments']
        self.blocks = copy.deepcopy(blocks) if blocks is not None else interval_blocks(
            rows, rows[first]['start'], rows[last]['end'])
        validate_blocks(self.blocks, project['duration'])
        self.undo_stack, self.redo_stack = [], []

    def remember(self, previous):
        if previous != self.blocks:
            self.undo_stack.append(previous)
            self.undo_stack = self.undo_stack[-100:]
            self.redo_stack.clear()

    def undo(self):
        if self.undo_stack:
            self.redo_stack.append(copy.deepcopy(self.blocks))
            self.blocks = self.undo_stack.pop()

    def redo(self):
        if self.redo_stack:
            self.undo_stack.append(copy.deepcopy(self.blocks))
            self.blocks = self.redo_stack.pop()

    def split(self, seconds):
        for index, block in enumerate(self.blocks):
            if block['start']+MIN_BLOCK <= seconds <= block['end']-MIN_BLOCK:
                if len(self.blocks) >= MAX_BLOCKS:
                    raise VoxlateError(f'最多 {MAX_BLOCKS} 个时间块。')
                previous = copy.deepcopy(self.blocks)
                left, right = copy.deepcopy(block), copy.deepcopy(block)
                left.update(end=round(seconds, 3), text='', manual_text=False)
                right.update(start=round(seconds, 3), text='', manual_text=False)
                self.blocks[index:index+1] = [left, right]
                self.remember(previous)
                return index
        return None

    def merge(self, cut):
        if not 0 < cut < len(self.blocks):
            return
        previous = copy.deepcopy(self.blocks)
        left, right = self.blocks[cut-1:cut+1]
        self.blocks[cut-1:cut+1] = [dict(start=left['start'], end=right['end'],
            enabled=left['enabled'] or right['enabled'], text='', manual_text=False)]
        self.remember(previous)

    def move_cut(self, cut, seconds):
        if not 0 < cut < len(self.blocks):
            return
        left, right = self.blocks[cut-1:cut+1]
        lower, upper = left['start']+MIN_BLOCK, right['end']-MIN_BLOCK
        if lower > upper:
            return
        seconds = round(max(lower, min(upper, seconds)), 3)
        left['end'] = right['start'] = seconds
        # Text copied from the old boundary is now only a hint for re-recognition.
        left['manual_text'] = right['manual_text'] = False

    def extend(self, start, end):
        old_start, old_end = self.blocks[0]['start'], self.blocks[-1]['end']
        start, end = max(0, min(old_start, start)), min(self.project['duration'], max(old_end, end))
        # Include whole neighboring sentences, never leave half a row outside.
        for row in self.project['segments']:
            if row['end'] > start and row['start'] < end:
                start, end = min(start, row['start']), max(end, row['end'])
        blocks = interval_blocks(self.project['segments'], start, old_start) + copy.deepcopy(self.blocks)
        blocks += interval_blocks(self.project['segments'], old_end, end)
        validate_blocks(blocks, self.project['duration'])
        previous, self.blocks = copy.deepcopy(self.blocks), blocks
        self.remember(previous)


def unchanged_segment(project, block):
    """Timing is the edit identity; unchanged rows keep all user content and takes."""
    if block.get('omit_row'):
        return None
    for row in project['segments']:
        if abs(row['start']-block['start']) < 1e-6 and abs(row['end']-block['end']) < 1e-6:
            if row.get('enabled', True) != block['enabled']:
                return None
            if block.get('manual_text') and block.get('text', '').strip() != row['source_text'].strip():
                return None
            if block.get('manual_translation') and block.get('target_text','').strip() != row.get('target_text','').strip():
                return None
            return row
    return None


def segmentation_needs_models(project, blocks):
    return any(b['enabled'] and unchanged_segment(project, b) is None and not (
        b.get('manual_text') and b.get('text','').strip() and
        b.get('manual_translation') and b.get('target_text','').strip()) for b in blocks)


def replace_segments(project, blocks, texts, translations, *, full=False):
    """Renumber display rows while retaining stable voice reference identities."""
    validate_blocks(blocks, project['duration'], full=full)
    if len(texts) != len(blocks) or len(translations) != len(blocks):
        raise VoxlateError('分句结果不完整，未修改项目。')
    start, end = blocks[0]['start'], blocks[-1]['end']
    old = project['segments']
    touched = [s for s in old if s['end'] > start and s['start'] < end]
    if any(s['start'] < start-1e-6 or s['end'] > end+1e-6 for s in touched):
        raise VoxlateError('编辑范围与邻句重叠，请将邻句一并纳入。')
    result = copy.deepcopy(project)
    before = [copy.deepcopy(s) for s in old if s['end'] <= start]
    after = [copy.deepcopy(s) for s in old if s['start'] >= end]
    mapping = {}
    new_rows = []
    default_role = (project.get('roles') or [{}])[0].get('id')
    for block, text, translated in zip(blocks, texts, translations):
        if not block['enabled'] and block.get('omit_row'):
            continue
        preserved = unchanged_segment(project, block)
        if preserved is not None and text == preserved['source_text'] and translated == preserved.get('target_text', ''):
            row = copy.deepcopy(preserved)
            row['_old_id'] = row['id']
            row.setdefault('voice_identity', row['id'])
            new_rows.append(row)
            continue
        if block['enabled'] and (not text.strip() or not translated.strip()):
            raise VoxlateError('有时间块未识别或翻译成功，未修改项目。')
        row = dict(start=block['start'], end=block['end'], enabled=block['enabled'],
            source_text=text.strip() or '（保留原声）', target_text=translated.strip(), speaker='A',
            source_lang=project.get('source_lang', 'en'), target_lang=project.get('target_lang', 'zh'), manual_boundary=True,
            voice_identity='manual-'+uuid.uuid4().hex)
        neighbors = [s for s in touched if s['end'] > row['start'] and s['start'] < row['end']]
        exact = next((s for s in neighbors if abs(s['start']-row['start']) < 1e-6 and abs(s['end']-row['end']) < 1e-6), None)
        if exact:
            row['voice_identity'] = exact.get('voice_identity', exact['id'])
            row['_old_id'] = exact['id']
        roles = {s.get('role_id') for s in neighbors if s.get('role_id')}
        if default_role:
            row['role_id'] = next(iter(roles)) if len(roles) == 1 else default_role
            row['role_uncertain'] = len(roles) != 1 or any(s.get('role_uncertain') for s in neighbors)
        new_rows.append(row)
    for row in before + after:
        row['_old_id'] = row['id']
        row.setdefault('voice_identity', row['id'])
    result['segments'] = before + new_rows + after
    for number, row in enumerate(result['segments'], 1):
        if (old_id := row.pop('_old_id', None)) is not None:
            mapping[old_id] = number
        row['id'] = number
    reference_changed = False
    for role in result.get('roles', []):
        old_id = role.get('reference_sentence_id')
        if old_id is not None:
            role['reference_sentence_id'] = mapping.get(old_id)
            reference_changed |= old_id not in mapping
    old_id = result.get('reference_sentence_id')
    if old_id is not None:
        result['reference_sentence_id'] = mapping.get(old_id)
        if old_id not in mapping:
            result['reference_auto_recommend'] = True
            reference_changed = True
    recommendation = result.pop('reference_recommendation', None)
    if recommendation:
        old_id = recommendation.get('sentence_id')
        if old_id in mapping:
            from .dubbing_state import recommended_reference
            result['reference_recommendation'] = recommended_reference(result, mapping[old_id])
        else:
            reference_changed = True
    for stage in ('timeline', 'mix', 'mux'):
        result.get('stages', {}).pop(stage, None)
    from .pipeline import validate_segments
    validate_segments(result['segments'], result['duration'])
    from .roles import ensure_roles, validate_roles
    if result.get('roles'):
        ensure_roles(result)
        validate_roles(result, allow_legacy_names=True)
    return result, reference_changed


def recognize_segment(project_path, expected_hash, cfg, start, end, *, use_original=True, runner=None, tts_session=None):
    """On-demand preview recognition: project-local files, no project mutation."""
    from .pipeline import VideoDubPipeline, project_lock
    from .project_storage import validate_project_directory
    path = Path(project_path)
    with project_lock(path.parent):
        project = read_json(path)
        if digest(project) != expected_hash:
            raise VoxlateError('项目已变化，请重新打开分句编辑器。')
        validate_project_directory(project['input'], path.parent)
        from .languages import direction
        if direction(project) != direction(cfg):
            raise VoxlateError('语言方向与项目不同，请重新打开对应项目。')
        if not 0 <= start < end <= project['duration'] or end-start > MAX_SPAN:
            raise VoxlateError('识别范围无效。')
        work = path.parent/'.temp'/'manual-preview'/uuid.uuid4().hex
        if not work.resolve().is_relative_to(path.parent.resolve()):
            raise VoxlateError('识别缓存目录指向项目外。')
        pipeline = VideoDubPipeline(cfg, runner=runner, tts_session=tts_session)
        pipeline.work, pipeline.project = path.parent, project
        pipeline.log_directory = path.parent
        original, vocals, _ = pipeline.cached_media()
        work.mkdir(parents=True)
        pipeline.work = work
        clip = work/'sentence.wav'
        pipeline.media.trim(original if use_original else vocals, clip, start, end-start)
        recognized = pipeline.runner('asr', dict(manual_blocks=[dict(index=0, audio=str(clip))]))
        check_cancelled()
        if not isinstance(recognized, list) or len(recognized) != 1 or not isinstance(recognized[0], str) or not recognized[0].strip():
            raise VoxlateError('此句未识别到文字，请试听并调整边界。')
        return recognized[0].strip()


def preview_translations(project_path, expected_hash, cfg, sentences, *, context=None,
                         use_original=True, runner=None, tts_session=None):
    """Recognize missing text and translate a selection without saving the project."""
    from .pipeline import VideoDubPipeline, project_lock
    from .project_storage import validate_project_directory
    path = Path(project_path)
    rows = copy.deepcopy(sentences)
    if not rows or len(rows) > MAX_FULL_SENTENCES:
        raise VoxlateError('请选择要翻译的句子。')
    with project_lock(path.parent):
        project = read_json(path)
        if digest(project) != expected_hash:
            raise VoxlateError('项目已变化，请重新打开分句编辑器。')
        validate_project_directory(project['input'], path.parent)
        for row in rows:
            start, end = row['start'], row['end']
            if (not all(type(t) in (int,float) and math.isfinite(t) for t in (start,end))
                    or not 0 <= start < end <= project['duration'] or end-start > MAX_SPAN):
                raise VoxlateError('翻译句子的时间范围无效。')
            if not isinstance(row.get('text'),str) or len(row['text']) > 1200:
                raise VoxlateError('单句原文最多 1200 字。')
        work = path.parent/'.temp'/'manual-preview'/uuid.uuid4().hex
        if not work.resolve().is_relative_to(path.parent.resolve()):
            raise VoxlateError('翻译缓存目录指向项目外。')
        work.mkdir(parents=True)
        pipeline = VideoDubPipeline(cfg, runner=runner, tts_session=tts_session)
        pipeline.work, pipeline.project = path.parent, project
        tracks = pipeline.cached_media() if any(not r['text'].strip() for r in rows) else None
        pipeline.work = work
        pipeline.log_directory = path.parent
        inputs = []
        for i, row in enumerate(rows):
            check_cancelled()
            if not row['text'].strip():
                clip = work/f'block-{i+1}.wav'
                pipeline.media.trim(tracks[0 if use_original else 1], clip, row['start'],row['end']-row['start'])
                inputs.append(dict(index=i,audio=str(clip)))
        if inputs:
            recognized = pipeline.runner('asr',dict(manual_blocks=inputs))
            if not isinstance(recognized,list) or len(recognized)!=len(inputs) or any(not isinstance(t,str) for t in recognized):
                raise VoxlateError('分块识别结果不完整，编辑内容保留。')
            for item,text in zip(inputs,recognized):
                rows[item['index']]['text'] = text.strip()
        active = [i for i,row in enumerate(rows) if row['text'].strip()]
        for row in rows:
            row['target_text'] = ''
        if active:
            background = list(context) if context is not None else [row['text'] for row in rows]
            indices = [rows[i].get('index',i) if context is not None else i for i in active]
            if any(type(i) is not int or not 0 <= i < len(background) for i in indices):
                raise VoxlateError('翻译选区无效。')
            for i,context_index in zip(active,indices):
                background[context_index] = rows[i]['text']
            translated = pipeline.runner('translator',dict(texts=[rows[i]['text'] for i in active],context=background,indices=indices))
            if (not isinstance(translated,list) or len(translated)!=len(active)
                    or any(not isinstance(t,str) or not t.strip() or len(t)>1200 for t in translated)):
                raise VoxlateError('翻译结果不完整，编辑内容保留。')
            for i,text in zip(active,translated):
                rows[i]['target_text'] = text.strip()
        check_cancelled()
        return rows


def apply_segmentation(project_path, expected_hash, cfg, blocks, *, use_original=True, runner=None, full=False):
    """Generate in a private work directory and commit only a complete result."""
    from .pipeline import VideoDubPipeline, project_lock, elapsed_text
    from .project_storage import validate_project_directory
    path = Path(project_path)
    blocks = copy.deepcopy(blocks)
    skipped = []
    started = time.perf_counter()
    with project_lock(path.parent):
        project = read_json(path)
        if digest(project) != expected_hash:
            raise VoxlateError('项目已变化，请重新打开分句编辑器。')
        validate_project_directory(project['input'], path.parent)
        from .languages import direction
        if direction(project) != direction(cfg):
            raise VoxlateError('语言方向与项目不同，请重新打开对应项目。')
        validate_blocks(blocks, project['duration'], full=full)
        for folder in (path.parent/'.temp', path.parent/'history'):
            if not folder.resolve().is_relative_to(path.parent.resolve()):
                raise VoxlateError('项目临时或备份目录指向项目外，请移除目录链接后重试。')
        start, end = blocks[0]['start'], blocks[-1]['end']
        if any(s['start'] < start-1e-6 or s['end'] > end+1e-6 for s in project['segments']
               if s['end'] > start and s['start'] < end):
            raise VoxlateError('编辑范围与邻句重叠，请将邻句一并纳入。')
        retained = {i: row for i, block in enumerate(blocks) if (row := unchanged_segment(project, block)) is not None}
        touched = [s for s in project['segments'] if s['end'] > start and s['start'] < end]
        check_cancelled()
        if len(retained) == len(touched) and all(i in retained or b.get('omit_row') for i, b in enumerate(blocks)):
            try:
                (path.parent/'.temp'/('segmentation-full-draft.json' if full else 'segmentation-draft.json')).unlink(missing_ok=True)
            except OSError:
                pass
            return dict(reference_changed=False, sentences=sum(b['enabled'] for b in blocks),
                        updated=0, reused=len(retained), removed=0, unchanged=True, skipped=[], backup=None)
        pipeline = VideoDubPipeline(cfg, runner=runner)
        pipeline.work, pipeline.project = path.parent, project
        pipeline.log_directory = path.parent
        tracks = None
        work = path.parent/'.temp'/'manual-segments'/uuid.uuid4().hex
        work.mkdir(parents=True)
        pipeline.work = work
        inputs, texts, translations = [], ['']*len(blocks), ['']*len(blocks)
        for index, block in enumerate(blocks):
            check_cancelled()
            if block.get('manual_translation'):
                translations[index] = block.get('target_text','').strip()
            if index in retained:
                texts[index] = retained[index]['source_text']
                translations[index] = retained[index].get('target_text', '')
            elif not block['enabled']:
                texts[index] = block.get('text', '')
            elif block.get('manual_text') and block.get('text', '').strip():
                texts[index] = block['text'].strip()
            else:
                if tracks is None:
                    pipeline.work = path.parent
                    tracks = pipeline.cached_media()
                    pipeline.work = work
                clip = work/f'block-{index+1}.wav'
                pipeline.media.trim(tracks[0 if use_original else 1], clip, block['start'], block['end']-block['start'])
                inputs.append(dict(index=index, audio=str(clip)))
        if inputs:
            recognized = pipeline.runner('asr', dict(manual_blocks=inputs))
            if not isinstance(recognized, list) or len(recognized) != len(inputs):
                raise VoxlateError('分块识别结果不完整，原项目保留。')
            for item, text in zip(inputs, recognized):
                if not isinstance(text, str):
                    raise VoxlateError('分块识别结果格式无效，原项目保留。')
                if not text.strip():
                    block = blocks[item['index']]
                    skipped.append(dict(start=block['start'], end=block['end']))
                    block.update(enabled=False, omit_row=True, text='', manual_text=False)
                    continue
                texts[item['index']] = text.strip()
        active = [i for i, b in enumerate(blocks) if b['enabled']]
        changed = [i for i in active if i not in retained]
        pending = [i for i in changed if not translations[i]]
        if pending:
            start, end = blocks[0]['start'], blocks[-1]['end']
            preceding = [s['source_text'] for s in project['segments'] if s['end'] <= start][-2:]
            following = [s['source_text'] for s in project['segments'] if s['start'] >= end][:2]
            context = preceding + [texts[i] for i in active] + following
            translated = pipeline.runner('translator', dict(texts=[texts[i] for i in pending], context=context,
                indices=[len(preceding)+active.index(i) for i in pending]))
            if not isinstance(translated, list) or len(translated) != len(pending) or any(not isinstance(t, str) or not t.strip() for t in translated):
                raise VoxlateError('分块翻译不完整，原项目保留。')
            for index, text in zip(pending, translated):
                translations[index] = text
        result, reference_changed = replace_segments(project, blocks, texts, translations, full=full)
        if changed:
            result['translation_config_key'] = digest(pipeline.cfg['translator'])
        backup = path.parent/'history'/'manual-segments'/work.name/'project.json'
        write_json(backup, project)
        result.setdefault('manual_edits', []).append(dict(backup=str(backup), start=blocks[0]['start'], end=blocks[-1]['end']))
        check_cancelled()
        if digest(read_json(path)) != expected_hash:
            raise VoxlateError('项目已变化，分句结果未覆盖当前项目。')
        write_json(path, result)
        pipeline.work = path.parent
        deleted = sum(not any(not b.get('omit_row') and b['start'] < row['end'] and b['end'] > row['start']
                              for b in blocks) for row in touched)
        details = [f'{label} {count} 句' for label, count in
                   (('保留', len(retained)), ('更新', len(changed)), ('删除', deleted)) if count]
        if skipped:
            details.append(f'{len(skipped)} 段无文字，保留原声')
        elapsed = time.perf_counter()-started
        duration = elapsed_text(elapsed) if elapsed >= 1 else '<1 秒'
        message = f"分句完成：{'，'.join(details)} · 耗时 {duration}"
        pipeline.record_elapsed(message)
        try:
            (path.parent/'.temp'/('segmentation-full-draft.json' if full else 'segmentation-draft.json')).unlink(missing_ok=True)
        except OSError:
            pass  # A stale draft is ignored because its project hash no longer matches.
        return dict(reference_changed=reference_changed, sentences=len(active), updated=len(changed), reused=len(retained),
                    removed=len(touched)-len(retained), unchanged=False, skipped=skipped, backup=str(backup), message=message)
