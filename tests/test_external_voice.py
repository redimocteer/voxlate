from pathlib import Path
import shutil
import unittest
import wave
from unittest.mock import patch

import test_pipeline
from test_pipeline import tone
from voxlate.common import VoxlateError, read_json, write_json
from voxlate.external_voice import import_reference, checked_reference, reference_info, export_role_reference
from voxlate.dubbing_state import voice_key


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg unavailable')
class ExternalVoiceTests(unittest.TestCase):
    def setUp(self):
        self.i = test_pipeline.PipelineIntegration()
        self.i.setUp()
        self.addCleanup(self.i.tearDown)
        self.audio = self.i.root/'参考音色.wav'
        tone(self.audio, 4)

    def test_audio_and_video_import_are_short_local_copies(self):
        for source in (self.audio, self.i.video):
            before = source.read_bytes()
            ref = import_reference(source, self.i.work, self.i.cfg, [.5, 2.5])
            audio = checked_reference(dict(external_voice=ref), self.i.work)
            self.assertTrue(audio.is_relative_to(self.i.work))
            self.assertEqual(ref['label'], source.name)
            self.assertEqual(source.read_bytes(), before)
            with wave.open(str(audio)) as stream:
                self.assertAlmostEqual(stream.getnframes()/stream.getframerate(), 2, places=2)
            self.assertEqual(import_reference(source, self.i.work, self.i.cfg, [.5, 2.5])['key'], ref['key'])
        self.assertFalse(reference_info(self.audio, self.i.cfg)['video'])
        self.assertTrue(reference_info(self.i.video, self.i.cfg)['video'])
        self.assertFalse(list((self.i.work/'references').glob('.import-*')))

    def test_external_reference_survives_mode_switches_and_changing_it_regenerates_audio(self):
        self.i.run_pipeline(stop_after='translate')
        ref = import_reference(self.audio, self.i.work, self.i.cfg, [0, 3])
        args = dict(stop_after='dub', require_translated=True)
        self.i.run_pipeline(**args, voice_mode='external', external_voice=ref)
        path = self.i.work/'project.json'
        project = read_json(path)
        first_audio = [s['tts_audio'] for s in project['segments']]
        self.assertTrue(all(s['speaker_reference_audio'] == ref['audio'] for s in project['segments']))
        self.assertEqual(project['voice_mode'], 'external')
        self.i.run_pipeline(**args, voice_mode='individual')
        calls = self.i.calls['tts']
        self.i.run_pipeline(**args, voice_mode='external')
        self.assertEqual(self.i.calls['tts'], calls)
        self.assertEqual([s['tts_audio'] for s in read_json(path)['segments']], first_audio)
        second = self.i.root/'other.wav'; tone(second, 4, frequency=650)
        new_ref = import_reference(second, self.i.work, self.i.cfg, [0, 3])
        self.assertNotEqual(voice_key(project, self.i.cfg), voice_key(dict(project, external_voice=new_ref), self.i.cfg))
        self.i.run_pipeline(**args, voice_mode='external', external_voice=new_ref)
        self.assertEqual(self.i.calls['tts'], calls+1)
        self.assertNotEqual([s['tts_audio'] for s in read_json(path)['segments']], first_audio)

    def test_bad_reference_and_cancelled_import_leave_existing_results(self):
        self.i.run_pipeline(stop_after='translate')
        ref = import_reference(self.audio, self.i.work, self.i.cfg, [0, 3])
        path = self.i.work/'project.json'; before = path.read_bytes()
        tone(ref['audio'], 3, frequency=800)
        with self.assertRaisesRegex(VoxlateError, '已变化'):
            self.i.run_pipeline(stop_after='dub', require_translated=True, voice_mode='external', external_voice=ref)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.i.calls['tts'], 0)
        with self.assertRaises(VoxlateError):
            checked_reference(dict(external_voice=dict(ref, audio=str(self.audio))), self.i.work)
        with patch('voxlate.external_voice.check_cancelled', side_effect=VoxlateError('已取消')):
            with self.assertRaisesRegex(VoxlateError, '已取消'):
                import_reference(self.audio, self.i.work, self.i.cfg, [0, 2])
        self.assertFalse(list((self.i.work/'references').glob('.import-*')))

    def test_exported_role_reference_can_be_reimported_and_cannot_overwrite_project(self):
        self.i.run_pipeline(stop_after='translate')
        project = read_json(self.i.work/'project.json')
        segment = project['segments'][0]
        target = self.i.video.parent/'exported-voice.wav'
        export_role_reference(project, self.i.work, self.i.cfg, segment, target)
        with wave.open(str(target)) as audio:
            self.assertAlmostEqual(audio.getnframes()/audio.getframerate(), segment['end']-segment['start'], places=2)
        ref = import_reference(target, self.i.work, self.i.cfg, [0, segment['end']-segment['start']])
        self.assertTrue(checked_reference(dict(external_voice=ref), self.i.work).is_file())
        with self.assertRaises(VoxlateError):
            export_role_reference(project, self.i.work, self.i.cfg, segment, self.i.video)
