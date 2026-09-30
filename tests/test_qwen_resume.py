from pathlib import Path
import sys
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

import numpy as np

from test_pipeline import TestDirectory
from voxlate.common import read_json, write_json
from voxlate.qwen_recognition import transcribe
from voxlate.recognition_cache import QwenCheckpoint


class FakeAudio:
    samplerate = 16000
    frames = 40 * samplerate

    def __init__(self, *args):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def seek(self, frame):
        pass

    def read(self, frames, **kwargs):
        return np.zeros((frames, 1), dtype=np.float32)


class QwenResumeTests(unittest.TestCase):
    def setUp(self):
        self.directory = TestDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.audio = self.root/'audio.wav'
        self.audio.write_bytes(b'synthetic audio identity')
        model = self.root/'model'
        model.mkdir()
        (model/'weights').write_bytes(b'synthetic model identity')
        self.cfg = dict(model_path=str(model), device='cpu', language='en')
        self.asr = Mock()
        self.asr.transcribe.return_value = [NS(text='Hello.')]
        self.aligner = Mock()
        self.aligner.align.return_value = [NS(items=[NS(text='Hello', start_time=0, end_time=.5)])]
        self.asr_factory = Mock(return_value=self.asr)
        self.align_factory = Mock(return_value=self.aligner)
        modules = dict(soundfile=NS(SoundFile=FakeAudio), librosa=Mock(), torch=Mock(),
                       qwen_asr=NS(Qwen3ASRModel=NS(from_pretrained=self.asr_factory),
                                   Qwen3ForcedAligner=NS(from_pretrained=self.align_factory)))
        patcher = patch.dict(sys.modules, modules)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_asr(self):
        return transcribe(self.audio, self.cfg, self.root)

    def test_interrupted_recognition_resumes_only_missing_block(self):
        self.asr.transcribe.side_effect = [[NS(text='Hello.')], RuntimeError('stopped')]
        with self.assertRaisesRegex(RuntimeError, 'stopped'):
            self.run_asr()
        saved = read_json(self.root/'recognition/qwen_transcript.json')
        self.assertEqual(len(saved), 1)
        self.asr.transcribe.reset_mock(side_effect=True)
        rows = self.run_asr()
        self.assertEqual(self.asr.transcribe.call_count, 1)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]['start'], saved[0]['end'])
        self.assertEqual(read_json(self.root/'recognition/qwen_transcript.json')[0], saved[0])

    def test_alignment_resume_does_not_load_recognizer_or_repeat_finished_alignment(self):
        self.aligner.align.side_effect = [self.aligner.align.return_value, RuntimeError('align stopped')]
        with self.assertRaisesRegex(RuntimeError, 'align stopped'):
            self.run_asr()
        self.asr_factory.reset_mock()
        self.aligner.align.reset_mock(side_effect=True)
        rows = self.run_asr()
        self.asr_factory.assert_not_called()
        self.assertEqual(self.aligner.align.call_count, 1)
        self.assertEqual(len(rows), 2)
        self.align_factory.reset_mock()
        self.assertEqual(self.run_asr(), rows)
        self.asr_factory.assert_not_called()
        self.align_factory.assert_not_called()

    def test_deleting_transcript_forces_fresh_recognition(self):
        self.run_asr()
        (self.root/'recognition/qwen_transcript.json').unlink()
        self.asr.transcribe.reset_mock()
        self.run_asr()
        self.assertEqual(self.asr.transcribe.call_count, 2)

    def test_cached_suspicious_alignment_is_protected_without_loading_models(self):
        self.aligner.align.return_value = [NS(items=[NS(text='Hello',start_time=0,end_time=8)])]
        rows = self.run_asr()
        self.assertTrue(all(not row['enabled'] for row in rows))
        self.assertTrue(all(row['auto_preserve_original'] for row in rows))
        files = [self.root/'recognition'/name for name in ('qwen_transcript.json','qwen_alignment.json','qwen_resume.json')]
        original = [path.read_bytes() for path in files]
        self.asr_factory.reset_mock()
        self.align_factory.reset_mock()
        self.assertEqual(self.run_asr(), rows)
        self.asr_factory.assert_not_called()
        self.align_factory.assert_not_called()
        self.assertEqual([path.read_bytes() for path in files], original)

    def test_untimed_block_keeps_text_and_original_audio_without_stopping_later_blocks(self):
        untimed = [NS(items=[NS(text='Hello', start_time=0, end_time=0)])]
        self.aligner.align.side_effect = [untimed, self.aligner.align.return_value]
        rows = self.run_asr()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['source_text'], 'Hello.')
        self.assertFalse(rows[0]['enabled'])
        self.assertTrue(rows[0]['timing_fallback'])
        self.assertEqual(rows[0]['start'], 0)
        self.assertEqual(rows[0]['end'], rows[1]['start'])
        self.assertEqual(rows[0]['words'], [])
        self.assertFalse(rows[1].get('timing_uncertain'))
        issue = read_json(self.root/'recognition/qwen_alignment_issue_0001.json')
        self.assertEqual(issue['words'][0]['end_time'], 0)
        self.asr_factory.reset_mock()
        self.align_factory.reset_mock()
        self.assertEqual(self.run_asr(), rows)
        self.asr_factory.assert_not_called()
        self.align_factory.assert_not_called()

    def test_out_of_range_alignment_fallback_survives_interruption_and_resume(self):
        self.aligner.align.side_effect = [
            [NS(items=[NS(text='Hello', start_time=0, end_time=999)])], RuntimeError('interrupted')]
        with self.assertRaisesRegex(RuntimeError, 'interrupted'):
            self.run_asr()
        self.asr_factory.reset_mock()
        self.aligner.align.reset_mock(side_effect=True)
        rows = self.run_asr()
        self.asr_factory.assert_not_called()
        self.assertEqual(self.aligner.align.call_count, 1)
        self.assertFalse(rows[0]['enabled'])
        self.assertTrue(rows[0]['timing_fallback'])
        self.assertTrue(rows[1].get('enabled', True))

    def test_cache_rejects_changed_audio_language_model_and_partial_commit(self):
        self.run_asr()
        directory = self.root/'recognition'
        def load(config=None):
            return QwenCheckpoint(self.audio, config or self.cfg, directory, FakeAudio.samplerate, FakeAudio.frames, lambda _: None)
        self.assertEqual(len(load().chunks), 2)
        self.assertEqual(load(dict(self.cfg, language='ja')).chunks, [])
        self.audio.write_bytes(b'changed audio identity')
        self.assertEqual(load().chunks, [])
        self.audio.write_bytes(b'synthetic audio identity')
        weights = Path(self.cfg['model_path'])/'weights'
        weights.write_bytes(b'changed model')
        self.assertEqual(load().chunks, [])
        self.run_asr()
        chunks = read_json(directory/'qwen_transcript.json')
        chunks[0]['text'] = 'interrupted write'
        write_json(directory/'qwen_transcript.json', chunks)
        self.assertEqual(load().chunks, [])

    def test_bad_alignment_reuses_text_and_realigns(self):
        self.run_asr()
        (self.root/'recognition/qwen_alignment.json').write_text('{')
        self.asr_factory.reset_mock()
        self.aligner.align.reset_mock()
        self.assertEqual(len(self.run_asr()), 2)
        self.asr_factory.assert_not_called()
        self.assertEqual(self.aligner.align.call_count, 2)

    def test_non_contiguous_or_invalid_ranges_are_rejected(self):
        good = dict(first_frame=0, last_frame=16000, start=0, end=1, text='Hello')
        for invalid in (dict(good, first_frame=2), dict(good, end=float('nan')), dict(good, last_frame=99999999)):
            with self.assertRaises(ValueError):
                QwenCheckpoint.validate_chunks([invalid], 16000, 32000)

    def test_legacy_long_chunk_resumes_with_short_contiguous_chunks(self):
        with patch.object(FakeAudio, 'frames', 40*FakeAudio.samplerate):
            checkpoint = QwenCheckpoint(self.audio, self.cfg, self.root/'recognition',
                FakeAudio.samplerate, FakeAudio.frames, lambda _: None)
            old = dict(start=0, end=25, first_frame=0, last_frame=25*16000, text='Hello.')
            checkpoint.chunks.append(old)
            checkpoint.save()
            from voxlate.qwen_recognition import recognize_chunk
            count = [0]
            def first_timeout(*args):
                count[0] += 1
                if count[0] == 1:
                    raise TimeoutError()
                return recognize_chunk(*args)
            with patch('voxlate.qwen_recognition.recognize_chunk', side_effect=first_timeout):
                self.run_asr()
            chunks = read_json(self.root/'recognition/qwen_transcript.json')
            self.assertEqual(chunks[0], old)
            self.assertEqual(chunks[-1]['last_frame'], FakeAudio.frames)
            self.assertTrue(all(a['last_frame']==b['first_frame'] for a,b in zip(chunks,chunks[1:])))
            self.assertTrue(all(c['end']-c['start']<=12 for c in chunks[1:]))
            self.assertEqual(self.asr.transcribe.call_count, len(chunks)-1)

    def test_timeout_retries_smaller_range_without_committing_partial_text(self):
        from voxlate.qwen_recognition import recognize_chunk
        calls = []
        def slow_first(model, samples, language, timeout):
            calls.append(len(samples))
            if len(calls)==1:
                raise TimeoutError()
            return recognize_chunk(model, samples, language, timeout)
        with patch('voxlate.qwen_recognition.recognize_chunk', side_effect=slow_first):
            self.run_asr()
        chunks = read_json(self.root/'recognition/qwen_transcript.json')
        self.assertLess(calls[1], calls[0])
        self.assertEqual(chunks[0]['first_frame'], 0)
        self.assertLessEqual(chunks[0]['end'], 12)
        self.assertEqual(chunks[-1]['last_frame'], FakeAudio.frames)
        self.assertTrue(all(a['last_frame']==b['first_frame'] for a,b in zip(chunks,chunks[1:])))

    def test_repeated_timeout_stops_without_saving_truncated_text(self):
        from voxlate.common import VoxlateError
        with patch('voxlate.qwen_recognition.recognize_chunk', side_effect=TimeoutError()) as recognize:
            with self.assertRaisesRegex(VoxlateError, '此前进度保留'):
                self.run_asr()
        self.assertEqual(recognize.call_count, 4)
        self.assertFalse((self.root/'recognition/qwen_transcript.json').exists())
