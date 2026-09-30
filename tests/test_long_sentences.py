import copy
import unittest
from unittest.mock import patch

import test_pipeline as fixtures
from voxlate.common import read_json
from voxlate.long_sentences import split_long_sentence, refine_long_sentences
from voxlate.pipeline import validate_segments


def sample():
    words = [dict(start=start, end=end, word=text) for start,end,text in (
        (0,1.3,'Please '),(1.3,2.7,'wait, '),(3.2,4.3,'then '),
        (4.3,5.7,'open '),(5.7,7,'the '),(7,9,'door.'))]
    return dict(id=1,start=0,end=9,source_text='Please wait, then open the door.',
                target_text='',speaker='A',words=words)


class LongSentenceTests(unittest.TestCase):
    def test_only_long_rows_split_preserving_every_word_and_valid_ids(self):
        row = sample()
        unchanged = copy.deepcopy(row)
        result, changed = refine_long_sentences([row])
        self.assertEqual(changed,1)
        self.assertEqual([r['source_text'] for r in result], ['Please wait,','then open the door.'])
        self.assertEqual([r['id'] for r in result],[1,2])
        self.assertEqual([(r['start'],r['end']) for r in result],[(0,2.7),(3.2,9)])
        self.assertEqual([w for r in result for w in r['words']],row['words'])
        self.assertEqual(row, unchanged)
        validate_segments(result,9)

    def test_eight_seconds_and_shorter_are_unchanged(self):
        for duration in (4,8):
            row=sample();row['end']=duration
            self.assertEqual(split_long_sentence(row),[row])

    def test_unsupported_or_existing_rows_are_preserved_in_full(self):
        for changes in ({'enabled':False},{'timing_uncertain':True},{'timing_fallback':True},
                {'auto_preserve_original':True},{'manual_boundary':True},{'target_text':'已翻译'},
                {'words':[]},{'source_text':'Unmatched transcript.'}):
            with self.subTest(changes=changes):
                row=dict(sample(),**changes)
                self.assertEqual(split_long_sentence(row),[row])

    def test_invalid_word_times_are_not_guessed(self):
        for start,end in ((2,1),(0,float('nan')),(-1,2),(1,8),(0,10)):
            row=sample();row['words'][1].update(start=start,end=end)
            self.assertEqual(split_long_sentence(row),[row])
        row=sample()
        for word in row['words']:word['end']=word['start']
        self.assertEqual(split_long_sentence(row),[row])

    def test_no_boundary_and_tiny_tail_keep_entire_row(self):
        row=sample();row['words'][1]['word']='wait ';row['source_text']='Please wait then open the door.'
        self.assertEqual(split_long_sentence(row),[row])
        row=sample();row['words'][2]['start']=row['words'][1]['end']
        self.assertEqual(split_long_sentence(row),[row])

    def test_cjk_punctuation_and_abbreviations(self):
        for text in ('请稍等，','少々お待ちください、'):
            row=sample();row['words'][0]['word']='';row['words'][1]['word']=text
            row['source_text']=''.join(w['word'] for w in row['words'])
            self.assertEqual(len(split_long_sentence(row)),2)
        for text in ('Mr. ','Dr. ','U.S. '):
            row=sample();row['words'][1]['word']=text;row['source_text']=''.join(w['word'] for w in row['words'])
            self.assertEqual(split_long_sentence(row),[row])
        row=sample();row['words'][1]['word']='wait. ';row['source_text']=''.join(w['word'] for w in row['words'])
        self.assertEqual(len(split_long_sentence(row)),2)

    def test_pipeline_refines_only_new_recognition_not_translation_or_dubbing(self):
        fixture=fixtures.PipelineIntegration();fixture.setUp();self.addCleanup(fixture.tearDown)
        fixture.run_pipeline(stop_after='recognize')
        with patch('voxlate.long_sentences.refine_long_sentences',side_effect=AssertionError('existing sentences changed')):
            fixture.run_pipeline(stop_after='translate',translate_only=True)
            fixture.run_pipeline(stop_after='dub',require_translated=True)
        from voxlate.long_sentences import refine_long_sentences as refine
        with patch('voxlate.long_sentences.refine_long_sentences',wraps=refine) as refinement:
            fixture.run_pipeline(stop_after='recognize',force_recognition=True)
            refinement.assert_called_once()
        project=read_json(fixture.work/'project.json')
        self.assertEqual(len(project['segments']),2)
