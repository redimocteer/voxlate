"""Bounded waveform editor: drag ranges to create sentences."""
from array import array
import math
import sys
import wave

from PySide6.QtCore import Qt, Signal, QPointF, QRectF, QTimer, QUrl
from PySide6.QtGui import QColor, QPainter, QPen, QPolygonF, QShortcut, QKeySequence, QPixmap, QIcon
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
from PySide6.QtWidgets import (QDialog, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QHeaderView, QComboBox,
    QScrollBar, QMessageBox, QAbstractItemView)

from .common import VoxlateError, digest, read_json, write_json
from .media import check_cancelled
from .player import ClickSlider, playback_icon
from .paired_segments import PairedSegmentPlan, COLORS
from .segmentation import segmentation_needs_models


def editor_icon(kind):
    pixmap = QPixmap(24, 24)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(QColor('#526885'), 1.7, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    if kind == 'stop':
        painter.fillRect(6, 6, 12, 12, QColor('#385dce'))
    else:
        painter.drawLine(5, 7, 19, 7)
        painter.drawLine(9, 4, 15, 4)
        painter.drawLine(7, 8, 8, 20)
        painter.drawLine(17, 8, 16, 20)
        painter.drawLine(8, 20, 16, 20)
        painter.drawLine(10, 10, 10, 17)
        painter.drawLine(14, 10, 14, 17)
    painter.end()
    return QIcon(pixmap)


def time_text(value):
    millis = round(max(0, value)*1000)
    seconds, fraction = divmod(millis, 1000)
    return f'{seconds//3600:02d}:{seconds//60%60:02d}:{seconds%60:02d}.{fraction:03d}'


def waveform_peaks(path, limit=150000, start=0., end=None):
    """Stream PCM into bounded peak bins; never load a whole film into memory."""
    values = []
    with wave.open(str(path), 'rb') as source:
        if source.getsampwidth() != 2 or source.getcomptype() != 'NONE':
            raise VoxlateError('波形需要 PCM16 音轨，请重新提取。')
        total, rate, channels = source.getnframes(), source.getframerate(), source.getnchannels()
        first = max(0, min(total, round(start*rate)))
        last = total if end is None else max(first, min(total, round(end*rate)))
        source.setpos(first)
        width = max(1, math.ceil((last-first)/limit), round(rate*.01))
        while source.tell() < last:
            raw = source.readframes(min(width*256, last-source.tell()))
            if not raw:
                break
            check_cancelled()
            samples = array('h')
            samples.frombytes(raw)
            if sys.byteorder != 'little':
                samples.byteswap()
            for offset in range(0, len(samples), width*channels):
                block = samples[offset:offset+width*channels]
                values.append(max(abs(min(block)), abs(max(block)))/32768)
    return dict(peaks=values, step=width/rate, duration=total/rate, start=first/rate, end=last/rate)


def cached_waveform(path, project_directory, start=0., end=None):
    from pathlib import Path
    path, root = Path(path), Path(project_directory).resolve()
    stat = path.stat()
    key = digest('waveform-v2', str(path.resolve()), stat.st_size, stat.st_mtime_ns, start, end)
    cache = root/'.temp'/'waveforms'/f'{key}.json'
    if not cache.resolve().is_relative_to(root):
        raise VoxlateError('波形缓存目录指向项目外。')
    if cache.is_file():
        try:
            data = read_json(cache)
            if data['step'] > 0 and isinstance(data['peaks'], list) and data['duration'] > 0:
                return data
        except (OSError, ValueError, KeyError, TypeError):
            pass
    data = waveform_peaks(path, start=start, end=end)
    check_cancelled()
    write_json(cache, data)
    return data


class WaveformCanvas(QWidget):
    changed = Signal()
    selected = Signal(int)
    seek = Signal(float)
    preview = Signal(int)
    viewChanged = Signal()
    notice = Signal(str)

    def __init__(self, plan, waveform, parent=None):
        super().__init__(parent)
        self.plan, self.waveform = plan, waveform
        self.position = plan.cuts[0] if plan.cuts else plan.start
        self.current, self.cut = 0, None
        self.drag = self.before_drag = None
        self.range_anchor = self.range_end = self.press_x = None
        self.setMinimumHeight(235)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName('声音波形与成对切线')
        self.setToolTip('拖选空白新增一句；拖边界调整，双击句子试听，滚轮缩放。')
        self.fit()

    def fit(self):
        self.left, self.span = self.plan.start, self.plan.end-self.plan.start
        self.update()
        self.viewChanged.emit()

    def x(self, seconds):
        return 14+(seconds-self.left)/max(.001, self.span)*max(1, self.width()-28)

    def seconds(self, x):
        return max(self.plan.start, min(self.plan.end, self.left+(x-14)/max(1, self.width()-28)*self.span))

    def zoom(self, factor, anchor=None):
        anchor = self.left+self.span/2 if anchor is None else anchor
        fraction = (anchor-self.left)/self.span
        duration = self.plan.end-self.plan.start
        self.span = max(min(.5, duration), min(duration, self.span*factor))
        self.left = max(self.plan.start, min(self.plan.end-self.span, anchor-fraction*self.span))
        self.update()
        self.viewChanged.emit()

    def wheelEvent(self, event):
        self.zoom(.8 if event.angleDelta().y() > 0 else 1.25, self.seconds(event.position().x()))
        event.accept()

    def cut_x(self, index):
        # Touching sentences have two distinct handles at the shared endpoint.
        value = self.plan.cuts[index]
        offset = -3 if index % 2 and index+1 < len(self.plan.cuts) and self.plan.cuts[index+1] == value else 0
        if not index % 2 and index and self.plan.cuts[index-1] == value:
            offset = 3
        return self.x(value)+offset

    def hit_cut(self, x):
        if not self.plan.cuts:
            return None
        index = min(range(len(self.plan.cuts)), key=lambda i: abs(x-self.cut_x(i)))
        return index if abs(x-self.cut_x(index)) <= 7 else None

    def block_at(self, seconds):
        return next((i for i, (start, end) in enumerate(self.plan.pairs) if start <= seconds < end), None)

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return super().mousePressEvent(event)
        self.setFocus()
        seconds = self.seconds(event.position().x())
        self.cut = self.hit_cut(event.position().x())
        if self.cut is not None:
            self.drag = self.cut
            self.before_drag = self.plan.snapshot()
            self.current = self.cut//2
            self.selected.emit(self.current)
        else:
            self.range_anchor = self.range_end = seconds
            self.press_x = event.position().x()
            self.position = seconds
            index = self.block_at(seconds)
            self.current = index if index is not None else -1
            self.selected.emit(self.current)
            self.seek.emit(seconds)
        self.changed.emit()
        self.update()

    def mouseMoveEvent(self, event):
        if self.drag is not None:
            self.plan.move_cut(self.drag, self.seconds(event.position().x()))
            self.update()
        elif self.range_anchor is not None:
            self.range_end = self.seconds(event.position().x())
            self.update()
        else:
            self.setCursor(Qt.CursorShape.SizeHorCursor if self.hit_cut(event.position().x()) is not None else Qt.CursorShape.CrossCursor)

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return super().mouseReleaseEvent(event)
        if self.drag is None and self.range_anchor is None:
            return super().mouseReleaseEvent(event)
        if self.drag is not None:
            self.plan.remember(self.before_drag)
        elif self.range_anchor is not None and abs(event.position().x()-self.press_x) >= 5:
            try:
                self.current = self.plan.add_sentence(self.range_anchor, self.seconds(event.position().x()))
                self.selected.emit(self.current)
            except VoxlateError as exc:
                self.notice.emit(str(exc))
                self.range_anchor = self.range_end = self.press_x = None
                self.update()
                return
        self.drag = self.before_drag = None
        self.range_anchor = self.range_end = self.press_x = None
        self.changed.emit()
        self.update()

    def mouseDoubleClickEvent(self, event):
        self.range_anchor = self.range_end = self.press_x = None
        self.drag = self.before_drag = None
        if event.button() == Qt.MouseButton.LeftButton:
            if (index := self.block_at(self.seconds(event.position().x()))) is not None:
                self.current = index
                self.selected.emit(index)
                self.preview.emit(index)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor('#f5f7fb'))
        top, bottom = 26, self.height()-35
        for i, (start, end) in enumerate(self.plan.pairs):
            lo, hi = max(0., self.x(start)), min(float(self.width()), self.x(end))
            if hi <= lo:
                continue
            color = QColor(COLORS[i])
            color.setAlpha(88 if i == self.current else 26)
            painter.fillRect(QRectF(lo, top, hi-lo, bottom-top), color)
            painter.setPen(QColor(COLORS[i]))
            if hi-lo > 25:
                painter.drawText(QRectF(lo+4, top+3, hi-lo-8, 22), Qt.AlignmentFlag.AlignCenter, str(self.plan.number(i)))
        peaks, step = self.waveform['peaks'], self.waveform['step']
        offset = self.waveform.get('start', 0)
        gain = max(.06, max(peaks, default=.06))
        center, amplitude = (top+bottom)/2+8, (bottom-top)/2-28
        painter.setPen(QPen(QColor('#46617d'), 1))
        for x in range(14, self.width()-14):
            lo = max(0, int((self.seconds(x)-offset)/step))
            hi = min(len(peaks), max(lo+1, math.ceil((self.seconds(x+1)-offset)/step)))
            value = max(peaks[lo:hi], default=0)/gain*amplitude
            painter.drawLine(QPointF(x, center-value), QPointF(x, center+value))
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        tick = next((v for v in (.1, .2, .5, 1, 2, 5, 10, 20, 30, 60, 120, 300, 600) if v >= self.span/8), 600)
        value = math.ceil(self.left/tick)*tick
        painter.setPen(QColor('#61728a'))
        while value <= self.left+self.span:
            x = self.x(value)
            painter.drawLine(QPointF(x, bottom), QPointF(x, bottom+5))
            painter.drawText(QRectF(max(2., min(self.width()-92., x-43)), bottom+8, 90, 22),
                             Qt.AlignmentFlag.AlignCenter, time_text(value)[:-1])
            value += tick
        for i, value in enumerate(self.plan.cuts):
            x = self.cut_x(i)
            if not -8 <= x <= self.width()+8:
                continue
            odd = i == len(self.plan.cuts)-1 and len(self.plan.cuts) % 2
            color = QColor('#e23845' if odd else COLORS[i//2])
            painter.setPen(QPen(color, 3.5 if i//2 == self.current else 2.2,
                                Qt.PenStyle.DashLine if odd else Qt.PenStyle.SolidLine))
            painter.drawLine(QPointF(x, top-9), QPointF(x, bottom))
            painter.setBrush(color)
            painter.drawPolygon(QPolygonF([QPointF(x-5, top-16), QPointF(x+5, top-16), QPointF(x, top-8)]))
            if odd:
                painter.drawText(QRectF(max(0., min(self.width()-55., x-25)), top+2, 55, 22), Qt.AlignmentFlag.AlignCenter, '待配对')
        if self.range_anchor is not None and self.range_end is not None and abs(self.range_end-self.range_anchor) > .01:
            left, right = sorted((self.x(self.range_anchor), self.x(self.range_end)))
            painter.fillRect(QRectF(left, top, right-left, bottom-top), QColor(52, 102, 214, 65))
            painter.setPen(QPen(QColor('#3466d6'), 2, Qt.PenStyle.DashLine))
            painter.drawRect(QRectF(left, top, right-left, bottom-top))
        painter.setPen(QPen(QColor('#293649'), 1.5))
        painter.drawLine(QPointF(self.x(self.position), top), QPointF(self.x(self.position), bottom))


class SegmentationDialog(QDialog):
    volumeChanged = Signal(int)

    def __init__(self, project, first, last, original, vocals, waves, *, blocks=None, volume=80,
                 load_waves=None, parent=None):
        super().__init__(parent)
        self.plan = PairedSegmentPlan(project, first, last, blocks)
        self.wave_loader = load_waves
        self.worker = None
        self.setWindowTitle('手动分句' + (' · 已恢复草稿' if blocks else ''))
        self.resize(1120, 680)
        self.setMinimumSize(1040, 560)
        self.preview_range = None
        self.current = 0
        self.original, self.vocals, self.waves = original, vocals, waves
        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.audio.setVolume(volume/100)
        self.player.setAudioOutput(self.audio)
        self.player.errorOccurred.connect(self.playback_error)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        self.undo_button, self.redo_button = QPushButton('撤销 Z'), QPushButton('重做 R')
        self.undo_button.clicked.connect(lambda: self.history(False))
        self.redo_button.clicked.connect(lambda: self.history(True))
        self.canvas = WaveformCanvas(self.plan, waves[0])
        self.canvas.selected.connect(self.select_block)
        self.canvas.seek.connect(self.seek)
        self.canvas.preview.connect(self.preview_row)
        self.canvas.changed.connect(self.edited)
        self.canvas.notice.connect(self.message_notice)
        layout.addWidget(self.canvas, 1)
        self.scroll = QScrollBar(Qt.Orientation.Horizontal)
        self.scroll.valueChanged.connect(self.scroll_view)
        self.canvas.viewChanged.connect(self.update_scroll)
        layout.addWidget(self.scroll)
        controls = QHBoxLayout()
        self.play = QPushButton()
        self.play.setFixedWidth(36)
        self.play.setAccessibleName('播放或停止')
        self.play.clicked.connect(self.toggle_play)
        controls.addWidget(self.play)
        self.clock = QLabel(time_text(self.canvas.position))
        controls.addWidget(self.clock)
        self.source = QComboBox()
        self.source.addItems(['原声', '分离人声'])
        self.source.setToolTip('用于波形、试听和识别。')
        self.source.currentIndexChanged.connect(self.change_source)
        controls.addWidget(self.source)
        controls.addWidget(QLabel('音量'))
        self.volume = ClickSlider(Qt.Orientation.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setValue(volume)
        self.volume.setFixedWidth(75)
        self.volume.valueChanged.connect(lambda value: (self.audio.setVolume(value/100), self.volumeChanged.emit(value)))
        controls.addWidget(self.volume)
        controls.addStretch()
        controls.addWidget(self.undo_button)
        controls.addWidget(self.redo_button)
        self.left_button, self.right_button = QPushButton('向左扩展1句'), QPushButton('向右扩展1句')
        self.left_button.clicked.connect(lambda: self.extend('start'))
        self.right_button.clicked.connect(lambda: self.extend('end'))
        controls.addWidget(self.left_button)
        controls.addWidget(self.right_button)
        fit = QPushButton('适应范围')
        fit.clicked.connect(self.canvas.fit)
        controls.addWidget(fit)
        layout.addLayout(controls)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(['句号', '时间', '试听', '原文', ''])
        self.table.verticalHeader().hide()
        header = self.table.horizontalHeader()
        header.setSectionsClickable(False)
        for column in (0, 1, 2, 4):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setMinimumHeight(175)
        self.table.currentCellChanged.connect(lambda row, col, old, oldcol: self.select_block(row))
        self.table.cellDoubleClicked.connect(lambda row, col: self.preview_row(row))
        layout.addWidget(self.table, 1)
        footer = QHBoxLayout()
        self.message = QLabel()
        self.message.setWordWrap(True)
        footer.addWidget(self.message, 1)
        self.cancel_button = QPushButton('取消')
        self.cancel_button.clicked.connect(self.reject)
        footer.addWidget(self.cancel_button)
        self.apply_button = QPushButton('应用修改')
        self.apply_button.setObjectName('primary')
        self.apply_button.clicked.connect(self.apply)
        footer.addWidget(self.apply_button)
        layout.addLayout(footer)
        self.timer = QTimer(self)
        self.timer.setInterval(20)
        self.timer.timeout.connect(self.tick)
        self.timer.start()
        self.player.playbackStateChanged.connect(self.update_playback_controls)
        self.play.setIcon(playback_icon(False))
        self.pending_position = self.canvas.position
        self.pending_play = False
        self.player.mediaStatusChanged.connect(self.media_ready)
        self.player.setSource(QUrl.fromLocalFile(str(original)))
        self.update_scroll()
        self.render()
        for button in self.findChildren(QPushButton):
            button.setAutoDefault(False)
        for key, action in (
                ('D', self.delete_sentence), ('Delete', self.delete_sentence),
                ('Z', lambda: self.history(False)), ('R', lambda: self.history(True)),
                ('Ctrl+Z', lambda: self.history(False)), ('Ctrl+Shift+Z', lambda: self.history(True)),
                ('Ctrl+Y', lambda: self.history(True)), ('Space', self.toggle_play)):
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(action)
        self.canvas.setFocus()

    def export_blocks(self):
        return self.plan.blocks

    def message_notice(self, text):
        self.message.setText(text)

    def render(self):
        self.current = min(self.current, len(self.plan.pairs)-1)
        self.canvas.current = self.current
        self.table.blockSignals(True)
        self.table.setRowCount(len(self.plan.pairs))
        for index, (start, end) in enumerate(self.plan.pairs):
            text = next((row['source_text'] for row in self.plan.project['segments']
                         if abs(row['start']-start) < 1e-6 and abs(row['end']-end) < 1e-6), '')
            for col, value in ((0, str(self.plan.number(index))), (1, f'{time_text(start)} → {time_text(end)}'), (3, text)):
                item = QTableWidgetItem(value)
                if col != 3:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if col == 0:
                    item.setForeground(QColor(COLORS[index]))
                if col == 3:
                    item.setToolTip(text)
                    item.setForeground(QColor('#8994a5'))
                self.table.setItem(index, col, item)
            play = QPushButton()
            play.setIcon(playback_icon(False))
            play.setAccessibleName(f'试听第 {self.plan.number(index)} 句')
            play.clicked.connect(lambda checked=False, row=index: self.preview_row(row))
            delete = QPushButton()
            delete.setIcon(editor_icon('trash'))
            delete.setToolTip('删除此句（D / Delete），保留原声。')
            delete.setAccessibleName(f'删除第 {self.plan.number(index)} 句')
            delete.setEnabled(self.worker is None)
            delete.clicked.connect(lambda checked=False, row=index: self.delete_sentence(row))
            for col, button in ((2, play), (4, delete)):
                button.setAutoDefault(False)
                self.table.setCellWidget(index, col, button)
            self.table.setRowHeight(index, 35)
        self.table.blockSignals(False)
        self.select_block(self.current)
        busy = self.worker is not None
        self.canvas.setEnabled(not busy)
        self.source.setEnabled(not busy)
        self.undo_button.setEnabled(not busy and bool(self.plan.undo_stack))
        self.redo_button.setEnabled(not busy and bool(self.plan.redo_stack))
        self.left_button.setEnabled(not busy and self.plan.first > 0 and len(self.plan.cuts) <= 18)
        self.right_button.setEnabled(not busy and self.plan.last+1 < len(self.plan.project['segments']) and len(self.plan.cuts) <= 18)
        self.apply_button.setEnabled(not busy)
        self.apply_button.setText('应用并识别翻译' if segmentation_needs_models(self.plan.project, self.plan.blocks) else '应用修改')
        self.update_playback_controls()
        self.canvas.update()

    def select_block(self, index):
        if not 0 <= index < len(self.plan.pairs):
            self.current = self.canvas.current = -1
            self.table.blockSignals(True)
            self.table.setCurrentCell(-1, -1)
            self.table.clearSelection()
            self.table.blockSignals(False)
            self.canvas.update()
            return
        self.current = self.canvas.current = index
        self.table.blockSignals(True)
        self.table.setCurrentCell(index, 0)
        self.table.blockSignals(False)
        self.canvas.update()

    def edited(self):
        self.stop_playback()
        self.message.clear()
        self.render()

    def history(self, redo):
        if self.worker:
            return
        previous = (self.plan.start, self.plan.end)
        rollback = (self.plan.snapshot(), self.plan.undo_stack[:], self.plan.redo_stack[:])
        self.plan.redo() if redo else self.plan.undo()
        self.canvas.cut = None
        self.edited()
        if previous != (self.plan.start, self.plan.end):
            self.reload_waves(rollback)

    def delete_sentence(self, index=None):
        if self.worker:
            return
        index = self.current if index is None else index
        if not 0 <= index < len(self.plan.pairs):
            return
        self.plan.delete_sentence(index)
        self.canvas.cut = None
        self.current = min(index, len(self.plan.pairs)-1)
        self.edited()

    def extend(self, side):
        if self.worker:
            return
        try:
            rollback = (self.plan.snapshot(), self.plan.undo_stack[:], self.plan.redo_stack[:])
            if self.plan.extend_sentence(side):
                self.canvas.cut = None
                self.edited()
                self.reload_waves(rollback)
        except VoxlateError as exc:
            self.message.setText(str(exc))

    def run_job(self, action, complete, message, failure=None):
        from .gui import TaskThread
        self.message.setText(message)
        worker = self.worker = TaskThread(action)
        worker.result.connect(complete)
        worker.failure.connect(failure or self.message.setText)
        worker.status.connect(self.message.setText)
        def finished():
            self.worker = None
            worker.deleteLater()
            self.cancel_button.setText('取消')
            if getattr(self, 'closing', False):
                self.reject()
            else:
                self.render()
        worker.finished.connect(finished)
        self.render()
        worker.start()

    def reload_waves(self, rollback=None):
        self.canvas.position = max(self.plan.start, min(self.plan.end, self.canvas.position))
        self.canvas.fit()
        if self.wave_loader:
            start, end = self.plan.start, self.plan.end
            def complete(waves):
                self.waves = waves
                self.canvas.waveform = waves[self.source.currentIndex()]
                self.canvas.update()
                self.message.clear()
            def failed(text):
                if rollback:
                    self.plan.restore(rollback[0])
                    self.plan.undo_stack, self.plan.redo_stack = rollback[1:]
                    self.canvas.position = max(self.plan.start, min(self.plan.end, self.canvas.position))
                    self.canvas.fit()
                self.message.setText(text)
            self.run_job(lambda emit: self.wave_loader(start, end), complete, '正在读取波形…', failed)

    def update_scroll(self):
        self.scroll.blockSignals(True)
        self.scroll.setRange(round(self.plan.start*1000), max(round(self.plan.start*1000), round((self.plan.end-self.canvas.span)*1000)))
        self.scroll.setPageStep(max(1, round(self.canvas.span*1000)))
        self.scroll.setValue(round(self.canvas.left*1000))
        self.scroll.blockSignals(False)

    def scroll_view(self, value):
        self.canvas.left = value/1000
        self.canvas.update()

    def media_ready(self, status):
        if status in (QMediaPlayer.MediaStatus.LoadedMedia, QMediaPlayer.MediaStatus.BufferedMedia) and self.pending_position is not None:
            self.player.setPosition(round(self.pending_position*1000))
            self.pending_position = None
            if self.pending_play:
                self.pending_play = False
                self.player.play()
        elif status == QMediaPlayer.MediaStatus.EndOfMedia:
            self.stop_playback()

    def playback_error(self, error, text):
        self.stop_playback()
        self.message.setText('试听失败：'+text)

    def update_playback_controls(self, *_):
        active = self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState or getattr(self, 'pending_play', False)
        self.play.setIcon(editor_icon('stop') if active else playback_icon(False))
        for index, pair in enumerate(self.plan.pairs):
            button = self.table.cellWidget(index, 2)
            if button:
                playing = active and self.preview_range == pair
                button.setIcon(editor_icon('stop') if playing else playback_icon(False))
                button.setAccessibleName('停止试听' if playing else f'试听第 {self.plan.number(index)} 句')

    def stop_playback(self):
        self.pending_play = False
        self.preview_range = None
        self.player.pause()
        self.update_playback_controls()

    def seek(self, seconds):
        self.stop_playback()
        self.canvas.position = seconds
        self.player.setPosition(round(seconds*1000))
        if self.pending_position is not None:
            self.pending_position = seconds
        self.clock.setText(time_text(seconds))

    def toggle_play(self):
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState or self.pending_play:
            self.stop_playback()
        else:
            self.preview_range = None
            if self.canvas.position >= self.plan.end-.02:
                self.seek(self.plan.start)
            self.pending_play = self.pending_position is not None
            self.player.play()
            self.update_playback_controls()

    def preview_row(self, index):
        if not 0 <= index < len(self.plan.pairs):
            return
        pair = self.plan.pairs[index]
        if self.preview_range == pair and (self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState or self.pending_play):
            self.stop_playback()
            return
        self.select_block(index)
        self.preview_block()

    def preview_block(self):
        if not 0 <= self.current < len(self.plan.pairs):
            return
        start, end = self.plan.pairs[self.current]
        self.seek(start)
        self.preview_range = (start, end)
        self.pending_play = self.pending_position is not None
        self.player.play()
        self.update_playback_controls()

    def change_source(self, index):
        self.stop_playback()
        self.pending_position = self.canvas.position
        self.player.stop()
        self.player.setSource(QUrl.fromLocalFile(str(self.original if index == 0 else self.vocals)))
        self.canvas.waveform = self.waves[index]
        self.render()

    def tick(self):
        if self.player.playbackState() != QMediaPlayer.PlaybackState.PlayingState:
            return
        seconds = self.player.position()/1000
        end = self.preview_range[1] if self.preview_range else self.plan.end
        if seconds >= end:
            self.stop_playback()
            seconds = end
            self.player.setPosition(round(end*1000))
        self.canvas.position = seconds
        self.clock.setText(time_text(seconds))
        if seconds > self.canvas.left+self.canvas.span or seconds < self.canvas.left:
            self.canvas.left = max(self.plan.start, min(self.plan.end-self.canvas.span, seconds-self.canvas.span*.1))
            self.update_scroll()
        self.canvas.update()

    def keyPressEvent(self, event):
        key, mods = event.key(), event.modifiers()
        if key in (Qt.Key.Key_D, Qt.Key.Key_Delete) and not mods:
            self.delete_sentence()
        elif key == Qt.Key.Key_Z:
            self.history(bool(mods & Qt.KeyboardModifier.ShiftModifier))
        elif key == Qt.Key.Key_R and not mods or key == Qt.Key.Key_Y and mods & Qt.KeyboardModifier.ControlModifier:
            self.history(True)
        elif key == Qt.Key.Key_Space:
            self.toggle_play()
        else:
            return super().keyPressEvent(event)
        event.accept()

    def apply(self):
        if self.worker:
            return
        try:
            self.plan.validate()
        except VoxlateError as exc:
            QMessageBox.warning(self, '请调整分句', str(exc))
            return
        self.accept()

    def done(self, result):
        if self.worker is not None:
            self.closing = True
            self.worker.cancel.set()
            self.cancel_button.setText('正在取消…')
            return
        self.timer.stop()
        self.player.stop()
        self.player.setSource(QUrl())
        super().done(result)


