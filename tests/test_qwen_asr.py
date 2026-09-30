from types import SimpleNamespace as Word
from pathlib import Path
import unittest
from test_pipeline import TestDirectory
from voxlate.app_settings import prepare_settings
from voxlate.common import load_config, VoxlateError
from voxlate.qwen_recognition import aligned_segments, recognize_chunk, next_chunk_end, protect_uncertain_rows
from unittest.mock import Mock, patch
from voxlate.recognition_models import select_model, required_model
from voxlate.installer import resource_root, planned_stages
from voxlate.diagnostics import check_resources
from voxlate.project_storage import default_project_directory
from voxlate.runtime import worker_command


class QwenTests(unittest.TestCase):
    def test_sparse_zero_timings_preserve_original_instead_of_fake_span(self):
        words = [Word(text='Come', start_time=0, end_time=0), Word(text='back', start_time=12, end_time=12)]
        rows = aligned_segments('Come back!', words, 50, 15, 'en')
        protect_uncertain_rows(rows)
        self.assertFalse(rows[0]['enabled'])
        self.assertTrue(rows[0]['auto_preserve_original'])
        self.assertEqual(rows[0]['source_text'], 'Come back!')
        self.assertEqual(rows[0]['words'][1]['start'], 62)

    def test_conservative_policy_rejects_stretched_words_and_heavy_zero_timings(self):
        for text, words, duration in [
                ('Hey!', [Word(text='Hey', start_time=0, end_time=18)], 20),
                ('One two three four.', [Word(text=t,start_time=i*.2,end_time=i*.2+(0 if i<3 else .2))
                    for i,t in enumerate(('One','two','three','four'))], 2)]:
            with self.subTest(text=text):
                row = protect_uncertain_rows(aligned_segments(text, words, 0, duration, 'en'))[0]
                self.assertFalse(row['enabled'])
                self.assertTrue(row['recognition_warning'])

    def test_isolated_zero_word_and_normal_multilingual_speech_stay_available(self):
        for text, words, language in [
                ('Come back.', [Word(text='Come',start_time=0,end_time=0),Word(text='back',start_time=.05,end_time=.4)],'en'),
                ('你好。', [Word(text='你',start_time=0,end_time=.2),Word(text='好',start_time=.2,end_time=.5)],'zh'),
                ('はい。', [Word(text='はい',start_time=0,end_time=.5)],'ja')]:
            row = protect_uncertain_rows(aligned_segments(text,words,0,1,language))[0]
            self.assertTrue(row.get('enabled',True))
            self.assertNotIn('auto_preserve_original',row)

    def test_time_limited_result_is_never_accepted_as_complete(self):
        model = Mock()
        model.transcribe.return_value = [Word(text='Incomplete text')]
        with patch('voxlate.qwen_recognition.time.monotonic', side_effect=[0, 45.01]):
            with self.assertRaises(TimeoutError):
                recognize_chunk(model, [], 'English', 45)

    def test_short_chunk_uses_quiet_region_and_preserves_final_tail(self):
        import numpy as np
        source = Mock(samplerate=100, frames=3000)
        samples = np.ones((400, 2), dtype=np.float32)
        samples[100:120] = 0
        source.read.return_value = samples
        self.assertEqual(next_chunk_end(source, 0), 910)
        self.assertEqual(next_chunk_end(source, 2500), 3000)

    def test_preserves_english_spelling_punctuation_and_offsets(self):
        words = [Word(text=t, start_time=i*.5, end_time=i*.5+.4)
                 for i,t in enumerate(['Hello', "I'm", 'Anna', 'Come', 'here'])]
        rows = aligned_segments('Hello! I’m Anna. Come here?', words, 60, 3, 'en')
        self.assertEqual([r['source_text'] for r in rows], ['Hello!', 'I’m Anna.', 'Come here?'])
        self.assertEqual(rows[0]['start'],60)
        self.assertAlmostEqual(rows[-1]['end'],62.4)
        self.assertTrue(all(a['end'] <= b['start'] for a,b in zip(rows, rows[1:])))

    def test_japanese_is_not_dropped_or_space_inserted(self):
        words = [Word(text=t,start_time=i*.3,end_time=i*.3+.25)
                 for i,t in enumerate(['これ','は','何','です','か','そう','です'])]
        rows=aligned_segments('これは何ですか？そうです。',words,0,3,'ja')
        self.assertEqual([r['source_text'] for r in rows], ['これは何ですか？','そうです。'])

    def test_refuses_incomplete_text_and_invalid_timestamps(self):
        for token,start,end in [('there',0,.3),('hello',0,float('nan')),('hello',-1,.3),('hello',0,5)]:
            with self.subTest(token=token,start=start,end=end),self.assertRaises(VoxlateError):
                aligned_segments('hello',[Word(text=token,start_time=start,end_time=end)],0,1,'en')
        with self.assertRaises(VoxlateError):
            aligned_segments('hello again',[Word(text='hello',start_time=0,end_time=.3)],0,1,'en')

    def test_zero_duration_interjection_is_retained_for_review(self):
        words=[Word(text='And',start_time=.5,end_time=.5),Word(text='now',start_time=2,end_time=2.4)]
        rows=aligned_segments('And... now!',words,0,3,'en')
        self.assertEqual(rows[0]['source_text'],'And... now!')
        self.assertTrue(rows[0]['timing_uncertain'])

    def test_abbreviation_period_does_not_end_a_sentence(self):
        words=[Word(text='Mr',start_time=0,end_time=.4),Word(text='Smith',start_time=.4,end_time=.8)]
        rows=aligned_segments('Mr. Smith.',words,0,1,'en')
        self.assertEqual([r['source_text'] for r in rows],['Mr. Smith.'])

    def test_qwen_uses_own_environment_and_project_but_not_whisper_combination(self):
        with TestDirectory() as folder:
            config,_=prepare_settings(folder)
            cfg=load_config(config)
            root=resource_root(cfg)
            select_model(cfg,'asr_qwen_model',root)
            self.assertFalse(cfg['asr']['combined'])
            self.assertIn('.venv-qwen',worker_command(cfg,'asr',Path(folder)/'job.json')[0])
            self.assertTrue(default_project_directory(Path(folder)/'sample.mp4',cfg).name.endswith('qwen3-asr-1.7b'))
            stages=planned_stages(check_resources(cfg,config,quick=True))
            self.assertIn('qwen',stages)
            self.assertIn('asr_qwen_model',stages)
            self.assertNotIn('asr_large_model',stages)
            cfg['asr']['combined']=True
            self.assertEqual(worker_command(cfg,'asr',Path(folder)/'job.json')[0],cfg['runtime']['python'])
            self.assertFalse(required_model(cfg,'asr_qwen_model'))
            self.assertTrue(required_model(cfg,'asr_large_model'))
            select_model(cfg,'asr_turbo_model',root)
            self.assertNotIn('python',cfg['asr'])
