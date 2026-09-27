"""Explicit selection of third-party downloads, before any installation work."""
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, QCheckBox,
                               QScrollArea, QWidget, QDialogButtonBox)
from .resource_terms import RESOURCE_LINKS


class ResourceTermsDialog(QDialog):
    def __init__(self, stages, parent=None):
        super().__init__(parent)
        self.setWindowTitle('选择第三方资源')
        self.resize(740, 540)
        layout = QVBoxLayout(self)
        intro = QLabel('所选资源将从上游或您启用的镜像下载，不包含在 Voxlate 安装包中。\n'
                       '请查看来源与许可；不接受的项目可以取消勾选，之后也可自行准备并在设置中指定。')
        intro.setWordWrap(True)
        layout.addWidget(intro)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        rows = QVBoxLayout(body)
        self.choices = {}
        for stage in stages:
            name, source, terms = RESOURCE_LINKS[stage]
            row = QHBoxLayout()
            choice = QCheckBox(name)
            choice.setChecked(True)
            choice.toggled.connect(self.refresh)
            self.choices[stage] = choice
            row.addWidget(choice, 1)
            links = QLabel(f'<a href="{source}">来源</a> · <a href="{terms}">许可</a>')
            links.setOpenExternalLinks(True)
            row.addWidget(links)
            rows.addLayout(row)
        details = QLabel('IndexTTS 专用协议涉及输出、下游分发及规模门槛。辅助模型另有条款：'
            '<a href="https://huggingface.co/facebook/w2v-bert-2.0">w2v-BERT</a>、'
            '<a href="https://huggingface.co/nvidia/bigvgan_v2_22khz_80band_256x">BigVGAN</a>、'
            '<a href="https://huggingface.co/funasr/campplus">CAMPPlus</a>。'
            'CAMPPlus 模型卡与 <a href="https://github.com/modelscope/FunASR/blob/main/MODEL_LICENSE">FunASR 模型协议</a>'
            '的声明不同，请一并核对；不能将整套资源视为 MIT 或无限制商用。')
        details.setWordWrap(True)
        details.setOpenExternalLinks(True)
        if any(s in stages for s in ('tts', 'tts_model')):
            rows.addWidget(details)
        rows.addStretch()
        scroll.setWidget(body)
        layout.addWidget(scroll, 1)
        self.accept_terms = QCheckBox('我已阅读所选资源条款，并决定下载；素材与声音应有授权或其他合法依据。')
        self.accept_terms.toggled.connect(self.refresh)
        layout.addWidget(self.accept_terms)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.download = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.download.setText('下载所选资源')
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('取消')
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.refresh()

    def selected_stages(self):
        return [key for key, choice in self.choices.items() if choice.isChecked()]

    def refresh(self):
        if hasattr(self, 'download'):
            self.download.setEnabled(self.accept_terms.isChecked() and bool(self.selected_stages()))
