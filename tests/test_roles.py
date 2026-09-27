import copy
from pathlib import Path
import unittest
from voxlate.common import VoxlateError
from voxlate.roles import ensure_roles, validate_roles, reference_segment, automatic_role_name, apply_automatic_voice_mode
from voxlate.dubbing_state import voice_key, select_voice_version, sentence_ready
from voxlate.speaker_groups import group_records
from test_pipeline import TestDirectory, tone


def fixture():
    return dict(voice_mode='roles', roles=[
        dict(id='a', name='角色 A', reference_sentence_id=1),
        dict(id='b', name='角色 B', reference_sentence_id=3)], segments=[
        dict(id=1, role_id='a', start=0, end=2, target_text='你好'),
        dict(id=2, role_id='a', start=3, end=6, target_text='再见'),
        dict(id=3, role_id='b', start=7, end=9, target_text='谢谢')])


class RoleTests(unittest.TestCase):
    def test_many_roles_preserve_role_and_individual_modes(self):
        for mode in ('roles', 'individual'):
            with self.subTest(mode=mode):
                project = dict(voice_mode=mode)
                self.assertFalse(apply_automatic_voice_mode(project, 6))
                self.assertEqual(project, dict(voice_mode=mode))

    def test_many_roles_switch_once_and_respect_later_manual_mode(self):
        project = dict(voice_mode='uniform')
        self.assertFalse(apply_automatic_voice_mode(project,5))
        self.assertEqual(project['voice_mode'],'uniform')
        self.assertTrue(apply_automatic_voice_mode(project,6))
        self.assertEqual(project['voice_mode'],'individual')
        project['voice_mode']='uniform'
        self.assertFalse(apply_automatic_voice_mode(project,12))
        self.assertEqual(project['voice_mode'],'uniform')

    def test_auto_reference_prefers_dominant_speaker_and_clear_representative(self):
        records = [dict(id=1,start=0,end=20,embedding=[0.,1.],quality=.8,voiced_fraction=.5),
                   dict(id=2,start=21,end=27,embedding=[1.,0.],quality=.95,voiced_fraction=1),
                   dict(id=3,start=28,end=36,embedding=[1.,0.],quality=.6,voiced_fraction=1),
                   dict(id=4,start=37,end=46,embedding=[1.,0.],quality=1,unstable=True,voiced_fraction=1)]
        result = group_records(records)
        self.assertEqual(result['recommended_sentence_id'], 2)
        # The longest clip belongs to a different, less prominent speaker;
        # the unstable clip must not become the reference despite its score.

    def test_auto_selection_invalidates_with_timing_but_not_translation(self):
        from voxlate.dubbing_state import voice_selection, recommended_reference
        project = fixture()
        project.update(voice_mode='uniform', reference_auto_recommend=True,
                       reference_recommendation=recommended_reference(project, 1))
        self.assertEqual(voice_selection(project), ('uniform', 1))
        project['segments'][0]['target_text'] = '新译文'
        self.assertEqual(voice_selection(project), ('uniform', 1))
        project['segments'][0]['end'] = 2.5
        self.assertEqual(voice_selection(project), ('uniform', None))
        project.update(reference_auto_recommend=False, reference_sentence_id=3)
        self.assertEqual(voice_selection(project), ('uniform', 3))

    def test_role_count_and_automatic_names_at_fifty(self):
        self.assertEqual([automatic_role_name(i) for i in (0,25,26,49)],['角色A','角色Z','角色AA','角色AX'])
        project=fixture()
        project['roles'].extend(dict(id=f'role{i}',name=automatic_role_name(i),reference_sentence_id=1) for i in range(2,50))
        validate_roles(project)
        project['roles'].append(dict(id='extra',name='额外',reference_sentence_id=1))
        with self.assertRaisesRegex(VoxlateError,'50'): validate_roles(project)
    def test_names_and_assignments_are_validated(self):
        validate_roles(fixture())
        for name in ('', 'x'*9, '角色 B', '../x', 'a\nb'):
            project = fixture()
            project['roles'][0]['name'] = name
            with self.assertRaises(VoxlateError):
                validate_roles(project)
        project = fixture()
        project['segments'][0]['role_id'] = 'missing'
        with self.assertRaises(VoxlateError):
            validate_roles(project)

    def test_explicit_reference_and_longest_fallback(self):
        project = fixture()
        self.assertEqual(reference_segment(project, 'a')['id'], 1)
        project['roles'][0]['reference_sentence_id'] = None
        self.assertEqual(reference_segment(project, 'a')['id'], 2)

    def test_legacy_names_can_be_read_without_changing_voice_keys(self):
        project=fixture()
        original=voice_key(project,{'tts':{}},project['segments'][0])
        ensure_roles(project)
        self.assertEqual(project['roles'][0]['name'],'角色A')
        self.assertEqual(voice_key(project,{'tts':{}},project['segments'][0]),original)
        project['roles'][0]['name']='long name'
        validate_roles(project,allow_legacy_names=True)
        with self.assertRaises(VoxlateError): validate_roles(project)

    def test_rename_preserves_audio_assignment_and_reference_only_affect_members(self):
        project, cfg = fixture(), {'tts': {'emotion_reference': True}}
        with TestDirectory() as directory:
            audio = Path(directory)/'audio.wav'
            tone(audio, .1)
            for segment in project['segments']:
                segment.update(tts_audio=str(audio), tts_text=segment['target_text'],
                    voice_key=voice_key(project, cfg, segment))
            original = copy.deepcopy(project)
            project['roles'][0]['name'] = '改名'
            select_voice_version(project, cfg)
            self.assertTrue(all(sentence_ready(s, voice_key(project,cfg,s)) for s in project['segments']))
            project['segments'][1]['role_id'] = 'b'
            select_voice_version(project, cfg)
            self.assertEqual([sentence_ready(s, voice_key(project,cfg,s)) for s in project['segments']], [True,False,True])
            project['segments'][1]['role_id'] = 'a'
            select_voice_version(project, cfg)
            self.assertTrue(all(sentence_ready(s, voice_key(project,cfg,s)) for s in project['segments']))
            project['roles'][0]['reference_sentence_id'] = 2
            select_voice_version(project,cfg)
            self.assertEqual([sentence_ready(s, voice_key(project,cfg,s)) for s in project['segments']], [False,False,True])
            self.assertTrue(audio.exists())

    def test_grouping_separates_distinct_voices_and_flags_short_utterance(self):
        records = [dict(id=1,start=0,end=3,embedding=[1.,0],quality=.95),
            dict(id=2,start=4,end=10,embedding=[1.,0],quality=.6),
            dict(id=3,start=11,end=14,embedding=[0,1.],quality=.9),
            dict(id=4,start=15,end=15.3,embedding=[0,1.],quality=.2)]
        result = group_records(records)
        self.assertEqual(len(result['roles']), 2)
        self.assertEqual(result['roles'][0]['reference_sentence_id'], 1)
        self.assertEqual(result['assignments']['1']['role_id'], result['assignments']['2']['role_id'])
        self.assertTrue(result['assignments']['4']['role_uncertain'])
        self.assertNotEqual(result['assignments']['1']['role_id'],result['assignments']['3']['role_id'])

    def test_silence_has_manual_recovery_error(self):
        with self.assertRaisesRegex(VoxlateError, '手动'):
            group_records([dict(id=1,start=0,end=2,embedding=None)])
