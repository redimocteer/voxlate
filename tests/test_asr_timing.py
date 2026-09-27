from types import SimpleNamespace as Obj
import unittest
from voxlate.asr import timed_segments


class TimingTests(unittest.TestCase):
    def test_word_boundaries_remove_leading_and_trailing_silence(self):
        result = timed_segments([Obj(start=2, end=5, text='What are you doing?', words=[
            Obj(start=3.52, end=3.52, word=' What'), Obj(start=3.52, end=4.48, word=' are you doing?')])], 'en')
        self.assertEqual((result[0]['start'], result[0]['end']), (3.52, 4.48))
        self.assertEqual(result[0]['source_text'], 'What are you doing?')

    def test_long_silence_splits_without_losing_japanese_text(self):
        result = timed_segments([Obj(start=0, end=35, text='はい。お願いします。', words=[
            Obj(start=1, end=1.5, word='はい。'), Obj(start=31, end=32, word='お願い'),
            Obj(start=32, end=33, word='します。')])], 'ja')
        self.assertEqual([s['source_text'] for s in result], ['はい。', 'お願いします。'])
        self.assertEqual([(s['start'], s['end']) for s in result], [(1, 1.5), (31, 33)])
        self.assertEqual([s['id'] for s in result], [1, 2])

    def test_missing_words_fall_back_and_segment_ids_remain_contiguous(self):
        result = timed_segments([Obj(start=0, end=1, text=' ', words=None),
                                Obj(start=2, end=3, text='hello', words=None)], 'en')
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['id'], 1)
        self.assertEqual(result[0]['start'], 2)
