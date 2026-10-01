"""Shared compact controls for the desktop interface."""
from PySide6.QtCore import Qt, QSize, QRectF
from PySide6.QtGui import QPalette, QPixmap, QPainter, QColor, QPen, QIcon
from PySide6.QtWidgets import QComboBox, QStylePainter, QStyleOptionComboBox, QStyle, QPushButton, QWidget, QHBoxLayout


def action_icon(kind):
    pixmap = QPixmap(24, 24)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(QColor('#526885'), 1.7))
    if kind == 'delete':
        for line in ((5,7,19,7),(9,4,15,4),(7,8,8,20),(17,8,16,20),(8,20,16,20),(10,10,10,17),(14,10,14,17)):
            painter.drawLine(*line)
    elif kind in ('include', 'exclude'):
        painter.drawEllipse(QRectF(4,4,16,16))
        if kind == 'exclude':
            painter.drawLine(7,17,17,7)
        else:
            painter.drawLine(7,12,11,16)
            painter.drawLine(11,16,17,8)
    else:
        painter.drawText(QRectF(0,0,15,18), Qt.AlignmentFlag.AlignCenter, '文')
        painter.drawText(QRectF(10,8,14,16), Qt.AlignmentFlag.AlignCenter, 'A')
    painter.end()
    return QIcon(pixmap)


def action_button(icon, name, callback):
    button = QPushButton()
    button.setIcon(icon)
    button.setIconSize(QSize(16,16))
    button.setFixedSize(30,26)
    button.setStyleSheet('QPushButton {padding:0;}')
    button.setAccessibleName(name)
    button.setAutoDefault(False)
    button.clicked.connect(callback)
    return button


def action_cell(button):
    cell = QWidget()
    layout = QHBoxLayout(cell)
    layout.setContentsMargins(2,0,2,0)
    layout.addWidget(button, alignment=Qt.AlignmentFlag.AlignCenter)
    cell.button = button
    return cell


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
