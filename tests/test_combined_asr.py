import copy
from pathlib import Path
import unittest
from unittest.mock import patch

from test_pipeline import TestDirectory
from voxlate.combined_asr import corroborated_rows, merge_repairs, review_windows
from voxlate.common import VoxlateError, load_config
from voxlate.project_storage import default_project_directory, validate_project_directory


def row(text, start=1., end=2., confidence=.9):
    return dict(id=1, start=start, end=end, source_text=text, target_text='',
        confidence=confidence, no_speech_prob=.1,
        words=[dict(start=start, end=end, word=text, probability=confidence)])


class CombinedTests(unittest.TestCase):
    def test_new_configuration_defaults_to_combined(self):
        cfg = load_config(Path(__file__).resolve().parents[1]/'config.json')
        self.assertTrue(cfg['asr']['combined'])

    def test_disagreement_low_confidence_and_non_speech_are_not_repairs(self):
        good = row('Hello!')
        self.assertEqual(len(corroborated_rows([good], [row('hello.')])), 1)
        for bad in (row('Goodbye'), row('Hello', confidence=.1), row('Hello', start=5, end=6),
                    dict(good, no_speech_prob=.9)):
            self.assertEqual(corroborated_rows([good], [bad]), [])

    def test_insertions_preserve_absolute_times_do_not_duplicate_or_truncate(self):
        primary = [row('First', 0, 1), row('Long sentence', 4, 8)]
        snapshot = copy.deepcopy(primary)
        result, changes = merge_repairs(primary, [row('Missing', 2, 3), row('Missing', 2, 3), row('fragment', 5, 6)])
        self.assertEqual([s['source_text'] for s in result], ['First', 'Missing', 'Long sentence'])
        self.assertEqual([s['id'] for s in result], [1, 2, 3])
        self.assertEqual(len(changes), 1)
        self.assertEqual(primary, snapshot)
        self.assertEqual(result[-1]['start'], 4)

    def test_suspected_gap_is_prioritized_and_review_work_is_bounded(self):
        primary = [row('First', 0, 3), row('Second', 10, 12)]
        windows = review_windows(primary, primary, [dict(start=5, end=7)], 20)
        self.assertTrue(any(lo <= 5 and hi >= 7 for lo, hi in windows))
        self.assertTrue(all(0 <= lo < hi <= 20 for lo, hi in windows))
        self.assertLessEqual(sum(hi-lo for lo, hi in windows), 16)

    def test_mode_has_independent_project_directory_beside_video(self):
        cfg = load_config(Path(__file__).resolve().parents[1]/'config.json')
        cfg['asr']['combined'] = False
        with TestDirectory() as folder:
            video = Path(folder)/'movie.mkv'
            single = default_project_directory(video, cfg)
            cfg['asr']['combined'] = True
            combined = default_project_directory(video, cfg)
            self.assertNotEqual(single, combined)
            self.assertEqual(combined.parent, video.parent/(video.name+'.voxlate'))
            validate_project_directory(video, combined)
            with self.assertRaises(VoxlateError):
                validate_project_directory(video, video.parent/'projects')
