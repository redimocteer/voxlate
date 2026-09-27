import unittest
from PySide6.QtCore import Qt, QPoint, QPointF
from PySide6.QtGui import QMouseEvent, QWheelEvent
from PySide6.QtWidgets import QApplication
from voxlate.timeline import RangeTimeline


class TimelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.bar = RangeTimeline()
        self.bar.resize(1024, 58)
        self.bar.set_duration(100000)
        self.seeks = []
        self.bar.seekRequested.connect(self.seeks.append)

    def tearDown(self):
        self.bar.deleteLater()
        self.app.processEvents()

    def mouse(self, kind, milliseconds, y=28):
        point = QPointF(self.bar.x_at(milliseconds), y)
        button = Qt.MouseButton.NoButton if kind == QMouseEvent.Type.MouseMove else Qt.MouseButton.LeftButton
        buttons = Qt.MouseButton.NoButton if kind == QMouseEvent.Type.MouseButtonRelease else Qt.MouseButton.LeftButton
        event = QMouseEvent(kind, point, point, button, buttons, Qt.KeyboardModifier.NoModifier)
        QApplication.sendEvent(self.bar, event)

    def drag(self, start, end, y=28):
        self.mouse(QMouseEvent.Type.MouseButtonPress, start, y)
        self.mouse(QMouseEvent.Type.MouseMove, end, y)
        self.mouse(QMouseEvent.Type.MouseButtonRelease, end, y)

    def test_drag_selects_both_directions_click_only_seeks(self):
        self.drag(20000, 27000)
        self.assertEqual(self.bar.selection, (20000, 27000))
        self.assertEqual(self.seeks[-1], 27000)
        self.drag(60000, 52000)
        self.assertEqual(self.bar.selection, (52000, 60000))
        self.mouse(QMouseEvent.Type.MouseButtonPress, 30000)
        self.mouse(QMouseEvent.Type.MouseButtonRelease, 30000)
        self.assertEqual(self.seeks[-1], 30000)
        self.assertEqual(self.bar.selection, (52000, 60000))

    def test_handles_resize_and_can_cross_without_getting_stuck(self):
        self.bar.set_selection(20000, 27000)
        self.drag(20000, 23000)
        self.assertEqual(self.bar.selection, (23000, 27000))
        self.drag(27000, 32000)
        self.assertEqual(self.bar.selection, (23000, 32000))
        self.drag(23000, 40000)
        self.assertEqual(self.bar.selection, (32000, 40000))

    def test_playhead_drag_and_viewer_mode_do_not_change_range(self):
        self.bar.set_selection(20000, 27000)
        self.drag(10000, 50000, y=8)
        self.assertEqual(self.seeks[-1], 50000)
        self.assertEqual(self.bar.selection, (20000, 27000))
        self.bar.selectable = False
        self.bar.selection = None
        self.drag(10000, 30000)
        self.assertEqual(self.seeks[-1], 30000)
        self.assertIsNone(self.bar.selection)

    def test_zoom_allows_short_selection_in_long_video_and_clamps_edges(self):
        self.bar.set_duration(7200000)
        point = QPointF(self.bar.width()/2, 28)
        for _ in range(15):
            event = QWheelEvent(point, point, QPoint(), QPoint(0, 120), Qt.MouseButton.NoButton,
                                Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
            QApplication.sendEvent(self.bar, event)
        self.assertLess(self.bar.view_end - self.bar.view_start, 40000)
        self.drag(3600000, 3605000)
        self.assertAlmostEqual(self.bar.selection[1] - self.bar.selection[0], 5000, delta=2)
        self.bar.reset_zoom()
        self.assertEqual((self.bar.view_start, self.bar.view_end), (0, 7200000))
        self.assertEqual(self.bar.time_at(-100), 0)
        self.assertEqual(self.bar.time_at(100000), 7200000)
