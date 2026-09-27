"""Shared compact controls for the desktop interface."""
from PySide6.QtCore import Qt
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QComboBox, QStylePainter, QStyleOptionComboBox, QStyle


class CenteredComboBox(QComboBox):
    """Center the closed label while retaining normal full-cell popup interaction."""
    def paintEvent(self, event):
        painter = QStylePainter(self)
        option = QStyleOptionComboBox()
        self.initStyleOption(option)
        text, option.currentText = option.currentText, ''
        painter.drawComplexControl(QStyle.ComplexControl.CC_ComboBox, option)
        rect = self.style().subControlRect(QStyle.ComplexControl.CC_ComboBox, option,
            QStyle.SubControl.SC_ComboBoxEditField, self)
        text = self.fontMetrics().elidedText(text, Qt.TextElideMode.ElideRight, rect.width())
        painter.drawItemText(rect, Qt.AlignmentFlag.AlignCenter, option.palette,
            self.isEnabled(), text, QPalette.ColorRole.Text)
