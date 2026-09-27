import unittest

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from voxlate.player import ClickSlider


class PlayerControls(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_volume_click_jumps_and_drag_continues(self):
        slider = ClickSlider(Qt.Orientation.Horizontal)
        slider.setRange(0, 100)
        slider.resize(200, 30)
        slider.setValue(80)
        slider.show()
        self.app.processEvents()
        QTest.mouseClick(slider, Qt.MouseButton.LeftButton, pos=QPoint(40, 15))
        self.assertLess(slider.value(), 25)
        QTest.mousePress(slider, Qt.MouseButton.LeftButton, pos=QPoint(100, 15))
        self.assertAlmostEqual(slider.value(), 50, delta=2)
        QTest.mouseMove(slider, QPoint(180, 15))
        QTest.mouseRelease(slider, Qt.MouseButton.LeftButton, pos=QPoint(180, 15))
        self.assertGreater(slider.value(), 90)
        self.assertFalse(slider.isSliderDown())
        slider.close()
        slider.deleteLater()
