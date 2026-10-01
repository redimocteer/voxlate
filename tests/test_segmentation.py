import copy
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from voxlate.common import VoxlateError, digest, read_json, write_json
from voxlate.segmentation import SegmentPlan, replace_segments, apply_segmentation, validate_blocks
from voxlate.dubbing_state import voice_key, sentence_ready
from voxlate.media import set_cancel_event
from test_pipeline import TestDirectory, tone
import test_pipeline as pipeline_tests


def fixture():
    return dict(duration=20, segments=[
        dict(id=1, start=1., end=3., source_text='First.', target_text='第一句'),
        dict(id=2, start=4., end=7., source_text='Second.', target_text='第二句'),
        dict(id=3, start=9., end=12., source_text='Third.', target_text='第三句')])


class PlanTests(unittest.TestCase):
    def test_single_voice_reference_tracks_renumbering_and_deleted_reference(self):
        project = fixture()
        project['voice_mode'] = 'individual'
        project['segments'][1]['voice_reference_sentence_id'] = 3
        cfg = {'tts':{}}
        key = voice_key(project,cfg,project['segments'][1])
        plan = SegmentPlan(project,0,0)
        plan.split(2)
        current, changed = replace_segments(project,plan.blocks,['One','Two'],['一','二'])
        self.assertFalse(changed)
        self.assertEqual(current['segments'][2]['voice_reference_sentence_id'],4)
        self.assertEqual(voice_key(current,cfg,current['segments'][2]),key)
        deleted, changed = replace_segments(current,[dict(start=9,end=12,enabled=False,omit_row=True)],[''],[''])
        self.assertTrue(changed)
        self.assertNotIn('voice_reference_sentence_id',deleted['segments'][2])

    def test_gaps_remain_in_timeline_and_history_restores_cuts(self):
        project = fixture()
        plan = SegmentPlan(project, 0, 1)
        self.assertEqual([(b['start'], b['end'], b['enabled']) for b in plan.blocks], [(1,3,True),(3,4,False),(4,7,True)])
        initial = copy.deepcopy(plan.blocks)
        plan.split(2)
        self.assertEqual(len(plan.blocks), 4)
        plan.undo()
        self.assertEqual(plan.blocks, initial)
        plan.redo()
        plan.merge(1)
        self.assertEqual([(b['start'],b['end']) for b in plan.blocks], [(1,3),(3,4),(4,7)])
        self.assertEqual(plan.blocks[0]['text'], '')
        plan.undo()
        self.assertEqual(len(plan.blocks), 4)

    def test_extension_absorbs_whole_neighbors_and_preserves_existing_edits(self):
        plan = SegmentPlan(fixture(), 1, 1)
        plan.split(5)
        plan.extend(2.5, 9.1)
        self.assertEqual((plan.blocks[0]['start'],plan.blocks[-1]['end']), (1,12))
        self.assertIn(5, [b['end'] for b in plan.blocks])
        self.assertEqual([b['enabled'] for b in plan.blocks], [True,False,True,True,False,True])
        validate_blocks(plan.blocks,20)
        plan.undo()
        self.assertEqual((plan.blocks[0]['start'],plan.blocks[-1]['end']), (4,7))

    def test_drag_cannot_create_overlaps_or_negative_times(self):
        plan = SegmentPlan(fixture(),0,0)
        plan.split(2)
        plan.move_cut(1, 100)
        self.assertAlmostEqual(plan.blocks[0]['end'],2.9)
        self.assertEqual(plan.blocks[0]['end'],plan.blocks[1]['start'])
        validate_blocks(plan.blocks,20)
        plan.extend(-100,4.1)
        self.assertEqual((plan.blocks[0]['start'],plan.blocks[-1]['end']), (0,7))
        with self.assertRaises(VoxlateError):
            validate_blocks([dict(start=0,end=1,enabled=True),dict(start=.5,end=2,enabled=True)],20)

    def test_voice_references_survive_renumbering(self):
        project = fixture()
        project.update(voice_mode='uniform',reference_sentence_id=3,reference_auto_recommend=False,
            roles=[dict(id='a',name='角色A',reference_sentence_id=3)])
        for s in project['segments']:
            s['role_id']='a'
        cfg = {'tts': {'emotion_reference':True}}
        old_uniform = voice_key(project,cfg)
        project['voice_mode']='roles'
        old_role = voice_key(project,cfg,project['segments'][2])
        plan = SegmentPlan(project,0,0)
        plan.split(2)
        result, changed = replace_segments(project,plan.blocks,['One','Two'],['一','二'])
        self.assertFalse(changed)
        self.assertEqual([s['id'] for s in result['segments']],[1,2,3,4])
        self.assertEqual(result['reference_sentence_id'],4)
        self.assertEqual(result['roles'][0]['reference_sentence_id'],4)
        self.assertEqual(voice_key(result,cfg,result['segments'][3]),old_role)
        result['voice_mode']='uniform'
        self.assertEqual(voice_key(result,cfg),old_uniform)
        self.assertEqual(project['reference_sentence_id'],3)

    def test_cutting_a_reference_invalidates_it_and_flags_review(self):
        project=fixture()
        project.update(reference_sentence_id=1,reference_auto_recommend=False)
        plan=SegmentPlan(project,0,0)
        plan.split(2)
        result, changed=replace_segments(project,plan.blocks,['One','Two'],['一','二'])
        self.assertTrue(changed)
        self.assertIsNone(result['reference_sentence_id'])
        self.assertTrue(result['reference_auto_recommend'])

    def test_manual_recognition_loads_each_whisper_once_and_keeps_block_count(self):
        from voxlate.manual_asr import transcribe_blocks
        cfg=dict(model_path='models/faster-whisper-large-v3-turbo',device='cpu',compute_type='int8',cpu_threads=2,beam_size=5,combined=True)
        first=Mock(transcribe=Mock(side_effect=[([SimpleNamespace(text='one'),SimpleNamespace(text='two')],None),([],None)]))
        second=Mock(transcribe=Mock(side_effect=[([SimpleNamespace(text='different')],None),([SimpleNamespace(text='recovered')],None)]))
        constructor=Mock(side_effect=[first,second])
        with TestDirectory() as folder, patch.dict(sys.modules,{'faster_whisper':SimpleNamespace(WhisperModel=constructor)}):
            result=transcribe_blocks([dict(audio='a.wav'),dict(audio='b.wav')],cfg,folder)
        self.assertEqual(result,['one two','recovered'])
        self.assertEqual(constructor.call_count,2)
        self.assertFalse(first.transcribe.call_args.kwargs['vad_filter'])
        self.assertFalse(first.transcribe.call_args.kwargs['word_timestamps'])


class CommitTests(unittest.TestCase):
    def setUp(self):
        self.integration = pipeline_tests.PipelineIntegration('test_render_resume_and_single_sentence_edit')
        self.integration.setUp()
        f = self.integration
        f.work = f.video.with_name(f.video.name+'.voxlate')/'en'
        f.run_pipeline(stop_after='translate',voice_mode='uniform',reference_sentence_id=2)
        f.run_pipeline(stop_after='dub',require_translated=True,voice_mode='uniform',reference_sentence_id=2)
        self.path=f.work/'project.json'
        self.project=read_json(self.path)
        self.plan=SegmentPlan(self.project,0,0)
        block=self.plan.blocks[0]
        self.plan.split((block['start']+block['end'])/2)

    def tearDown(self):
        set_cancel_event(None)
        self.integration.tearDown()

    def runner(self, kind, job):
        if kind == 'asr':
            self.assertIn('manual_blocks',job)
            self.assertTrue(all(Path(b['audio']).is_relative_to(self.path.parent) for b in job['manual_blocks']))
            return ['Manually cut speech.']*len(job['manual_blocks'])
        self.assertEqual(kind,'translator')
        return ['手动分句。']*len(job['texts'])

    def test_atomic_commit_preserves_outside_audio_and_creates_restorable_backup(self):
        outside=self.project['segments'][1]
        result=apply_segmentation(self.path,digest(self.project),self.integration.cfg,self.plan.blocks,runner=self.runner)
        current=read_json(self.path)
        self.assertEqual(len(current['segments']),3)
        self.assertEqual([(s['start'],s['end']) for s in current['segments'][:2]],[(b['start'],b['end']) for b in self.plan.blocks])
        self.assertEqual(current['segments'][2]['tts_audio'],outside['tts_audio'])
        self.assertTrue(sentence_ready(current['segments'][2],voice_key(current,self.integration.cfg,current['segments'][2])))
        self.assertFalse(current['segments'][0].get('tts_audio'))
        self.assertEqual(read_json(result['backup']),self.project)
        self.assertFalse(result['reference_changed'])
        # The existing pipeline can dub/export the new rows without re-recognition.
        f=self.integration
        f.run_pipeline(stop_after='dub',require_translated=True,voice_mode='uniform',reference_sentence_id=3)
        f.run_pipeline(export_only=True,voice_mode='uniform',reference_sentence_id=3)
        self.assertTrue(f.output.is_file())

    def test_full_manual_after_separation_and_replacing_existing_rows(self):
        from voxlate.paired_segments import PairedSegmentPlan
        for empty in (False,True):
            project=copy.deepcopy(self.project)
            if empty:
                project['segments']=[]
                project.pop('roles',None)
                project.pop('reference_sentence_id',None)
            write_json(self.path,project)
            plan=PairedSegmentPlan(project,0,len(project['segments'])-1,full=True)
            self.assertEqual(len(plan.pairs),len(project['segments']))
            plan.delete_sentences(range(len(plan.pairs)))
            plan.add_sentence(.2,.7)
            plan.add_sentence(1.6,2.2)
            draft=self.path.parent/'.temp'/'segmentation-full-draft.json'
            write_json(draft,dict(blocks=plan.blocks))
            result=apply_segmentation(self.path,digest(project),self.integration.cfg,plan.blocks,runner=self.runner,full=True)
            current=read_json(self.path)
            self.assertEqual([(s['start'],s['end']) for s in current['segments']],[(.2,.7),(1.6,2.2)])
            self.assertEqual([s['id'] for s in current['segments']],[1,2])
            self.assertEqual(current.get('prepared_audio_key'),project.get('prepared_audio_key'))
            self.assertEqual(read_json(result['backup']),project)
            self.assertFalse(draft.exists())

    def test_full_import_without_edits_keeps_existing_results_without_models(self):
        from voxlate.paired_segments import PairedSegmentPlan
        plan=PairedSegmentPlan(self.project,0,len(self.project['segments'])-1,full=True)
        runner=Mock(side_effect=AssertionError('unchanged sentences must not run models'))
        result=apply_segmentation(self.path,digest(self.project),self.integration.cfg,plan.blocks,runner=runner,full=True)
        self.assertTrue(result['unchanged'])
        self.assertEqual(read_json(self.path),self.project)
        runner.assert_not_called()

    def test_editor_translation_is_temporary_and_apply_reuses_it(self):
        from voxlate.paired_segments import PairedSegmentPlan
        from voxlate.segmentation import preview_translations, segmentation_needs_models
        plan=PairedSegmentPlan(self.project,0,1,full=True)
        rows=[dict(index=0,start=plan.pairs[0][0],end=plan.pairs[0][1],text='Edited source.')]
        before=self.path.read_bytes()
        runner=Mock(return_value=['临时译文。'])
        translated=preview_translations(self.path,digest(self.project),self.integration.cfg,rows,
            context=['Edited source.','Welcome.'],runner=runner)
        self.assertEqual(runner.call_args.args[0],'translator')
        self.assertEqual(self.path.read_bytes(),before)
        plan.translated(translated)
        self.assertFalse(segmentation_needs_models(self.project,plan.blocks))
        fail=Mock(side_effect=AssertionError('completed editor translation must not reload models'))
        result=apply_segmentation(self.path,digest(self.project),self.integration.cfg,plan.blocks,runner=fail,full=True)
        current=read_json(self.path)
        self.assertEqual(current['segments'][0]['source_text'],'Edited source.')
        self.assertEqual(current['segments'][0]['target_text'],'临时译文。')
        self.assertEqual(current['segments'][1]['tts_audio'],self.project['segments'][1]['tts_audio'])
        self.assertEqual(result['updated'],1)
        fail.assert_not_called()

    def test_preview_recognizes_only_missing_sources_and_never_commits(self):
        from voxlate.segmentation import preview_translations
        rows=[dict(start=.2,end=.8,text=''),dict(start=1.6,end=2.4,text='Typed source.')]
        calls=[]
        def runner(kind,job):
            calls.append((kind,job))
            return ['New speech.'] if kind=='asr' else ['新对白。','手写原文。']
        before=self.path.read_bytes()
        result=preview_translations(self.path,digest(self.project),self.integration.cfg,rows,runner=runner)
        self.assertEqual([c[0] for c in calls],['asr','translator'])
        self.assertEqual(len(calls[0][1]['manual_blocks']),1)
        self.assertEqual(result[0]['text'],'New speech.')
        self.assertEqual(result[1]['target_text'],'手写原文。')
        self.assertEqual(self.path.read_bytes(),before)

    def test_full_manual_on_fresh_separation_can_dub_and_export(self):
        from voxlate.paired_segments import PairedSegmentPlan
        f=self.integration
        f.work=f.video.with_name(f.video.name+'.voxlate')/'manual-fresh'
        f.run_pipeline(stop_after='separate',voice_mode='individual')
        self.path=f.work/'project.json'
        project=read_json(self.path)
        self.assertEqual(project['segments'],[])
        plan=PairedSegmentPlan(project,0,-1,full=True)
        plan.add_sentence(.2,1.1)
        plan.add_sentence(1.6,2.5)
        apply_segmentation(self.path,digest(project),f.cfg,plan.blocks,runner=self.runner,full=True)
        calls=f.calls.copy()
        f.run_pipeline(stop_after='dub',require_translated=True,voice_mode='individual')
        f.run_pipeline(export_only=True,voice_mode='individual')
        self.assertEqual(f.calls['asr'],calls['asr'])
        self.assertEqual(f.calls['translator'],calls['translator'])
        self.assertTrue(f.output.is_file())
        current=read_json(self.path)
        self.assertEqual([(s['start'],s['end']) for s in current['segments']],[(.2,1.1),(1.6,2.5)])

    def test_invalid_recognition_or_empty_translation_never_overwrites_project(self):
        for stage in ('asr','translator'):
            with self.subTest(stage=stage):
                original=self.path.read_bytes()
                def failing(kind,job):
                    return ([None]*len(job['manual_blocks']) if kind=='asr' else ['']*len(job['texts'])) if kind==stage else self.runner(kind,job)
                with self.assertRaises(VoxlateError):
                    apply_segmentation(self.path,digest(self.project),self.integration.cfg,self.plan.blocks,runner=failing)
                self.assertEqual(self.path.read_bytes(),original)

    def test_cancel_or_concurrent_change_preserves_project(self):
        event=threading.Event()
        event.set()
        set_cancel_event(event)
        with self.assertRaisesRegex(VoxlateError,'已取消'):
            apply_segmentation(self.path,digest(self.project),self.integration.cfg,self.plan.blocks,runner=self.runner)
        self.assertEqual(read_json(self.path),self.project)

        set_cancel_event(None)
        with self.assertRaisesRegex(VoxlateError,'项目已变化'):
            apply_segmentation(self.path,'outdated',self.integration.cfg,self.plan.blocks,runner=self.runner)
        self.assertEqual(read_json(self.path),self.project)

    def test_empty_asr_discards_only_empty_ranges_and_translates_remaining(self):
        outside=copy.deepcopy(self.project['segments'][1])
        before=copy.deepcopy(self.plan.blocks)
        def runner(kind,job):
            if kind=='asr':
                return ['', 'Kept sentence.']
            self.assertEqual(job['texts'],['Kept sentence.'])
            return ['保留的句子。']
        result=apply_segmentation(self.path,digest(self.project),self.integration.cfg,self.plan.blocks,runner=runner)
        current=read_json(self.path)
        self.assertEqual(len(result['skipped']),1)
        self.assertEqual(result['sentences'],1)
        self.assertEqual(self.plan.blocks,before)
        self.assertEqual(len(current['segments']),2)
        self.assertEqual(current['segments'][0]['start'],before[1]['start'])
        self.assertEqual(current['segments'][1]['tts_audio'],outside['tts_audio'])
        from voxlate.media import original_audio_intervals
        self.assertTrue(any(lo <= before[0]['start'] and hi >= before[0]['end']
                            for lo,hi in original_audio_intervals(current['segments'],current['duration'])))

    def test_all_empty_asr_can_remove_whole_selected_range(self):
        from voxlate.paired_segments import PairedSegmentPlan
        plan=PairedSegmentPlan(self.project,0,len(self.project['segments'])-1)
        for index in range(0, len(plan.cuts), 2):
            plan.move_cut(index, plan.cuts[index]+.01)
        def runner(kind,job):
            self.assertEqual(kind,'asr')
            return ['']*len(job['manual_blocks'])
        result=apply_segmentation(self.path,digest(self.project),self.integration.cfg,plan.blocks,runner=runner)
        self.assertEqual(result['sentences'],0)
        self.assertEqual(read_json(self.path)['segments'],[])
        self.assertEqual(read_json(result['backup']),self.project)

    def test_delete_only_preserves_remaining_translation_role_and_audio_without_models(self):
        from voxlate.paired_segments import PairedSegmentPlan
        from voxlate.pipeline import VideoDubPipeline
        plan=PairedSegmentPlan(self.project,0,1)
        plan.delete_sentence(0)
        retained=copy.deepcopy(self.project['segments'][1])
        retained.update(id=1,voice_identity=retained.get('voice_identity',2))
        with patch.object(VideoDubPipeline,'cached_media',side_effect=AssertionError('media not needed')):
            result=apply_segmentation(self.path,digest(self.project),self.integration.cfg,plan.blocks,
                                      runner=Mock(side_effect=AssertionError('models not needed')))
        current=read_json(self.path)
        self.assertEqual(current['segments'],[retained])
        self.assertEqual(current['reference_sentence_id'],1)
        self.assertEqual((result['reused'],result['updated']),(1,0))
        self.integration.run_pipeline(export_only=True,voice_mode='uniform',reference_sentence_id=1)
        self.assertTrue(self.integration.output.exists())

    def test_addition_or_boundary_change_only_processes_changed_sentence(self):
        from voxlate.paired_segments import PairedSegmentPlan
        for edit in ('addition','boundary'):
            with self.subTest(edit=edit):
                write_json(self.path,self.project)
                plan=PairedSegmentPlan(self.project,0,1)
                if edit=='addition':
                    plan.add_sentence(1.2,1.4)
                else:
                    plan.move_cut(1,1.2)
                calls=[]
                def runner(kind,job):
                    calls.append(kind)
                    if kind=='asr':
                        self.assertEqual(len(job['manual_blocks']),1)
                        return ['New speech.']
                    self.assertEqual(job['texts'],['New speech.'])
                    self.assertIn(self.project['segments'][1]['source_text'],job['context'])
                    self.assertEqual(job['context'][job['indices'][0]],'New speech.')
                    return ['新对白。']
                result=apply_segmentation(self.path,digest(self.project),self.integration.cfg,plan.blocks,runner=runner)
                self.assertEqual(calls,['asr','translator'])
                self.assertEqual(result['updated'],1)
                current=read_json(self.path)
                for old in self.project['segments'][(0 if edit=='addition' else 1):]:
                    preserved=next(s for s in current['segments'] if s['start']==old['start'])
                    expected=copy.deepcopy(old)
                    expected.update(id=preserved['id'],voice_identity=old.get('voice_identity',old['id']))
                    self.assertEqual(preserved,expected)
                new=next(s for s in current['segments'] if s['source_text']=='New speech.')
                self.assertNotIn('tts_audio',new)

    def test_unchanged_and_undo_are_byte_identical_and_need_no_models(self):
        from voxlate.paired_segments import PairedSegmentPlan
        from voxlate.segmentation import segmentation_needs_models
        self.project['segments'][0]['enabled']=False
        write_json(self.path,self.project)
        original=self.path.read_bytes()
        plan=PairedSegmentPlan(self.project,0,1)
        for undo in (False,True):
            if undo:
                plan.delete_sentence(0)
                plan.undo()
            self.assertFalse(segmentation_needs_models(self.project,plan.blocks))
            result=apply_segmentation(self.path,digest(self.project),self.integration.cfg,plan.blocks,
                                      runner=Mock(side_effect=AssertionError('models not needed')))
            self.assertTrue(result['unchanged'])
            self.assertIsNone(result['backup'])
            self.assertEqual(self.path.read_bytes(),original)
        restored=PairedSegmentPlan(self.project,0,1,plan.blocks)
        self.assertEqual(restored.pairs,plan.pairs)

    def test_manual_text_skips_recognition_and_grey_blocks_skip_models(self):
        self.plan.blocks[0].update(text='Typed source.',manual_text=True)
        self.plan.blocks[1].update(enabled=False)
        calls=[]
        def runner(kind,job):
            calls.append(kind)
            self.assertEqual(job['texts'],['Typed source.'])
            return ['手动原文。']
        apply_segmentation(self.path,digest(self.project),self.integration.cfg,self.plan.blocks,runner=runner)
        self.assertEqual(calls,['translator'])
        current=read_json(self.path)
        self.assertFalse(current['segments'][1]['enabled'])
        self.assertEqual(current['segments'][1]['target_text'],'')

    def test_single_sentence_preview_is_local_non_mutating_and_reused_at_commit(self):
        from voxlate.segmentation import recognize_segment
        from voxlate.paired_segments import PairedSegmentPlan
        from voxlate.media import original_audio_intervals
        plan=PairedSegmentPlan(self.project,0,0)
        start,end=plan.pairs[0]
        calls=[]
        def runner(kind,job):
            calls.append(kind)
            return self.runner(kind,job)
        original=self.path.read_bytes()
        text=recognize_segment(self.path,digest(self.project),self.integration.cfg,start,end,runner=runner)
        self.assertEqual(calls,['asr'])
        self.assertEqual(self.path.read_bytes(),original)
        blocks=plan.blocks
        for block in blocks:
            if block['enabled']:
                block.update(text=text,manual_text=True)
        apply_segmentation(self.path,digest(self.project),self.integration.cfg,blocks,runner=runner)
        self.assertEqual(calls,['asr','translator'])
        current=read_json(self.path)
        self.assertEqual(len(current['segments']),len(self.project['segments']))
        self.assertEqual([(s['start'],s['end']) for s in current['segments']],
                         [(s['start'],s['end']) for s in self.project['segments']])
        self.assertEqual(original_audio_intervals(current['segments'],current['duration']),
                         original_audio_intervals(self.project['segments'],self.project['duration']))

    def test_main_window_opens_editor_and_applies_through_background_tasks(self):
        self.exercise_editor_apply(False)

    def test_main_window_updates_remaining_sentences_and_notifies_empty(self):
        self.exercise_editor_apply(True)

    def exercise_editor_apply(self, drop_empty):
        from PySide6.QtWidgets import QApplication, QDialog, QMessageBox
        from voxlate.gui import MainWindow
        from voxlate.segmentation_dialog import SegmentationDialog
        app=QApplication.instance() or QApplication([])
        window=MainWindow(self.integration.root/'gui-data',auto_check=False)
        window.cfg=copy.deepcopy(self.integration.cfg)
        window.load_project(self.path)
        opened=[]
        def edit(dialog):
            opened.append(True)
            start,end=dialog.plan.pairs[0]
            dialog.plan.split(start+(end-start)*.35)
            dialog.plan.split(start+(end-start)*.65)
            dialog.plan.validate()
            dialog.accept()
            return QDialog.DialogCode.Accepted
        expected_count=2 if drop_empty else 3
        def runner(kind,job):
            if drop_empty and kind=='asr':
                return ['', 'Kept sentence.']
            return self.runner(kind,job)
        def commit(*args,**kwargs):
            return apply_segmentation(*args,**kwargs,runner=runner)
        try:
            with patch.object(SegmentationDialog,'exec',new=edit), \
                    patch('voxlate.segmentation.apply_segmentation',side_effect=commit), \
                    patch.object(QMessageBox,'warning') as warning, patch.object(QMessageBox,'critical') as critical, \
                    patch.object(QMessageBox,'information') as information:
                window.table.time_anchor=0
                window.table.select_times(0)
                window.select_segmentation_range(0,0)
                self.assertIsNone(window.task)
                self.assertFalse(window.manual_segmentation_button.isHidden())
                self.assertEqual(opened,[])
                window.open_selected_segmentation()
                deadline=time.monotonic()+15
                while time.monotonic()<deadline and (window.task is not None or not opened or window.table.rowCount()!=expected_count):
                    app.processEvents()
                    time.sleep(.01)
                self.assertIsNone(window.task)
                self.assertEqual(opened,[True])
                warning.assert_not_called()
                critical.assert_not_called()
                self.assertEqual(window.table.rowCount(),expected_count)
                self.assertEqual(window.reference_sentence.value(),expected_count)
                if drop_empty:
                    self.assertIn('已舍弃并保留原声',information.call_args.args[2])
                self.assertFalse(window.export_button.isEnabled())
                self.assertFalse((self.path.parent/'.temp/segmentation-draft.json').exists())
        finally:
            for dialog in window.findChildren(SegmentationDialog):
                dialog.reject()
            if window.task is not None:
                window.cancel_task()
                deadline=time.monotonic()+10
                while window.task is not None and time.monotonic()<deadline:
                    app.processEvents()
                    time.sleep(.01)
            window.close()
            window.deleteLater()
            app.processEvents()


class EditorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app=QApplication.instance() or QApplication([])

    def test_time_column_single_click_and_drag_emit_ranges_only(self):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest
        from PySide6.QtWidgets import QTableWidgetItem
        from voxlate.sentence_table import SentenceTable, HEADER_HELP
        table=SentenceTable(4,3)
        table.setHorizontalHeaderLabels(['时间','原文','中文译文'])
        for row in range(4):
            for col in range(3):
                table.setItem(row,col,QTableWidgetItem('content'))
        table.resize(500,230)
        table.show()
        self.app.processEvents()
        events=[]
        table.timeRangeSelected.connect(lambda a,b: events.append((a,b)))
        point=lambda row,col: table.visualItemRect(table.item(row,col)).center()
        QTest.mouseClick(table.viewport(),Qt.MouseButton.LeftButton,pos=point(1,0))
        self.app.processEvents()
        QTest.mousePress(table.viewport(),Qt.MouseButton.LeftButton,pos=point(0,0))
        QTest.mouseMove(table.viewport(),point(3,0))
        QTest.mouseRelease(table.viewport(),Qt.MouseButton.LeftButton,pos=point(3,0))
        self.app.processEvents()
        QTest.mouseClick(table.viewport(),Qt.MouseButton.LeftButton,pos=point(1,1))
        self.app.processEvents()
        self.assertEqual(events,[(1,1),(0,3)])
        self.assertEqual(set(HEADER_HELP),{0,1,2})
        table.close()
        table.deleteLater()

    def test_wave_editor_shortcuts_pairs_and_scoped_waveform(self):
        from PySide6.QtCore import Qt, QPoint
        from PySide6.QtTest import QTest
        from voxlate.segmentation_dialog import SegmentationDialog,waveform_peaks
        with TestDirectory() as folder:
            audio=Path(folder)/'synthetic.wav'
            tone(audio,20)
            peaks=waveform_peaks(audio,limit=2000)
            self.assertLessEqual(len(peaks['peaks']),2000)
            dialog=SegmentationDialog(fixture(),0,1,audio,audio,[peaks,peaks])
            try:
                dialog.show()
                self.app.processEvents()
                self.assertEqual(dialog.table.rowCount(),2)
                self.assertEqual((dialog.canvas.left,dialog.canvas.span),(.5,7.5))
                scoped=waveform_peaks(audio,start=3,end=8)
                self.assertEqual((scoped['start'],scoped['end']),(3,8))
                self.assertLessEqual(len(scoped['peaks']),501)
                # S no longer changes segmentation. D/Delete removes the selected sentence.
                QTest.keyClick(dialog.canvas,Qt.Key.Key_S)
                self.assertEqual(len(dialog.plan.cuts),4)
                QTest.keyClick(dialog.canvas,Qt.Key.Key_Delete)
                self.assertEqual(dialog.plan.pairs,[(4,7)])
                QTest.keyClick(dialog.canvas,Qt.Key.Key_Z)
                self.assertEqual(dialog.plan.pairs,[(1,3),(4,7)])
                QTest.keyClick(dialog.canvas,Qt.Key.Key_R)
                self.assertEqual(dialog.plan.pairs,[(4,7)])
                dialog.history(False)
                QTest.mouseClick(dialog.canvas,Qt.MouseButton.LeftButton,pos=QPoint(round(dialog.canvas.x(3)),100))
                self.assertEqual(dialog.canvas.cut,1)
                QTest.keyClick(dialog.canvas,Qt.Key.Key_D)
                self.assertEqual(dialog.plan.pairs,[(4,7)])
                dialog.history(False)
                dialog.source.setCurrentIndex(1)
                self.assertEqual(dialog.canvas.waveform,peaks)
            finally:
                dialog.reject()
                dialog.deleteLater()
                self.app.processEvents()


if __name__ == '__main__':
    unittest.main()
