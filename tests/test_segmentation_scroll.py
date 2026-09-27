"""Opening the range editor must not move the table behind it."""
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from test_pipeline import TestDirectory, tone
from voxlate.common import write_json


class SegmentationScrollTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_open_and_cancel_preserve_main_table_scroll(self):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest
        from PySide6.QtWidgets import QDialog, QMessageBox
        from voxlate.gui import MainWindow
        from voxlate.segmentation_dialog import SegmentationDialog
        with TestDirectory() as directory:
            root = Path(directory)
            path = root/'synthetic.mp4.voxlate/en/project.json'
            project = dict(schema_version=1, name='voxlate', input=str(root/'synthetic.mp4'),
                           duration=240, source_lang='en', segments=[
                               dict(id=i+1, start=i*3.+.5, end=i*3.+2., source_text=f'Synthetic {i+1}.',
                                    target_text=f'合成测试{i+1}。', enabled=True)
                               for i in range(60)])
            write_json(path, project)
            audio = path.parent/'original.wav'
            tone(audio, 1)
            waves = dict(peaks=[0.1]*400, step=.5, start=0., end=240., duration=240.)
            window = MainWindow(root/'app-data', auto_check=False)
            positions, observed = [], []
            try:
                window.load_project(path)
                window.show()
                window.activateWindow()
                self.app.processEvents()
                table = window.table
                table.setCurrentCell(2, 0)
                table.setFocus()
                table.verticalScrollBar().setValue(20)
                self.app.processEvents()
                expected = table.verticalScrollBar().value()
                self.assertGreater(expected, 10)
                point = table.visualItemRect(table.item(24, 0)).center()
                QTest.mouseClick(table.viewport(), Qt.MouseButton.LeftButton, pos=point)
                self.app.processEvents()
                self.assertEqual(table.verticalScrollBar().value(), expected)
                self.assertTrue(window.manual_segmentation_button.isVisible())
                table.verticalScrollBar().valueChanged.connect(positions.append)
                def editor(dialog):
                    observed.append(table.verticalScrollBar().value())
                    dialog.show()
                    self.app.processEvents()
                    observed.append(table.verticalScrollBar().value())
                    dialog.reject()
                    return QDialog.DialogCode.Rejected
                with patch('voxlate.gui.VideoDubPipeline.cached_media', return_value=(audio,audio,audio)), \
                     patch('voxlate.segmentation_dialog.cached_waveform', return_value=waves), \
                     patch.object(SegmentationDialog, 'exec', new=editor), \
                     patch.object(QMessageBox, 'warning') as warning, patch.object(QMessageBox, 'critical') as critical:
                    QTest.mouseClick(window.manual_segmentation_button, Qt.MouseButton.LeftButton)
                    deadline = time.monotonic()+5
                    while time.monotonic() < deadline and (window.task is not None or not observed):
                        self.app.processEvents()
                        time.sleep(.01)
                    self.app.processEvents()
                    warning.assert_not_called()
                    critical.assert_not_called()
                    self.assertIsNone(window.task)
                    self.assertEqual(observed, [expected, expected], f'scroll transitions: {positions}')
                    self.assertEqual(table.verticalScrollBar().value(), expected)
                    self.assertTrue(all(value == expected for value in positions), positions)
            finally:
                for dialog in window.findChildren(SegmentationDialog):
                    dialog.reject()
                if window.task:
                    window.cancel_task()
                    deadline = time.monotonic()+5
                    while window.task and time.monotonic() < deadline:
                        self.app.processEvents()
                        time.sleep(.01)
                window.close()
                window.deleteLater()
                self.app.processEvents()
