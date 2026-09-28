import os
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QTableWidgetItem, QTableWidgetSelectionRange, QLineEdit

from voxlate.sentence_table import SentenceTable


class SentenceCopyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.table = SentenceTable(3, 6)
        for row in range(3):
            for column, text in enumerate(('00:00', f'Source {row + 1}', f'译文 {row + 1}', '▶', '●', 'A')):
                self.table.setItem(row, column, QTableWidgetItem(text))
        self.table.show()
        self.app.processEvents()
        self.app.clipboard().setText('unchanged')

    def tearDown(self):
        self.table.close()
        self.table.deleteLater()
        self.app.processEvents()

    def copy(self):
        QTest.keyClick(self.table, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
        return self.app.clipboard().text()

    def test_source_and_translation_copy_separately(self):
        for column, expected in ((1, 'Source 1'), (2, '译文 1')):
            self.table.clearSelection()
            self.table.setCurrentCell(0, column)
            self.assertEqual(self.copy(), expected)

    def test_mouse_rectangle_selects_multiple_text_cells(self):
        start = self.table.visualItemRect(self.table.item(0, 1)).center()
        end = self.table.visualItemRect(self.table.item(2, 2)).center()
        QTest.mousePress(self.table.viewport(), Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(self.table.viewport(), end)
        QTest.mouseRelease(self.table.viewport(), Qt.MouseButton.LeftButton, pos=end)
        self.assertEqual(self.copy(), 'Source 1\t译文 1\nSource 2\t译文 2\nSource 3\t译文 3')

    def test_multiple_rows_copy_in_order_without_controls_or_timestamps(self):
        self.table.setRangeSelected(QTableWidgetSelectionRange(0, 0, 2, 5), True)
        self.assertEqual(self.copy(), 'Source 1\t译文 1\nSource 2\t译文 2\nSource 3\t译文 3')

    def test_disjoint_selection_preserves_rows_and_empty_text(self):
        self.table.item(2, 2).setText('')
        for row in (2, 0):
            self.table.item(row, 2).setSelected(True)
        self.assertEqual(self.copy(), '译文 1\n')

    def test_time_selection_and_no_selection_leave_clipboard_unchanged(self):
        self.assertEqual(self.copy(), 'unchanged')
        self.table.setCurrentCell(0, 0)
        self.assertEqual(self.copy(), 'unchanged')

    def test_translation_editor_copies_only_selected_characters(self):
        self.table.setCurrentCell(0, 2)
        self.table.editItem(self.table.item(0, 2))
        self.app.processEvents()
        editor = self.table.findChild(QLineEdit)
        self.assertIsNotNone(editor)
        editor.setSelection(0, 2)
        QTest.keyClick(editor, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(self.app.clipboard().text(), '译文')
