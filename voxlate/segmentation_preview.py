"""Optional silent video preview, following the segmentation audio clock."""
from pathlib import Path

from PySide6.QtCore import QUrl, Qt
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel


class SegmentationPreview(QWidget):
    def __init__(self, path, parent=None):
        super().__init__(parent)
        self.path = Path(path) if path else None
        self.available = bool(self.path and self.path.is_file())
        self.active = False
        self.position = 0
        self.playing = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.video = QVideoWidget()
        self.video.setMinimumSize(260, 146)
        self.video.setAspectRatioMode(Qt.AspectRatioMode.KeepAspectRatio)
        layout.addWidget(self.video, 1)
        self.notice = QLabel('视频预览不可用，仍可编辑和试听音轨。')
        self.notice.setWordWrap(True)
        self.notice.hide()
        layout.addWidget(self.notice)
        self.player = QMediaPlayer(self)
        self.player.setVideoOutput(self.video)
        # No audio output: only the selected project WAV is heard, including
        # when the source video has multiple audio tracks.
        self.player.tracksChanged.connect(self.disable_other_tracks)
        self.player.mediaStatusChanged.connect(self.media_ready)
        self.player.errorOccurred.connect(self.failed)

    def disable_other_tracks(self):
        self.player.setActiveAudioTrack(-1)
        self.player.setActiveSubtitleTrack(-1)

    def enable(self, enabled):
        self.active = bool(enabled and self.available)
        self.setVisible(self.active)
        if self.active:
            self.notice.hide()
            self.player.setSource(QUrl.fromLocalFile(str(self.path.resolve())))
        else:
            self.player.stop()
            self.player.setSource(QUrl())

    def follow(self, seconds, playing, *, force=False):
        self.position, self.playing = round(seconds*1000), playing
        if not self.active or self.player.mediaStatus() in (
                QMediaPlayer.MediaStatus.NoMedia, QMediaPlayer.MediaStatus.LoadingMedia,
                QMediaPlayer.MediaStatus.InvalidMedia):
            return
        if force or abs(self.player.position()-self.position) > 180:
            self.player.setPosition(self.position)
        if playing:
            if self.player.playbackState() != QMediaPlayer.PlaybackState.PlayingState:
                self.player.play()
        else:
            self.player.pause()

    def media_ready(self, status):
        if status in (QMediaPlayer.MediaStatus.LoadedMedia, QMediaPlayer.MediaStatus.BufferedMedia):
            self.follow(self.position/1000, self.playing, force=True)

    def failed(self, *_):
        self.active = False
        self.player.stop()
        self.player.setSource(QUrl())
        self.notice.show()

    def shutdown(self):
        self.active = False
        self.player.stop()
        self.player.setSource(QUrl())
