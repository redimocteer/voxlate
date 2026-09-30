import copy
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch

import test_pipeline as fixtures
from voxlate.common import VoxlateError, digest, file_hash, read_json, write_json
from voxlate.languages import default_output
from voxlate.project_storage import project_root, relocate_saved_project, relocated_project
from voxlate.pipeline import project_lock, VideoDubPipeline


class ProjectRelocationTests(unittest.TestCase):
    def setUp(self):
        self.directory = fixtures.TestDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)
        self.old = self.base / 'before' / 'synthetic.AVI'
        self.old.parent.mkdir()
        self.old.write_bytes(b'synthetic video identity')
        self.old_root = project_root(self.old)
        self.profile = self.old_root / 'ja-qwen3-asr-1.7b-audio-2'
        self.profile.mkdir(parents=True)
        self.audio = self.profile / 'cache' / 'clip.wav'
        fixtures.tone(self.audio, .1)
        self.external = self.base / 'external.wav'
        self.project = dict(schema_version=1, name='voxlate', input=str(self.old),
            input_hash=file_hash(self.old), source_lang='ja', target_lang='zh', audio_track=1,
            output=str(default_output(self.old, dict(source_lang='ja', audio_track=1))),
            speaker_reference=str(self.external), duration=1, stages={'asr': {'key':'keep'}},
            segments=[dict(id=1, start=0, end=1, source_text=str(self.audio), target_text='synthetic',
                source_audio=str(self.audio), tts_audio=str(self.audio), aligned_audio=str(self.audio),
                speaker_reference_audio=str(self.audio), tts_key='keep', voice_versions={
                    'individual': {'tts_audio':str(self.audio), 'speaker_reference_audio':str(self.audio)}})],
            manual_edits=[{'backup':str(self.profile/'backups'/'before.json')}])
        write_json(self.profile/'project.json', self.project)
        (self.profile/'qwen_resume.json').write_bytes(b'synthetic checkpoint, unchanged')
        self.new = self.base / 'after 日本 & [1]' / self.old.name
        self.new.parent.mkdir()
        self.old.rename(self.new)
        self.new_root = project_root(self.new)
        shutil.move(self.old_root, self.new_root)
        self.path = self.new_root / self.profile.name / 'project.json'

    def test_rebases_all_voice_versions_preserves_text_keys_external_paths_and_checkpoints(self):
        before = copy.deepcopy(self.project)
        result = relocate_saved_project(self.project, self.path)
        audio = self.path.parent/'cache'/'clip.wav'
        self.assertEqual(result['input'], str(self.new))
        self.assertEqual(result['output'], str(default_output(self.new, result)))
        self.assertEqual(result['speaker_reference'], str(self.external))
        segment = result['segments'][0]
        for key in ('source_audio', 'tts_audio', 'aligned_audio', 'speaker_reference_audio'):
            self.assertEqual(segment[key], str(audio))
            self.assertTrue(Path(segment[key]).is_file())
        self.assertEqual(segment['voice_versions']['individual']['tts_audio'], str(audio))
        self.assertEqual(segment['source_text'], str(self.audio))
        self.assertEqual(segment['tts_key'], 'keep')
        self.assertEqual(result['stages'], before['stages'])
        self.assertEqual(result['manual_edits'][0]['backup'], str(self.path.parent/'backups'/'before.json'))
        self.assertEqual(self.project, before)
        self.assertEqual(read_json(self.path), result)
        self.assertEqual((self.path.parent/'qwen_resume.json').read_bytes(), b'synthetic checkpoint, unchanged')
        with patch('voxlate.project_storage.file_hash', side_effect=AssertionError('unneeded hash')):
            self.assertIs(relocate_saved_project(result, self.path), result)

    def test_wrong_or_missing_video_never_changes_saved_project(self):
        before = self.path.read_bytes()
        self.new.write_bytes(b'different same-name video')
        with self.assertRaisesRegex(VoxlateError, '不匹配'):
            relocate_saved_project(self.project, self.path)
        self.assertEqual(self.path.read_bytes(), before)
        self.new.unlink()
        with self.assertRaisesRegex(VoxlateError, '放在一起'):
            relocate_saved_project(self.project, self.path)
        self.assertEqual(self.path.read_bytes(), before)

    def test_concurrent_edits_and_active_projects_are_not_overwritten(self):
        with project_lock(self.path.parent):
            with self.assertRaisesRegex(VoxlateError, '正在使用'):
                relocate_saved_project(self.project, self.path)
        modified = dict(self.project, custom='changed')
        write_json(self.path, modified)
        with self.assertRaisesRegex(VoxlateError, '其他操作'):
            relocate_saved_project(self.project, self.path)
        self.assertEqual(read_json(self.path), modified)

    def test_copy_prefers_new_location_even_when_original_still_exists(self):
        self.old.write_bytes(self.new.read_bytes())
        custom_output = self.base/'custom-export.mp4'
        self.project['output'] = str(custom_output)
        result = relocated_project(self.project, self.path)
        self.assertEqual(result['input'], str(self.new))
        self.assertEqual(result['output'], str(custom_output))

    def test_unrelated_layout_is_not_relocated(self):
        self.assertIs(relocated_project(self.project, self.base/'unrelated'/'project.json'), self.project)

    def test_pipeline_exports_moved_project_without_reloading_models(self):
        fixture = fixtures.PipelineIntegration()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        fixture.work = project_root(fixture.video)/'en-combined'
        fixture.run_pipeline(voice_mode='individual')
        old = read_json(fixture.work/'project.json')
        destination = fixture.root/'moved'
        destination.mkdir()
        moved_video = destination/fixture.video.name
        fixture.video.rename(moved_video)
        moved_root = project_root(moved_video)
        shutil.move(project_root(fixture.video), moved_root)
        work = moved_root/fixture.work.name
        def unexpected(*args, **kwargs):
            self.fail('moving the project must not rerun models')
        pipeline = VideoDubPipeline(fixture.cfg, unexpected)
        pipeline.process(moved_video, destination/'result.mp4', work, export_only=True)
        result = read_json(work/'project.json')
        self.assertEqual(result['input'], str(moved_video))
        self.assertTrue((destination/'result.mp4').is_file())
        self.assertEqual([s['tts_key'] for s in old['segments']], [s['tts_key'] for s in result['segments']])
