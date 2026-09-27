"""Video seek bar with draggable reference boundaries and pointer-centred zoom."""
from PySide6.QtCore import Qt, Signal, QPointF, QRectF
from PySide6.QtGui import QPainter, QColor, QPen, QPolygonF
from PySide6.QtWidgets import QWidget


def time_text(milliseconds, precise=False):
    milliseconds = max(0, int(milliseconds))
    seconds = milliseconds // 1000
    text = f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"
    return text + f".{milliseconds % 1000:03d}" if precise else text


class RangeTimeline(QWidget):
    seekRequested = Signal(int)
    selectionChanged = Signal(object)
    interactionStarted = Signal()

    def __init__(self, parent=None, selectable=True):
        super().__init__(parent)
        self.duration = self.position = 0
        self.selection = None
        self.view_start = self.view_end = 0
        self.selectable = selectable
        self.mode = None
        self.anchor = self.press_x = 0
        self.setMinimumHeight(58)
        self.setMinimumWidth(280)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName("播放进度及音色范围" if selectable else "播放进度")
        self.setToolTip("单击跳转；拖动选择音色范围，拖两端调整；滚轮缩放，双击恢复全片。" if selectable else "单击或拖动跳转；滚轮缩放，双击恢复全片。")

    def x_at(self, milliseconds):
        return 12 + (milliseconds - self.view_start) * max(1, self.width() - 24) / max(1, self.view_end - self.view_start)

    def time_at(self, x):
        ratio = min(1, max(0, (x - 12) / max(1, self.width() - 24)))
        return round(self.view_start + ratio * (self.view_end - self.view_start))

    def set_duration(self, duration):
        self.duration = max(0, int(duration))
        self.reset_zoom()
        if self.selection and self.duration:
            self.set_selection(*self.selection)

    def set_position(self, position):
        self.position = max(0, min(self.duration, int(position)))
        self.update()

    def set_selection(self, start, end):
        start, end = sorted((max(0, int(start)), max(0, int(end))))
        if self.duration:
            start, end = min(start, self.duration), min(end, self.duration)
        self.selection = (start, end)
        self.selectionChanged.emit(self.selection)
        self.update()

    def reset_zoom(self):
        self.view_start, self.view_end = 0, self.duration
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#dce3ed"))
        painter.drawRoundedRect(QRectF(12, 23, self.width() - 24, 10), 4, 4)
        if self.duration:
            painter.save()
            painter.setClipRect(QRectF(7, 0, self.width() - 14, 42))
            x = self.x_at(self.position)
            painter.setBrush(QColor("#91aaf1"))
            painter.drawRoundedRect(QRectF(12, 23, max(0, x - 12), 10), 4, 4)
            if self.selection:
                left, right = (self.x_at(value) for value in self.selection)
                painter.setBrush(QColor("#55315de2"))
                painter.drawRect(QRectF(left, 17, right - left, 22))
                painter.setBrush(QColor("#315de2"))
                for edge in (left, right):
                    painter.drawRoundedRect(QRectF(edge - 5, 16, 10, 24), 3, 3)
                    painter.setPen(QPen(QColor("white"), 1))
                    painter.drawLine(QPointF(edge, 22), QPointF(edge, 34))
                    painter.setPen(Qt.PenStyle.NoPen)
            painter.setPen(QPen(QColor("#192e58"), 2))
            painter.drawLine(QPointF(x, 13), QPointF(x, 39))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#192e58"))
            painter.drawPolygon(QPolygonF([QPointF(x - 5, 5), QPointF(x + 5, 5), QPointF(x, 13)]))
            painter.restore()
        painter.setPen(QColor("#61728a"))
        painter.drawText(QRectF(12, 40, self.width() / 2, 18), Qt.AlignmentFlag.AlignLeft, time_text(self.view_start))
        painter.drawText(QRectF(self.width() / 2, 40, self.width() / 2 - 12, 18), Qt.AlignmentFlag.AlignRight, time_text(self.view_end))

    def edge_at(self, x, y):
        if self.selection and 14 <= y <= 42:
            distances = [abs(x - self.x_at(value)) for value in self.selection]
            if min(distances) <= 9:
                return "left" if distances[0] <= distances[1] else "right"
        return None

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton or not self.duration:
            return
        self.setFocus()
        self.press_x = event.position().x()
        self.anchor = self.time_at(self.press_x)
        self.mode = self.edge_at(self.press_x, event.position().y()) if self.selectable else None
        if self.mode is None:
            self.mode = "seek" if not self.selectable or event.position().y() < 14 else "pending"
        if self.mode in ("left", "right"):
            self.interactionStarted.emit()
        event.accept()

    def mouseMoveEvent(self, event):
        x, y = event.position().x(), event.position().y()
        value = self.time_at(x)
        if self.mode == "pending" and abs(x - self.press_x) >= 4:
            self.mode = "create"
            self.interactionStarted.emit()
        if self.mode == "create":
            self.set_selection(self.anchor, value)
        elif self.mode == "left":
            end = self.selection[1]
            self.set_selection(value, end)
            if value > end:
                self.mode = "right"
        elif self.mode == "right":
            start = self.selection[0]
            self.set_selection(start, value)
            if value < start:
                self.mode = "left"
        elif self.mode == "seek":
            self.seekRequested.emit(value)
        else:
            self.setCursor(Qt.CursorShape.SizeHorCursor if self.edge_at(x, y) else Qt.CursorShape.PointingHandCursor)
        if self.mode in ("create", "left", "right"):
            self.seekRequested.emit(value)

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton or self.mode is None:
            return
        if self.mode in ("pending", "seek"):
            self.seekRequested.emit(self.time_at(event.position().x()))
        else:
            self.mouseMoveEvent(event)
        self.mode = None

    def mouseDoubleClickEvent(self, event):
        self.mode = None
        self.reset_zoom()

    def wheelEvent(self, event):
        if not self.duration:
            return
        centre = self.time_at(event.position().x())
        old_span = self.view_end - self.view_start
        factor = 0.7 if event.angleDelta().y() > 0 else 1 / 0.7
        span = min(self.duration, max(min(3000, self.duration), round(old_span * factor)))
        fraction = (centre - self.view_start) / max(1, old_span)
        self.view_start = max(0, min(self.duration - span, round(centre - fraction * span)))
        self.view_end = self.view_start + span
        self.update()
        event.accept()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Left, Qt.Key.Key_Right):
            step = 10000 if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else 1000
            value = self.position + (step if event.key() == Qt.Key.Key_Right else -step)
            self.seekRequested.emit(max(0, min(self.duration, value)))
        else:
            super().keyPressEvent(event)
