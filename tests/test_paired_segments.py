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
                self.assertNotIn('识别',[dialog.table.horizontalHeaderItem(i).text() for i in range(5)])
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
                self.assertEqual(dialog.table.cellWidget(0,2).accessibleName(),'停止试听')
                dialog.preview_row(0)
                self.assertIsNone(dialog.preview_range)
                self.assertFalse(dialog.pending_play)
                self.assertIn('试听第',dialog.table.cellWidget(0,2).accessibleName())
                dialog.table.cellWidget(0,4).click()
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
