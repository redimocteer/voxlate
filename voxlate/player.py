"""Local, streaming video playback and a single speaker reference range."""
from pathlib import Path
import os

os.environ.setdefault("QT_MEDIA_BACKEND", "ffmpeg")

from PySide6.QtCore import Qt, QUrl, QSize, QPointF, QTimer, Signal
from PySide6.QtGui import QIcon, QPixmap, QPainter, QColor, QPolygonF
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
                               QSlider, QMessageBox, QCheckBox, QStyle, QStyleOptionSlider)
from .timeline import RangeTimeline


def playback_icon(playing):
    pixmap = QPixmap(48, 48)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#315de2"))
    if playing:
        painter.drawRoundedRect(12, 10, 8, 28, 2, 2)
        painter.drawRoundedRect(28, 10, 8, 28, 2, 2)
    else:
        painter.drawPolygon(QPolygonF([QPointF(15, 9), QPointF(38, 24), QPointF(15, 39)]))
    painter.end()
    return QIcon(pixmap)


def timestamp(milliseconds):
    seconds = max(0, int(milliseconds) // 1000)
    return f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


class ClickSlider(QSlider):
    """Place the handle at the pointer on press, and retain ordinary dragging."""
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.set_at(event.position().x())
            self.setSliderDown(True)
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.isSliderDown():
            self.set_at(event.position().x())
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.isSliderDown():
            self.set_at(event.position().x())
            self.setSliderDown(False)
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    def set_at(self, x):
        option = QStyleOptionSlider()
        self.initStyleOption(option)
        groove = self.style().subControlRect(QStyle.ComplexControl.CC_Slider, option,
                                             QStyle.SubControl.SC_SliderGroove, self)
        handle = self.style().subControlRect(QStyle.ComplexControl.CC_Slider, option,
                                             QStyle.SubControl.SC_SliderHandle, self)
        span = max(1, groove.width() - handle.width())
        position = round(x - groove.left() - handle.width() / 2)
        self.setValue(QStyle.sliderValueFromPosition(self.minimum(), self.maximum(),
                                                     max(0, min(span, position)), span, option.upsideDown))


class VideoPlayer(QDialog):
    volumeChanged = Signal(int)

    def __init__(self, path, parent=None, reference_range=None, select_reference=True, original_audio=None,
                 reference_guard=None, volume=80):
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.Window)
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.reference_guard = reference_guard
        self.setWindowTitle(Path(path).name)
        self.resize(920, 650)
        self.selected_range = None
        self.preview_end = None
        self.original_player = None
        self.original_audio = None
        self.original_check = None
        self.original_track = None
        layout = QVBoxLayout(self)
        self.video = QVideoWidget()
        layout.addWidget(self.video, 1)
        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        initial_volume = volume if type(volume) is int and 0 <= volume <= 100 else 80
        self.audio.setVolume(initial_volume / 100)
        self.player.setAudioOutput(self.audio)
        self.player.setVideoOutput(self.video)
        self.seek = RangeTimeline(selectable=select_reference)
        self.seek.seekRequested.connect(self.seek_to)
        self.seek.interactionStarted.connect(self.begin_selection)
        layout.addWidget(self.seek)
        controls = self.controls = QHBoxLayout()
        self.play = QPushButton()
        self.play.setFixedSize(36, 34)
        self.play.setStyleSheet("padding: 0px;")
        self.play.setIconSize(QSize(22, 22))
        self.update_play_icon(QMediaPlayer.PlaybackState.StoppedState)
        self.play.clicked.connect(self.toggle)
        controls.addWidget(self.play)
        self.time = QLabel("00:00:00 / 00:00:00")
        controls.addWidget(self.time)
        controls.addStretch()
        self.range_label = QLabel()
        if select_reference:
            self.preview_button = QPushButton("试听")
            self.preview_button.clicked.connect(self.preview)
            controls.addWidget(self.preview_button)
            controls.addWidget(self.range_label)
            self.apply_button = QPushButton("使用")
            self.apply_button.clicked.connect(self.apply_range)
            controls.addWidget(self.apply_button)
            self.seek.selectionChanged.connect(self.selection_changed)
            self.selection_changed(None)
        if not select_reference:
            self.original_check = QCheckBox("原声")
            controls.addWidget(self.original_check)
            available = original_audio is not None and Path(original_audio).is_file()
            self.original_check.setEnabled(available)
            self.original_check.setToolTip("对比原视频的完整音轨" if available else "项目原音轨不存在，请重新生成配音。")
            self.original_check.toggled.connect(self.switch_original)
            if available:
                self.original_player = QMediaPlayer(self)
                self.original_audio = QAudioOutput(self)
                self.original_audio.setVolume(initial_volume / 100)
                self.original_audio.setMuted(True)
                self.original_player.setAudioOutput(self.original_audio)
                self.original_player.errorOccurred.connect(self.original_error)
                self.original_player.setSource(QUrl.fromLocalFile(str(Path(original_audio).resolve())))
                self.original_player.mediaStatusChanged.connect(lambda _: self.sync_original())
        self.sync_timer = QTimer(self)
        self.sync_timer.setInterval(500)
        self.sync_timer.timeout.connect(self.sync_original)
        self.sync_timer.start()
        controls.addWidget(QLabel("音量"))
        volume = self.volume = ClickSlider(Qt.Orientation.Horizontal)
        volume.setRange(0, 100)
        volume.setValue(initial_volume)
        volume.setMaximumWidth(100)
        volume.valueChanged.connect(self.set_volume)
        controls.addWidget(volume)
        layout.addLayout(controls)
        if reference_range:
            self.seek.set_selection(*(round(value * 1000) for value in reference_range))
        self.error = QLabel()
        self.error.setWordWrap(True)
        self.error.hide()
        layout.addWidget(self.error)
        self.player.positionChanged.connect(self.position_changed)
        self.player.durationChanged.connect(self.seek.set_duration)
        self.player.playbackStateChanged.connect(self.update_play_icon)
        self.player.playbackStateChanged.connect(lambda _: self.sync_original(force=True))
        self.player.playbackRateChanged.connect(lambda _: self.sync_original(force=True))
        self.player.tracksChanged.connect(self.configure_comparison)
        self.player.errorOccurred.connect(self.show_error)
        self.player.setSource(QUrl.fromLocalFile(str(Path(path).resolve())))

    def set_volume(self, value):
        self.audio.setVolume(value / 100)
        if self.original_audio:
            self.original_audio.setVolume(value / 100)
        self.volumeChanged.emit(value)

    def switch_original(self, checked):
        if self.original_track is not None:
            self.audio.setMuted(False)
            self.player.setActiveAudioTrack(self.original_track if checked else 0)
            return
        if not self.original_player:
            return
        self.audio.setMuted(checked)
        # Seek while muted, so toggling never briefly plays the old position.
        self.original_audio.setMuted(True)
        self.sync_original(force=True)
        self.original_audio.setMuted(not checked)

    def sync_original(self, force=False):
        if not self.original_player or self.original_track is not None:
            return
        if not self.original_check.isChecked():
            self.original_player.pause()
            return
        if self.original_player.mediaStatus() in (QMediaPlayer.MediaStatus.NoMedia, QMediaPlayer.MediaStatus.LoadingMedia,
                                                  QMediaPlayer.MediaStatus.InvalidMedia):
            return
        position = self.player.position()
        self.original_player.setPlaybackRate(self.player.playbackRate())
        playing = self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
        # Video is the master clock; correct decoder drift, including after seeks.
        if force or abs(self.original_player.position() - position) > 120:
            self.original_player.setPosition(position)
        if playing:
            if self.original_player.playbackState() != QMediaPlayer.PlaybackState.PlayingState:
                self.original_player.play()
        else:
            self.original_player.pause()

    def configure_comparison(self):
        if self.original_check is None:
            return
        # App exports use dubbed audio first and the original audio second.
        if len(self.player.audioTracks()) >= 2:
            self.original_track = 1
            self.original_check.setEnabled(True)
            self.original_check.setToolTip("切换原声与中文配音")
            if self.original_player:
                self.original_audio.setMuted(True)
                self.original_player.stop()
                self.original_player.setSource(QUrl())
            self.sync_timer.stop()
            self.switch_original(self.original_check.isChecked())

    def original_error(self, error, message):
        if self.original_track is not None:
            return
        self.original_check.setChecked(False)
        self.original_check.setEnabled(False)
        self.original_check.setToolTip("原音轨无法播放：" + message)
        self.error.setText("原音轨无法播放：" + message)
        self.error.show()

    def update_play_icon(self, state):
        playing = state == QMediaPlayer.PlaybackState.PlayingState
        self.play.setIcon(playback_icon(playing))
        label = "暂停" if playing else "播放"
        self.play.setToolTip(label)
        self.play.setAccessibleName(label)

    def show_error(self, error, message):
        self.error.setText("无法播放此视频：" + message)
        self.error.show()

    def toggle(self):
        self.preview_end = None
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def position_changed(self, position):
        self.seek.set_position(position)
        self.time.setText(timestamp(position) + " / " + timestamp(self.player.duration()))
        if self.preview_end is not None and position >= self.preview_end:
            self.player.pause()
            self.preview_end = None

    def valid_range(self):
        start, end = [value / 1000 for value in self.seek.selection] if self.seek.selection else (0, 0)
        if not 3 <= end - start <= 15 or end * 1000 > self.player.duration():
            QMessageBox.information(self, "调整音色选段", "请选择视频内连续的 3–15 秒，终点须晚于起点。")
            return None
        return [start, end]

    def begin_selection(self):
        self.preview_end = None
        self.player.pause()

    def seek_to(self, position):
        self.preview_end = None
        self.player.setPosition(position)
        self.sync_original(force=True)

    def selection_changed(self, selected):
        valid = selected is not None and 3000 <= selected[1] - selected[0] <= 15000
        if selected:
            start, end = selected
            self.range_label.setText(f"{timestamp(round(start / 1000) * 1000)} – {timestamp(round(end / 1000) * 1000)}")
        else:
            self.range_label.setText("未选范围")
        self.range_label.setToolTip("选择同一人连续说话的3–15秒；拖动时间轴选段，拖两端调整。")
        self.preview_button.setEnabled(valid)
        self.apply_button.setEnabled(valid)

    def preview(self):
        selected = self.valid_range()
        if selected:
            self.preview_end = selected[1] * 1000
            self.player.setPosition(int(selected[0] * 1000))
            self.player.play()

    def apply_range(self):
        blocked = self.reference_guard() if self.reference_guard else None
        if blocked:
            QMessageBox.information(self, "暂时无法使用音色", blocked)
            return
        selected = self.valid_range()
        if selected:
            self.selected_range = selected
            self.accept()

    def done(self, result):
        self.release_media()
        super().done(result)

    def closeEvent(self, event):
        self.release_media()
        super().closeEvent(event)

    def release_media(self):
        self.sync_timer.stop()
        if self.original_check:
            self.original_check.setChecked(False)
        if self.original_player:
            self.original_player.stop()
            self.original_player.setSource(QUrl())
        self.player.stop()
        self.player.setSource(QUrl())
