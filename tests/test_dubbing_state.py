import unittest
from pathlib import Path

from test_pipeline import TestDirectory, tone
from voxlate.dubbing_state import select_voice_version, voice_key, remember_voice, sentence_ready


class VoiceVersions(unittest.TestCase):
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
