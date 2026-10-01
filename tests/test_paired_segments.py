import time
import threading
from pathlib import Path
import unittest

from voxlate.common import VoxlateError
from voxlate.paired_segments import PairedSegmentPlan
from voxlate.segmentation import replace_segments
from test_segmentation import fixture
from test_pipeline import TestDirectory, tone


class PairPlanTests(unittest.TestCase):
    def test_full_imports_existing_boundaries_and_allows_long_video(self):
        project=fixture()
        project['duration']=7200
        plan=PairedSegmentPlan(project,0,2,full=True)
        self.assertEqual(plan.pairs,[(1,3),(4,7),(9,12)])
        self.assertEqual((plan.start,plan.end),(0,7200))
        before=plan.snapshot()
        plan.delete_sentences([0,1,2])
        self.assertFalse(plan.pairs)
        plan.undo()
        self.assertEqual(plan.snapshot(),before)
        plan.redo()
        for index in range(25):
            plan.add_sentence(index*120+1,index*120+3)
        plan.validate()
        self.assertEqual(plan.number(24),25)
        self.assertEqual(plan.blocks[-1]['end'],7200)
        self.assertTrue(plan.blocks[-1]['omit_row'])
        restored=PairedSegmentPlan(project,0,2,plan.blocks,full=True)
        self.assertEqual(restored.pairs,plan.pairs)
        project['segments']=[]
        self.assertEqual(PairedSegmentPlan(project,0,-1,full=True).pairs,[])

    def test_text_edits_translation_draft_and_history_follow_boundaries(self):
        project=fixture(); plan=PairedSegmentPlan(project,0,2,full=True)
        before=plan.snapshot()
        plan.edit_text(0,'text','Revised source.')
        self.assertEqual(plan.content(0)['target_text'],'')
        plan.edit_text(0,'target_text','修改的译文')
        restored=PairedSegmentPlan(project,0,2,plan.blocks,full=True)
        self.assertEqual(restored.content(0)['target_text'],'修改的译文')
        plan.undo(); self.assertEqual(plan.content(0)['target_text'],'')
        plan.undo(); self.assertEqual(plan.snapshot(),before)
        plan.redo(); plan.redo()
        previous=plan.snapshot(); plan.move_cut(1,3.2); plan.remember(previous)
        self.assertEqual(plan.content(0)['text'],'')
        plan.undo(); self.assertEqual(plan.content(0)['text'],'Revised source.')

    def test_range_addition_order_overlap_and_deleting_last_sentence(self):
        plan=PairedSegmentPlan(fixture(),0,1)
        before=plan.snapshot()
        with self.assertRaisesRegex(VoxlateError,'重叠'):
            plan.add_sentence(2,5)
        self.assertEqual(plan.snapshot(),before)
        self.assertEqual(plan.add_sentence(3.8,3.2),1)
        self.assertEqual(plan.pairs,[(1,3),(3.2,3.8),(4,7)])
        plan.delete_sentence(1)
        self.assertEqual(plan.snapshot(),before)
        plan.delete_sentence(1)
        plan.delete_sentence(0)
        plan.validate()
        self.assertEqual(plan.blocks,[dict(start=.5,end=8,enabled=False,text='',omit_row=True)])
        plan.undo()
        self.assertEqual(plan.pairs,[(1,3)])

    def test_half_gap_context_pairs_numbering_and_extension_undo(self):
        plan=PairedSegmentPlan(fixture(),1,1)
        self.assertEqual((plan.start,plan.end),(3.5,8))
        self.assertEqual(plan.number(0),2)
        self.assertEqual(plan.pairs,[(4,7)])
        initial=plan.snapshot()
        plan.extend_sentence('end')
        self.assertEqual(plan.pairs,[(4,7),(9,12)])
        self.assertEqual((plan.start,plan.end),(3.5,16))
        plan.undo()
        self.assertEqual(plan.snapshot(),initial)
        plan.redo()
        self.assertEqual(plan.last,2)

    def test_pairs_reform_after_deletion_and_gaps_never_become_rows(self):
        project=fixture()
        plan=PairedSegmentPlan(project,0,1)
        plan.merge(1)
        self.assertEqual(plan.pairs,[(1,4)])
        with self.assertRaisesRegex(VoxlateError,'配对'):
            plan.validate()
        plan.merge(1)
        self.assertEqual(plan.pairs,[(1,7)])
        plan.validate()
        blocks=plan.blocks
        texts=['Merged' if b['enabled'] else '' for b in blocks]
        translations=['合并' if b['enabled'] else '' for b in blocks]
        result,_=replace_segments(project,blocks,texts,translations)
        self.assertEqual([(s['start'],s['end']) for s in result['segments']],[(1,7),(9,12)])
        self.assertEqual(result['segments'][1]['source_text'],'Third.')

    def test_limit_and_out_of_range_cut_protection(self):
        rows=[dict(id=i+1,start=i*3.,end=i*3.+1,source_text='Synthetic',target_text='') for i in range(11)]
        project=dict(duration=34,segments=rows)
        with self.assertRaisesRegex(VoxlateError,'10 句'):
            PairedSegmentPlan(project,0,10)
        plan=PairedSegmentPlan(project,0,9)
        previous=plan.snapshot()
        with self.assertRaises(VoxlateError):
            plan.extend_sentence('end')
        self.assertEqual(plan.snapshot(),previous)
        with self.assertRaises(VoxlateError):
            plan.split(1.5)
        plan.move_cut(0,-10)
        plan.move_cut(1,100)
        plan.validate()
        self.assertGreaterEqual(plan.cuts[0],plan.start)
        self.assertLessEqual(plan.cuts[1],plan.cuts[2])


class PairDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app=QApplication.instance() or QApplication([])

    def wait_job(self, dialog):
        deadline=time.monotonic()+5
        while dialog.worker is not None and time.monotonic()<deadline:
            self.app.processEvents()
            time.sleep(.01)
        self.assertIsNone(dialog.worker)

    def test_row_drag_multiselection_batch_delete_undo_and_pan_keys(self):
        from PySide6.QtCore import Qt,QPoint
        from PySide6.QtTest import QTest
        from voxlate.segmentation_dialog import SegmentationDialog
        with TestDirectory() as folder:
            audio=Path(folder)/'synthetic.wav'; tone(audio,1)
            project=fixture(); project['duration']=120
            wave=dict(peaks=[.1]*120,step=1,start=0,end=120,duration=120)
            dialog=SegmentationDialog(project,0,2,audio,audio,[wave,wave],full=True)
            try:
                dialog.show(); self.app.processEvents()
                self.assertEqual(dialog.plan.pairs,[(1,3),(4,7),(9,12)])
                table=dialog.table
                first=table.visualItemRect(table.item(0,2)).center()
                last=table.visualItemRect(table.item(1,2)).center()
                QTest.mousePress(table.viewport(),Qt.MouseButton.LeftButton,pos=first)
                QTest.mouseMove(table.viewport(),last)
                QTest.mouseRelease(table.viewport(),Qt.MouseButton.LeftButton,pos=last)
                self.assertEqual(dialog.selected_rows(),[0,1])
                self.assertEqual(dialog.canvas.selection,{0,1})
                dialog.delete_button.click()
                self.assertEqual(dialog.plan.pairs,[(9,12)])
                dialog.undo_button.click()
                self.assertEqual(dialog.plan.pairs,[(1,3),(4,7),(9,12)])
                QTest.keyClick(table,Qt.Key.Key_A,Qt.KeyboardModifier.ControlModifier)
                self.assertEqual(dialog.selected_rows(),[0,1,2])
                QTest.keyClick(table,Qt.Key.Key_Delete)
                self.assertEqual(dialog.plan.pairs,[])
                self.assertTrue(dialog.apply_button.isEnabled())
                QTest.keyClick(table,Qt.Key.Key_Z)
                self.assertEqual(len(dialog.plan.pairs),3)
                before=dialog.plan.snapshot()
                QTest.keyClick(table,Qt.Key.Key_Right)
                self.assertEqual(dialog.canvas.left,24)
                QTest.keyClick(table,Qt.Key.Key_Left)
                self.assertEqual(dialog.canvas.left,0)
                self.assertEqual(dialog.plan.snapshot(),before)
                p=QPoint(round(dialog.overview.x(5)),35)
                QTest.mouseMove(dialog.overview,p)
                self.assertEqual(dialog.overview.cursor().shape(),Qt.CursorShape.OpenHandCursor)
                QTest.mousePress(dialog.overview,Qt.MouseButton.LeftButton,pos=p)
                self.assertEqual(dialog.overview.cursor().shape(),Qt.CursorShape.ClosedHandCursor)
                QTest.mouseRelease(dialog.overview,Qt.MouseButton.LeftButton,pos=p)
                self.assertEqual(dialog.overview.cursor().shape(),Qt.CursorShape.OpenHandCursor)
            finally:
                dialog.reject(); dialog.deleteLater(); self.app.processEvents()

    def test_editor_shortcuts_selection_toggle_text_edit_and_temporary_translation(self):
        from unittest.mock import Mock
        from PySide6.QtCore import Qt,QPoint
        from PySide6.QtWidgets import QLineEdit
        from PySide6.QtTest import QTest
        from voxlate.segmentation_dialog import SegmentationDialog
        with TestDirectory() as folder:
            audio=Path(folder)/'synthetic.wav'; tone(audio,20)
            project=fixture()
            wave=dict(peaks=[.1]*2000,step=.01,start=0,end=20,duration=20)
            translator=Mock(side_effect=lambda rows,context,original:[dict(r,target_text='临时译文') for r in rows])
            dialog=SegmentationDialog(project,0,2,audio,audio,[wave,wave],full=True,translate_selection=translator)
            try:
                dialog.show(); self.app.processEvents()
                QTest.mouseClick(dialog.table.viewport(),Qt.MouseButton.RightButton,pos=QPoint(10,10))
                self.assertFalse(dialog.selected_rows())
                self.assertFalse(dialog.delete_button.isEnabled())
                self.assertFalse(dialog.translate_button.isEnabled())
                QTest.keyClick(dialog.canvas,Qt.Key.Key_A)
                self.assertTrue(dialog.select_all_button.isChecked())
                self.assertEqual(dialog.selected_rows(),[0,1,2])
                dialog.select_all_button.click()
                self.assertFalse(dialog.selected_rows())
                self.assertFalse(dialog.select_all_button.isChecked())
                dialog.select_block(0)
                point=dialog.table.visualItemRect(dialog.table.item(0,2)).center()
                QTest.mouseClick(dialog.table.viewport(),Qt.MouseButton.LeftButton,pos=point)
                deadline=time.monotonic()+1
                editor=None
                while time.monotonic()<deadline:
                    self.app.processEvents()
                    editor=next((e for e in dialog.table.findChildren(QLineEdit) if e.isVisible()),None)
                    if editor: break
                    time.sleep(.01)
                self.assertIsNotNone(editor)
                QTest.keyClick(editor,Qt.Key.Key_A,Qt.KeyboardModifier.ControlModifier)
                QTest.keyClicks(editor,'A D T Z R revised.')
                self.assertEqual(len(dialog.plan.pairs),3)
                translator.assert_not_called()
                self.assertFalse(dialog.pending_play)
                QTest.keyClick(editor,Qt.Key.Key_Return)
                self.app.processEvents()
                self.assertEqual(dialog.plan.content(0)['text'],'A D T Z R revised.')
                self.assertEqual(dialog.plan.content(0)['target_text'],'')
                dialog.canvas.setFocus()
                QTest.keyClick(dialog.canvas,Qt.Key.Key_T)
                self.wait_job(dialog)
                self.assertEqual(dialog.plan.content(0)['target_text'],'临时译文')
                self.assertEqual(project,fixture())
                QTest.keyClick(dialog.canvas,Qt.Key.Key_Z)
                self.assertEqual(dialog.plan.content(0)['target_text'],'')
                for row in range(dialog.table.rowCount()):
                    for col in range(dialog.table.columnCount()):
                        self.assertEqual(dialog.table.item(row,col).toolTip(),'')
                self.assertFalse(hasattr(dialog,'previous_view'))
                dialog.canvas.span=5
                QTest.mouseMove(dialog.overview,QPoint(round(dialog.overview.x(15)),35))
                self.assertEqual(dialog.overview.cursor().shape(),Qt.CursorShape.ArrowCursor)
            finally:
                dialog.reject(); dialog.deleteLater(); self.app.processEvents()

    def test_video_preview_frame_seek_mute_and_unload(self):
        from PySide6.QtMultimedia import QMediaPlayer
        from voxlate.segmentation_dialog import SegmentationDialog
        from voxlate.media import run_process
        with TestDirectory() as folder:
            root=Path(folder); video=root/'synthetic.mp4'; audio=root/'synthetic.wav'
            run_process(['ffmpeg','-v','error','-f','lavfi','-i','testsrc2=size=320x180:rate=10',
                '-t','4','-c:v','mpeg4',str(video)],'synthetic preview fixture')
            tone(audio,4)
            project=dict(input=str(video),duration=4,segments=[])
            wave=dict(peaks=[.1]*400,step=.01,start=0,end=4,duration=4)
            dialog=SegmentationDialog(project,0,-1,audio,audio,[wave,wave],full=True)
            try:
                dialog.show()
                preview=dialog.video_preview
                deadline=time.monotonic()+8
                while not preview.video.videoSink().videoFrame().isValid() and time.monotonic()<deadline:
                    self.app.processEvents(); time.sleep(.02)
                self.assertTrue(preview.video.videoSink().videoFrame().isValid())
                self.assertIsNone(preview.player.audioOutput())
                dialog.seek(2)
                self.assertEqual(preview.position,2000)
                deadline=time.monotonic()+3
                while abs(preview.player.position()-2000)>100 and time.monotonic()<deadline:
                    self.app.processEvents(); time.sleep(.02)
                self.assertAlmostEqual(preview.player.position(),2000,delta=100)
                dialog.audio.setMuted(True)
                dialog.seek(.5)
                dialog.toggle_play()
                deadline=time.monotonic()+1.2
                while time.monotonic()<deadline:
                    self.app.processEvents(); time.sleep(.02)
                self.assertGreater(dialog.player.position(),1100)
                self.assertAlmostEqual(preview.player.position(),dialog.player.position(),delta=300)
                dialog.stop_playback()
                self.assertNotEqual(preview.player.playbackState(),QMediaPlayer.PlaybackState.PlayingState)
                dialog.preview_check.setChecked(False)
                self.assertTrue(preview.player.source().isEmpty())
                self.assertEqual(preview.player.playbackState(),QMediaPlayer.PlaybackState.StoppedState)
                dialog.preview_check.setChecked(True)
                self.assertFalse(preview.player.source().isEmpty())
            finally:
                dialog.reject()
                self.assertTrue(dialog.video_preview.player.source().isEmpty())
                dialog.deleteLater(); self.app.processEvents()

    def test_full_navigation_boundary_seek_and_transparent_drag(self):
        from PySide6.QtCore import Qt,QPoint,QPointF
        from PySide6.QtGui import QWheelEvent
        from PySide6.QtTest import QTest
        from voxlate.segmentation_dialog import SegmentationDialog
        with TestDirectory() as folder:
            audio=Path(folder)/'synthetic.wav'
            tone(audio,1)
            project=dict(segments=[],duration=3600)
            wave=dict(peaks=[.3]*3600,step=1,start=0,end=3600,duration=3600)
            dialog=SegmentationDialog(project,0,2,audio,audio,[wave,wave],full=True)
            try:
                dialog.show(); self.app.processEvents()
                self.assertEqual(dialog.plan.pairs,[])
                self.assertFalse(dialog.apply_button.isEnabled())
                self.assertTrue(dialog.windowFlags() & Qt.WindowType.WindowMaximizeButtonHint)
                point=lambda t: QPoint(round(dialog.canvas.x(t)),70)
                QTest.mousePress(dialog.canvas,Qt.MouseButton.LeftButton,pos=point(2))
                QTest.mouseMove(dialog.canvas,point(6))
                # An active range remains translucent even after cut handles were painted.
                pixel=dialog.canvas.grab().toImage().pixelColor(QPoint(round(dialog.canvas.x(4)),28))
                self.assertGreater(pixel.red(),120)
                self.assertGreater(pixel.green(),130)
                QTest.mouseRelease(dialog.canvas,Qt.MouseButton.LeftButton,pos=point(6))
                self.assertEqual(len(dialog.plan.pairs),1)
                self.assertTrue(dialog.apply_button.isEnabled())
                for cut,target in ((1,7),(0,3)):
                    dialog.canvas.position=20
                    QTest.mousePress(dialog.canvas,Qt.MouseButton.LeftButton,pos=point(dialog.plan.cuts[cut]))
                    QTest.mouseMove(dialog.canvas,point(target))
                    QTest.mouseRelease(dialog.canvas,Qt.MouseButton.LeftButton,pos=point(target))
                    self.assertEqual(dialog.canvas.position,dialog.plan.cuts[0])
                    self.assertAlmostEqual(dialog.plan.cuts[cut],target,delta=.05)
                before=dialog.plan.snapshot()
                overview=dialog.overview
                QTest.mouseClick(overview,Qt.MouseButton.LeftButton,pos=QPoint(round(overview.x(1800)),35))
                self.assertAlmostEqual(dialog.canvas.left+dialog.canvas.span/2,1800,delta=4)
                p=QPointF(overview.x(2700),35)
                wheel=QWheelEvent(p,p,QPoint(),QPoint(0,120),Qt.MouseButton.NoButton,Qt.KeyboardModifier.NoModifier,Qt.ScrollPhase.NoScrollPhase,False)
                overview.wheelEvent(wheel)
                self.assertEqual(dialog.canvas.span,24)
                self.assertTrue(dialog.canvas.left <= 2700 <= dialog.canvas.left+dialog.canvas.span)
                self.assertEqual(dialog.plan.snapshot(),before)
                for index in range(1,22):
                    dialog.plan.add_sentence(index*60,index*60+3)
                dialog.render()
                self.assertEqual(dialog.table.rowCount(),22)
                self.assertIsNotNone(dialog.table.item(21,2))
                self.assertEqual(dialog.table.item(0,0).foreground().color(),dialog.table.item(10,0).foreground().color())
                dialog.delete_sentence(21)
                self.assertEqual(dialog.table.rowCount(),21)
            finally:
                dialog.reject(); dialog.deleteLater(); self.app.processEvents()

    def test_full_detail_loading_cancels_before_dialog_closes(self):
        from voxlate.segmentation_dialog import SegmentationDialog
        from voxlate.media import check_cancelled
        with TestDirectory() as folder:
            audio=Path(folder)/'synthetic.wav'; tone(audio,1)
            project=dict(duration=3600,segments=[])
            wave=dict(peaks=[.1]*3600,step=1,start=0,end=3600,duration=3600)
            ready=threading.Event()
            def load(*args):
                ready.set()
                while True:
                    check_cancelled(); time.sleep(.01)
            dialog=SegmentationDialog(project,0,-1,audio,audio,[wave,wave],full=True,load_waves=load)
            dialog.show(); dialog.load_detail()
            self.assertTrue(ready.wait(2))
            dialog.reject()
            deadline=time.monotonic()+5
            while dialog.detail_worker is not None and time.monotonic()<deadline:
                self.app.processEvents(); time.sleep(.01)
            self.assertIsNone(dialog.detail_worker)
            self.assertFalse(dialog.isVisible())
            dialog.deleteLater(); self.app.processEvents()

    def test_context_button_stays_beside_time_selection_and_tracks_scroll(self):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest
        from PySide6.QtWidgets import QTableWidgetItem,QPushButton
        from voxlate.sentence_table import SentenceTable
        table=SentenceTable(30,3)
        for row in range(30):
            table.setItem(row,0,QTableWidgetItem('00:00:01 – 03'))
        button=QPushButton('手动分句...')
        table.set_range_button(button)
        table.timeRangeSelected.connect(table.show_range_button)
        table.resize(500,230)
        table.show()
        self.app.processEvents()
        try:
            point=table.visualItemRect(table.item(1,0)).center()
            QTest.mouseClick(table.viewport(),Qt.MouseButton.LeftButton,pos=point)
            self.app.processEvents()
            rect=table.visualItemRect(table.item(1,0))
            self.assertTrue(button.isVisible())
            self.assertLessEqual(abs(button.x()-rect.right()),8)
            self.assertLessEqual(abs(button.geometry().center().y()-rect.center().y()),2)
            table.verticalScrollBar().setValue(20)
            self.app.processEvents()
            self.assertFalse(button.isVisible())
        finally:
            table.close()
            table.deleteLater()
            self.app.processEvents()

    def test_drag_add_delete_undo_and_preview_stop(self):
        from PySide6.QtCore import Qt, QPoint
        from PySide6.QtTest import QTest
        from PySide6.QtWidgets import QPushButton
        from voxlate.segmentation_dialog import SegmentationDialog,waveform_peaks
        with TestDirectory() as folder:
            audio=Path(folder)/'synthetic.wav'
            tone(audio,20)
            wave=waveform_peaks(audio,start=.5,end=8)
            dialog=SegmentationDialog(fixture(),0,1,audio,audio,[wave,wave])
            try:
                dialog.show()
                self.app.processEvents()
                self.assertEqual([dialog.table.horizontalHeaderItem(i).text() for i in range(dialog.table.columnCount())],['句号','时间','原文','译文','','',''])
                self.assertFalse(any(b.text().startswith('切开') for b in dialog.findChildren(QPushButton)))
                self.assertEqual(dialog.apply_button.toolTip(),'')
                point=lambda t: QPoint(round(dialog.canvas.x(t)),100)
                QTest.mousePress(dialog.canvas,Qt.MouseButton.LeftButton,pos=point(3.2))
                QTest.mouseMove(dialog.canvas,point(3.8))
                QTest.mouseRelease(dialog.canvas,Qt.MouseButton.LeftButton,pos=point(3.8))
                self.assertEqual(len(dialog.plan.pairs),3)
                lo,hi=dialog.plan.pairs[1]
                self.assertAlmostEqual(lo,3.2,delta=.02)
                self.assertAlmostEqual(hi,3.8,delta=.02)
                QTest.keyClick(dialog.canvas,Qt.Key.Key_D)
                self.assertEqual(dialog.plan.pairs,[(1,3),(4,7)])
                QTest.keyClick(dialog.canvas,Qt.Key.Key_Z)
                self.assertEqual(len(dialog.plan.pairs),3)
                QTest.keyClick(dialog.canvas,Qt.Key.Key_R)
                self.assertEqual(len(dialog.plan.pairs),2)
                # Pending playback must also be cancellable before media finishes loading.
                dialog.pending_position=1
                dialog.preview_row(0)
                self.assertEqual(dialog.play.accessibleName(),'停止试听')
                dialog.preview_row(0)
                self.assertIsNone(dialog.preview_range)
                self.assertFalse(dialog.pending_play)
                self.assertEqual(dialog.play.accessibleName(),'播放或停止')
                dialog.delete_button.click()
                self.assertEqual(dialog.plan.pairs,[(4,7)])
                dialog.delete_sentence(0)
                self.assertEqual(dialog.table.rowCount(),0)
                self.assertTrue(dialog.apply_button.isEnabled())
                dialog.plan.validate()
            finally:
                dialog.reject()
                dialog.deleteLater()
                self.app.processEvents()

    def test_cancel_wave_loading_waits_for_worker(self):
        from voxlate.segmentation_dialog import SegmentationDialog,waveform_peaks
        from voxlate.media import check_cancelled
        with TestDirectory() as folder:
            audio=Path(folder)/'synthetic.wav'
            tone(audio,20)
            wave=waveform_peaks(audio,start=.5,end=8)
            ready=threading.Event()
            def load(*args):
                ready.set()
                deadline=time.monotonic()+3
                while time.monotonic()<deadline:
                    check_cancelled()
                    time.sleep(.01)
                return [wave,wave]
            dialog=SegmentationDialog(fixture(),0,1,audio,audio,[wave,wave],load_waves=load)
            dialog.show()
            dialog.extend('end')
            ready.wait(1)
            dialog.reject()
            self.wait_job(dialog)
            self.assertFalse(dialog.isVisible())
            dialog.deleteLater()
            self.app.processEvents()

    def test_failed_wave_extension_restores_range_and_history(self):
        from voxlate.segmentation_dialog import SegmentationDialog,waveform_peaks
        with TestDirectory() as folder:
            audio=Path(folder)/'synthetic.wav'
            tone(audio,20)
            wave=waveform_peaks(audio,start=.5,end=8)
            def fail(start,end):
                raise OSError('Synthetic read failure')
            dialog=SegmentationDialog(fixture(),0,1,audio,audio,[wave,wave],load_waves=fail)
            initial=dialog.plan.snapshot()
            try:
                dialog.extend('end')
                self.wait_job(dialog)
                self.assertEqual(dialog.plan.snapshot(),initial)
                self.assertEqual(dialog.plan.undo_stack,[])
                self.assertEqual(dialog.canvas.waveform,wave)
                self.assertIn('Synthetic read failure',dialog.message.text())
            finally:
                dialog.reject()
                dialog.deleteLater()
                self.app.processEvents()
