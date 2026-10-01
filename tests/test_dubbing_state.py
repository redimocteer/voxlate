import unittest
from pathlib import Path
from unittest.mock import patch

from test_pipeline import TestDirectory, tone
from voxlate.dubbing_state import select_voice_version, voice_key, voice_contexts, remember_voice, sentence_ready


class VoiceVersions(unittest.TestCase):
    def test_contexts_match_individual_keys_with_one_scan_per_voice(self):
        cfg = {'tts': {}}
        rows = [dict(id=i, start=i*2, end=i*2+1, role_id='a' if i%2 else 'b') for i in range(1,101)]
        project = dict(segments=rows, reference_sentence_id=1, reference_auto_recommend=False,
                       roles=[dict(id='a', name='角色A', reference_sentence_id=1),
                              dict(id='b', name='角色B', reference_sentence_id=2)])
        for mode in (None, 'uniform', 'individual', 'roles'):
            with self.subTest(mode=mode):
                if mode is not None:
                    project['voice_mode'] = mode
                expected = {row['id']: voice_key(project, cfg, row) for row in rows}
                with patch('voxlate.dubbing_state.voice_key', wraps=voice_key) as resolve:
                    self.assertEqual(voice_contexts(project, cfg), expected)
                    self.assertEqual(resolve.call_count, 2 if mode == 'roles' else 1)

    def test_single_reference_overrides_mode_and_has_its_own_cache_key(self):
        cfg = {'tts': {}}
        project = dict(voice_mode='individual', segments=[dict(id=1,start=0,end=1),dict(id=2,start=2,end=3)])
        row = project['segments'][0]
        original = voice_key(project,cfg,row)
        row['voice_reference_sentence_id'] = 2
        custom = voice_key(project,cfg,row)
        self.assertNotEqual(custom,original)
        self.assertEqual(voice_contexts(project,cfg)[1],custom)
        self.assertEqual(voice_contexts(project,cfg)[2],original)
        project['voice_mode'] = 'uniform'
        self.assertEqual(voice_key(project,cfg,row),custom)
        project['segments'][1]['end'] = 3.5
        self.assertNotEqual(voice_key(project,cfg,row),custom)


    def test_versions_preserve_takes_and_restore_without_generation(self):
        with TestDirectory() as folder:
            audio = Path(folder)/'voice.wav'
            tone(audio, .1)
            cfg = {'tts': {'emotion_reference': True}}
            segment = dict(id=1, start=0, end=1, target_text='你好')
            project = dict(voice_mode='uniform', reference_sentence_id=1, segments=[segment])
            uniform = voice_key(project, cfg)
            segment.update(tts_audio=str(audio), tts_text='你好', voice_key=uniform, tts_take='uniform-take')
            project['voice_mode'] = 'individual'
            select_voice_version(project, cfg)
            self.assertNotIn('tts_audio', segment)
            individual = voice_key(project, cfg)
            segment.update(tts_audio=str(audio), tts_text='你好', voice_key=individual, tts_take='individual-take')
            project['voice_mode'] = 'uniform'
            select_voice_version(project, cfg)
            self.assertEqual(segment['tts_take'], 'uniform-take')
            self.assertTrue(sentence_ready(segment, uniform))
            segment['target_text'] = '您好'
            self.assertFalse(sentence_ready(segment, uniform))
            project['voice_mode'] = 'individual'
            select_voice_version(project, cfg)
            self.assertEqual(segment['tts_take'], 'individual-take')
            self.assertTrue(audio.exists())

    def test_incomplete_take_cannot_replace_completed_version(self):
        with TestDirectory() as folder:
            audio = Path(folder)/'voice.wav'
            tone(audio, .1)
            cfg = {'tts': {}}
            segment = dict(id=1, start=0, end=1, target_text='你好')
            project = dict(voice_mode='uniform', segments=[segment])
            context = voice_key(project, cfg)
            segment.update(tts_audio=str(audio), tts_text='你好', voice_key=context, tts_take='complete')
            remember_voice(segment)
            segment.pop('voice_key')
            segment.update(tts_take='unfinished', tts_audio=str(Path(folder)/'missing.wav'))
            select_voice_version(project, cfg)
            self.assertEqual(segment['tts_take'], 'complete')
            self.assertTrue(sentence_ready(segment, context))
