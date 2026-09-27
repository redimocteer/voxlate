"""Time-column range gestures and compact, discoverable header help."""
from PySide6.QtCore import Qt, Signal, QTimer, QRectF, QEvent, QSize
from PySide6.QtGui import QColor, QPen
from PySide6.QtWidgets import QTableWidget, QTableWidgetSelectionRange, QHeaderView, QToolTip, QStyleOptionHeader, QStyle


HEADER_HELP = {
    0: '单击时间或拖选连续几句，再点“手动分句…”。',
    1: '点击原文选取／舍弃此句。变淡表示保留原声。',
    2: '双击修改译文并自动保存。',
}


class InfoHeader(QHeaderView):
    def __init__(self, parent):
        super().__init__(Qt.Orientation.Horizontal, parent)
        self.setSectionsClickable(False)

    def info_rect(self, index):
        text = str(self.model().headerData(index, Qt.Orientation.Horizontal) or '')
        width = self.sectionSize(index)
        x = self.sectionViewportPosition(index)
        font = self.font()
        font.setBold(True)
        from PySide6.QtGui import QFontMetrics
        text_width = QFontMetrics(font).horizontalAdvance(text)
        return QRectF(x+(width+text_width+20)/2-14, (self.height()-14)/2, 14, 14)

    def sectionSizeFromContents(self, index):
        size = super().sectionSizeFromContents(index)
        return QSize(size.width()+24, size.height()) if index in HEADER_HELP else size

    def paintSection(self, painter, rect, logical):
        painter.save()
        if logical in HEADER_HELP:
            option = QStyleOptionHeader()
            self.initStyleOption(option)
            option.rect, option.section = rect, logical
            option.text = ''
            self.style().drawControl(QStyle.ControlElement.CE_Header, option, painter, self)
            painter.restore()
            painter.save()
            text = str(self.model().headerData(logical, Qt.Orientation.Horizontal) or '')
            font = painter.font()
            font.setBold(True)
            painter.setFont(font)
            painter.setPen(QColor('#153253'))
            painter.drawText(rect.adjusted(0, 0, -20, 0), Qt.AlignmentFlag.AlignCenter, text)
        else:
            super().paintSection(painter, rect, logical)
        painter.restore()
        if logical in HEADER_HELP:
            painter.save()
            painter.setRenderHint(painter.RenderHint.Antialiasing)
            painter.setPen(QPen(QColor('#6a83a7'), 1))
            circle = self.info_rect(logical).adjusted(1, 1, -1, -1)
            painter.drawEllipse(circle)
            font = painter.font()
            font.setPixelSize(10)
            font.setBold(True)
            painter.setFont(font)
            painter.drawText(circle, Qt.AlignmentFlag.AlignCenter, 'i')
            painter.restore()

    def viewportEvent(self, event):
        if event.type() in (QEvent.Type.ToolTip, QEvent.Type.MouseButtonPress):
            point = event.pos() if event.type() == QEvent.Type.ToolTip else event.position().toPoint()
            index = self.logicalIndexAt(point)
            if index in HEADER_HELP:
                QToolTip.showText(self.viewport().mapToGlobal(point), HEADER_HELP[index], self)
                event.accept()
                return True
        return super().viewportEvent(event)


class SentenceTable(QTableWidget):
    timeRangeSelected = Signal(int, int)

    def __init__(self, rows, columns):
        super().__init__(rows, columns)
        self.setHorizontalHeader(InfoHeader(self))
        self.time_anchor = None
        self.time_last = None
        self.range_button = None
        self.button_range = None
        self.verticalScrollBar().valueChanged.connect(self.position_range_button)
        self.horizontalScrollBar().valueChanged.connect(self.position_range_button)
        self.horizontalHeader().sectionResized.connect(self.position_range_button)

    def set_range_button(self, button):
        self.range_button = button
        button.setParent(self.viewport())
        # This floating action must not take focus from the table. Disabling a
        # focused child for preparation makes Qt refocus/scroll to a stale cell.
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        button.setStyleSheet(
            'QPushButton {border: 2px solid #5175c8; background: white; color: #234680; padding: 6px 12px;}'
            'QPushButton:hover {border-color: #285bd4; background: #edf3ff;}'
            'QPushButton:pressed {background: #dce7ff;}'
            'QPushButton:disabled {border-color: #9aaed5; color: #8292ad; background: #f1f3f6;}')
        button.hide()

    def show_range_button(self, first, last):
        self.button_range = (first, last)
        self.position_range_button()

    def position_range_button(self, *_):
        if not self.range_button or not self.button_range:
            return
        first, last = self.button_range
        # Use the nearest visible selected row; do not float away from the selection.
        visible = [row for row in range(first, min(last+1, self.rowCount()))
                   if self.rowViewportPosition(row)+self.rowHeight(row) > 0
                   and self.rowViewportPosition(row) < self.viewport().height()]
        selected = any(index.column() == 0 for index in self.selectedIndexes())
        if not visible or not selected:
            self.range_button.hide()
            return
        row = visible[-1]
        rect = self.visualItemRect(self.item(row, 0))
        self.range_button.adjustSize()
        x = min(self.viewport().width()-self.range_button.width(), rect.right()+6)
        y = max(0, min(self.viewport().height()-self.range_button.height(),
                       rect.center().y()-self.range_button.height()//2))
        self.range_button.move(max(0, x), y)
        self.range_button.show()
        self.range_button.raise_()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.position_range_button()

    def mousePressEvent(self, event):
        index = self.indexAt(event.position().toPoint())
        if event.button() == Qt.MouseButton.LeftButton and index.isValid() and index.column() == 0:
            if self.range_button:
                self.range_button.hide()
            self.time_anchor = self.time_last = index.row()
            self.select_times(index.row())
            event.accept()
            return
        super().mousePressEvent(event)

    def select_times(self, row):
        self.time_last = row
        self.clearSelection()
        self.setRangeSelected(QTableWidgetSelectionRange(min(self.time_anchor, row), 0, max(self.time_anchor, row), 0), True)

    def mouseMoveEvent(self, event):
        if self.time_anchor is not None:
            point = event.position().toPoint()
            if point.y() < 8:
                self.verticalScrollBar().setValue(self.verticalScrollBar().value()-1)
            elif point.y() > self.viewport().height()-8:
                self.verticalScrollBar().setValue(self.verticalScrollBar().value()+1)
            row = self.rowAt(max(0, min(self.viewport().height()-1, point.y())))
            if row >= 0:
                self.select_times(row)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self.time_anchor is not None and event.button() == Qt.MouseButton.LeftButton:
            first, last = sorted((self.time_anchor, self.time_last))
            self.time_anchor = self.time_last = None
            QTimer.singleShot(0, lambda: self.timeRangeSelected.emit(first, last))
            event.accept()
            return
        super().mouseReleaseEvent(event)
