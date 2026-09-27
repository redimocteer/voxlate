"""Duration feedback for translated sentences."""
import math

from PySide6.QtCore import Qt, QRectF
from PySide6.QtGui import QColor, QLinearGradient, QBrush, QPalette
from PySide6.QtWidgets import QStyledItemDelegate, QStyleOptionViewItem, QStyle, QStyleFactory


TIMING_ROLE = int(Qt.ItemDataRole.UserRole) + 1
GRADIENT_START = 1.2


def acceleration(segment, text):
    """Only reuse timing measured for this text, including legacy projects."""
    if not segment.get("enabled", True):
        return None
    if segment.get("timing_text", segment.get("target_text", "")) != text.strip():
        return None
    duration = segment["end"] - segment["start"]
    generated = segment.get("generated_duration")
    if not isinstance(generated, (int, float)) or not math.isfinite(generated) or generated <= 0 or duration <= 0:
        return None
    return generated / duration


def gradient_fraction(ratio):
    if ratio is None or ratio <= GRADIENT_START:
        return 0.0
    return min(0.5, (ratio - GRADIENT_START) / (2 - GRADIENT_START) * 0.5)


class TranslationDelegate(QStyledItemDelegate):
    def __init__(self, parent=None):
        super().__init__(parent)
        # The application uses Fusion. Bypass its stylesheet wrapper, which
        # otherwise replaces per-cell background brushes with solid white.
        self.item_style = QStyleFactory.create('Fusion')
        self.item_style.setParent(self)

    def paint(self, painter, option, index):
        fraction = gradient_fraction(index.data(TIMING_ROLE))
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.widget = None
        style = self.item_style
        selected = bool(opt.state & QStyle.StateFlag.State_Selected)
        if fraction:
            base = opt.palette.highlight().color() if selected else QColor('white')
            painter.fillRect(opt.rect, base)
            rect = QRectF(opt.rect)
            rect.setLeft(rect.right()-rect.width()*fraction)
            gradient = QLinearGradient(rect.topLeft(), rect.topRight())
            gradient.setColorAt(0, base)
            gradient.setColorAt(1, QColor("#8db7ff"))
            painter.fillRect(rect, QBrush(gradient))
        else:
            background = QStyleOptionViewItem(opt)
            background.text = ''
            style.drawControl(QStyle.ControlElement.CE_ItemViewItem, background, painter)
        # Inset only the text; selection and timing backgrounds still fill the cell.
        opt.rect.adjust(8, 0, 0, 0)
        opt.backgroundBrush = QBrush(Qt.BrushStyle.NoBrush)
        opt.features &= ~QStyleOptionViewItem.ViewItemFeature.Alternate
        if selected:
            opt.palette.setColor(QPalette.ColorRole.Text, opt.palette.highlightedText().color())
        opt.state &= ~(QStyle.StateFlag.State_Selected | QStyle.StateFlag.State_MouseOver | QStyle.StateFlag.State_HasFocus)
        # Keep opt.text intact so Qt uses the same text layout with/without a gradient.
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter)
