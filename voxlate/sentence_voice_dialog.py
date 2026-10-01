"""Pick an original sentence as the voice reference for one dubbed sentence."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QLabel, QComboBox, QDialogButtonBox
from .ui_controls import action_button, action_icon


class SentenceVoiceDialog(QDialog):
    def __init__(self, project, row, play_reference, parent=None):
        super().__init__(parent)
        segment = project['segments'][row]
        self.setWindowTitle(f"第 {segment['id']} 句 · 音色参考")
        self.setMinimumWidth(460)
        self.resize(540, 220)
        layout = QVBoxLayout(self)
        hint = QLabel('仅改变本句的参考音色；选择会保存，可恢复为跟随当前音色模式。')
        hint.setWordWrap(True)
        layout.addWidget(hint)
        line = QHBoxLayout()
        self.reference = QComboBox()
        self.reference.setMaxVisibleItems(8)
        self.reference.setStyleSheet('QComboBox {combobox-popup: 0;}')
        self.reference.view().setMaximumHeight(260)
        self.reference.view().setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.reference.addItem('跟随当前音色模式', None)
        self.segments = {s['id']: s for s in project['segments']}
        for candidate in project['segments']:
            text = candidate.get('source_text', '').replace('\n', ' ')
            self.reference.addItem(f"第 {candidate['id']} 句 · {candidate['end']-candidate['start']:.1f} 秒 · {text[:50]}", candidate['id'])
        self.reference.setCurrentIndex(max(0, self.reference.findData(segment.get('voice_reference_sentence_id'))))
        line.addWidget(self.reference, 1)
        self.preview = action_button(action_icon('play'), '试听参考原声',
            lambda: play_reference(self.reference.currentData()))
        line.addWidget(self.preview)
        layout.addLayout(line)
        self.text = QLabel()
        self.text.setWordWrap(True)
        self.text.setTextFormat(Qt.TextFormat.PlainText)
        self.text.setMaximumHeight(80)
        layout.addWidget(self.text)
        self.reference.currentIndexChanged.connect(self.refresh)
        self.refresh()
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText('配音本句')
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('取消')
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def refresh(self):
        selected = self.segments.get(self.reference.currentData())
        self.preview.setEnabled(selected is not None)
        self.text.setText(selected.get('source_text', '') if selected else '统一音色、逐句音色或分角色音色，由主界面的选择决定。')
