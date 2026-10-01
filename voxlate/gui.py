from __future__ import annotations

import argparse
import copy
import logging
from pathlib import Path
import threading
import sys
import os
import uuid
import time

from PySide6.QtCore import Qt, QThread, Signal, QTimer, QUrl, QSize, QPointF
from PySide6.QtGui import QDesktopServices, QColor, QFontDatabase, QIcon, QPixmap, QPainter, QPolygonF, QValidator
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTabWidget, QLineEdit, QFileDialog, QPlainTextEdit, QTableWidget, QTableWidgetItem,
    QHeaderView, QMessageBox, QFormLayout, QComboBox, QCheckBox,
    QProgressBar, QTreeWidget, QTreeWidgetItem, QDialog, QDialogButtonBox, QRadioButton, QButtonGroup, QSpinBox,
)

from .app_settings import default_data_dir, prepare_settings
from .common import VoxlateError, digest, load_config, read_json, write_json
from .diagnostics import ResourceStatus, check_resources, resource_required
from .installer import Installer, planned_stages, relocate, resource_root
from .media import set_cancel_event
from .pipeline import VideoDubPipeline, project_lock
from .resources import CATALOG, EXTRAS, PURPOSES, configured_paths, extra_resource_paths, tracked_resource_paths, remember_root, format_bytes, cleanup_inventory, clean_resources
from .translation_models import MODELS, selected_key, select_model
from .recognition_models import MODELS as ASR_MODELS, selected_key as selected_asr_key, select_model as select_asr_model, required_model
from .resource_cache import load_resource_cache, save_resource_cache
from .project_storage import default_project_directory, existing_project_directory, validate_project_directory, project_root, clear_video_project, relocate_saved_project
from .translation_view import TranslationDelegate, TIMING_ROLE, acceleration, GRADIENT_START
from .dubbing_state import audio_exists, sentence_ready, dubbing_ready, voice_key, voice_contexts, voice_selection, select_voice_version, automatic_reference
from .tts_session import TTSSession
from .roles import ensure_roles, validate_roles, apply_automatic_voice_mode
from .role_dialog import RoleDialog
from .ui_controls import CenteredComboBox, action_icon, action_button, action_cell
from .sentence_table import SentenceTable
from .audio_tracks import read_audio_tracks, selected_track, track_suffix


class ReferenceSentenceBox(QSpinBox):
    """Editable sentence number with explicit upper minus / lower plus."""
    def __init__(self):
        super().__init__()
        self.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lineEdit().setPlaceholderText('自动推荐')
        self.setStyleSheet('QSpinBox { padding-right: 23px; }')
        self.minus = QPushButton('−', self)
        self.plus = QPushButton('+', self)
        for button in (self.minus, self.plus):
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.setStyleSheet('QPushButton {padding:0; border-radius:0; font-size:13px;}')
        self.minus.clicked.connect(lambda: self.setValue(self.value() - 1))
        self.plus.clicked.connect(lambda: self.setValue(self.value() + 1))
        self.valueChanged.connect(self.refresh_steps)
        self.setFixedSize(116, 32)

    def textFromValue(self, value):
        return '' if value == 0 else super().textFromValue(value)

    def valueFromText(self, text):
        return 0 if not text.strip() else super().valueFromText(text)

    def validate(self, text, pos):
        if not text.strip():
            return QValidator.State.Acceptable, text, pos
        return super().validate(text, pos)

    def refresh_steps(self):
        self.minus.setEnabled(self.value() > self.minimum())
        self.plus.setEnabled(self.value() < self.maximum())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        height = self.height() // 2
        self.minus.setGeometry(self.width()-23, 0, 23, height)
        self.plus.setGeometry(self.width()-23, height, 23, self.height()-height)
        self.refresh_steps()


STYLE = """
QWidget { font-family: 'Microsoft YaHei UI', 'Segoe UI'; font-size: 13px; color: #23324a; }
QMainWindow { background: #f3f6fa; }
QLabel#brand { font-size: 30px; font-weight: 700; color: #12203b; }
QLabel#muted { color: #61728a; }
QLabel#notice { background: #e8efff; border-radius: 8px; padding: 12px; color: #274d95; }
QTabWidget::pane { border: 1px solid #dce3ed; background: white; border-radius: 8px; }
QTabBar::tab { padding: 12px 22px; background: #eaf0f7; margin-right: 4px; }
QTabBar::tab:selected { background: white; color: #285bd4; font-weight: 600; }
QPushButton { background: white; border: 1px solid #c9d4e4; border-radius: 6px; padding: 8px 13px; }
QPushButton:hover { background: #edf3ff; border-color: #8ba8e9; }
QPushButton:disabled { color: #9ba6b6; background: #f1f3f6; border-color: #e1e5ec; }
QRadioButton:disabled, QCheckBox:disabled { color: #9ba6b6; }
QPushButton#primary { background: #315de2; color: white; border-color: #315de2; font-weight: 600; }
QPushButton#primary:disabled { background: #a4b4e1; border-color: #a4b4e1; }
QLineEdit, QPlainTextEdit, QComboBox { background: white; border: 1px solid #cfd8e5; border-radius: 5px; padding: 7px; }
QTableWidget, QTreeWidget { background: white; border: 1px solid #dce3ed; gridline-color: #e9eef4; selection-background-color: #dce7ff; selection-color: #23324a; }
QTreeWidget::item { padding: 8px 2px; }
QHeaderView::section { background: #edf2f8; padding: 9px; border: none; font-weight: 600; }
QGroupBox { font-weight: 600; border: 1px solid #dce3ed; border-radius: 6px; margin-top: 14px; padding: 14px; }
QGroupBox::title { subcontrol-origin: margin; left: 12px; }
QProgressBar { border: none; background: #e7edf6; border-radius: 4px; min-height: 7px; max-height: 7px; }
QProgressBar::chunk { background: #315de2; border-radius: 4px; }
"""


def sentence_play_icon(stopped=False):
    pixmap = QPixmap(32, 32)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor('#3269df'))
    if stopped:
        painter.drawRect(8, 8, 16, 16)
    else:
        painter.drawPolygon(QPolygonF([QPointF(10, 6), QPointF(26, 16), QPointF(10, 26)]))
    painter.end()
    return QIcon(pixmap)


class ReadableLog(QPlainTextEdit):
    def appendPlainText(self, text):
        bar = self.verticalScrollBar()
        previous = bar.value()
        following = previous >= bar.maximum() - 1
        super().appendPlainText(text)
        if not following:
            bar.setValue(previous)


class TaskThread(QThread):
    result = Signal(object)
    failure = Signal(str)
    status = Signal(str)
    progress = Signal(object)

    def __init__(self, action, with_progress=False):
        super().__init__()
        self.action = action
        self.with_progress = with_progress
        self.cancel = threading.Event()

    def run(self):
        handler = SignalLog(self.status, self.progress)
        logger = logging.getLogger("voxlate")
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        set_cancel_event(self.cancel)
        try:
            self.result.emit(self.action(self.status.emit, self.progress.emit) if self.with_progress else self.action(self.status.emit))
        except Exception as exc:
            self.failure.emit(str(exc))
        finally:
            logger.removeHandler(handler)
            set_cancel_event(None)


class SignalLog(logging.Handler):
    def __init__(self, signal, progress=None):
        super().__init__()
        self.signal = signal
        self.progress = progress

    def emit(self, record):
        if not getattr(record, "progress_only", False):
            self.signal.emit(record.getMessage())
        if self.progress is not None and hasattr(record, "voxlate_progress"):
            self.progress.emit(record.voxlate_progress)


class ResourceSettingsDialog(QDialog):
    def __init__(self, key, cfg, defaults, resource, parent=None):
        super().__init__(parent)
        self.setWindowTitle(CATALOG[key].title + "设置")
        self.setMinimumWidth(460)
        self.cfg, self.defaults, self.resource = copy.deepcopy(cfg), defaults, resource
        self.fields = {}
        layout = QVBoxLayout(self)
        form = QFormLayout()
        sections = ("asr", "translator") if key == "runtime" else (('asr',) if key == 'qwen' else (key,))
        for section in sections:
            device = QComboBox()
            device.addItems(["cpu", "cuda:0"] if section == "tts" else ["cpu", "cuda"])
            current = cfg[section]["device"]
            if device.findText(current) < 0:
                device.addItem(current)
            device.setCurrentText(current)
            if section == "translator":
                device.clear()
                device.addItem("GPU（显卡）", "cuda")
                device.addItem("CPU（处理器）", "cpu")
                device.setCurrentIndex(1 if current == "cpu" else 0)
                device.setToolTip("GPU 使用 Vulkan 加速；CPU 更慢，但不占显存。")
            self.fields[section + ".device"] = device
            form.addRow({"asr": "识别设备", "translator": "翻译设备"}.get(section, "计算设备"), device)
            if section == "asr" and selected_asr_key(cfg) != 'asr_qwen_model' and key != 'qwen':
                precision = QComboBox()
                precision.addItems(["int8", "int8_float16", "float16", "float32"])
                precision.setCurrentText(cfg[section]["compute_type"])
                self.fields[section + ".compute_type"] = precision
                form.addRow("识别精度" if section == "asr" else "翻译精度", precision)
        if key == "tts":
            for name, label in (("use_bf16", "BF16（减少显存占用，需显卡支持）"),):
                check = QCheckBox(label)
                if name == "use_bf16":
                    check.setToolTip("不支持时取消勾选；不勾选仍可使用显卡。")
                check.setChecked(cfg[key][name])
                self.fields[key + "." + name] = check
                form.addRow(check)
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        self.default_button = buttons.addButton("恢复默认", QDialogButtonBox.ButtonRole.ResetRole)
        self.default_button.clicked.connect(self.restore_defaults)
        self.help_button = buttons.addButton("帮助", QDialogButtonBox.ButtonRole.HelpRole)
        self.help_button.clicked.connect(self.show_help)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def restore_defaults(self):
        for key, widget in self.fields.items():
            section, name = key.split(".")
            value = self.defaults[section][name]
            if isinstance(widget, QCheckBox):
                widget.setChecked(value)
            else:
                if widget.findData(value) >= 0:
                    widget.setCurrentIndex(widget.findData(value))
                else:
                    widget.setCurrentText(value)

    def settings(self):
        cfg = copy.deepcopy(self.cfg)
        for key, widget in self.fields.items():
            section, name = key.split(".")
            cfg[section][name] = widget.isChecked() if isinstance(widget, QCheckBox) else (widget.currentData() or widget.currentText())
        return cfg

    def show_help(self):
        box = QMessageBox(self)
        box.setWindowTitle(self.windowTitle() + "帮助")
        box.setText(self.resource.detail + "\n\n" + self.resource.instructions)
        box.addButton("关闭", QMessageBox.ButtonRole.RejectRole)
        if self.resource.url:
            website = box.addButton("打开帮助页面", QMessageBox.ButtonRole.ActionRole)
            website.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(self.resource.url)))
        box.exec()


class MainWindow(QMainWindow):
    model_event = Signal(str)
    def __init__(self, data_dir=None, auto_check=True):
        super().__init__()
        self.setWindowTitle("Voxlate")
        self.resize(1180, 820)
        self.setMinimumSize(900, 650)
        self.data_dir = Path(data_dir or default_data_dir()).resolve()
        self.config_path, self.defaults = prepare_settings(self.data_dir)
        self.config_error = ""
        try:
            self.cfg = load_config(self.config_path)
            if "runtime" not in self.cfg:
                raise VoxlateError("旧配置缺少识别与翻译运行环境，请完成资源配置")
            self.cfg = relocate(self.cfg, resource_root(self.cfg))
        except (ValueError, KeyError, TypeError, OSError, VoxlateError) as exc:
            self.cfg = copy.deepcopy(self.defaults)
            self.config_error = f"配置文件无法读取：{exc}。请检查资源目录并保存；原文件会备份为 config.invalid.json。"
        self.cfg["tts"]["emotion_reference"] = True
        self.cfg['audio_track'] = 0
        self.cfg['audio_track_count'] = 0
        self.audio_tracks_video = None
        self.audio_tracks = []
        self.task = None
        self.model_event.connect(self.notify)
        self.tts_session = TTSSession(self.model_event.emit)
        self.translation_session = TTSSession(self.model_event.emit, kind='translator')
        self.project = None
        self.project_path = None
        self.project_hash = None
        self.dirty = False
        self.resources = []
        self.busy_widgets = []
        self.close_pending = False
        self.installing = False
        self.install_started = None
        self.install_percent = 0
        self.install_detail = ""
        self.install_outcome = ""
        self.status_message = "就绪"
        self.install_timer = QTimer(self)
        self.install_timer.setInterval(1000)
        self.install_timer.timeout.connect(self.render_status)
        self.inspect_after_task = False
        self.inspect_after_keys = None
        self.inspect_after_size_keys = None
        self.output_path = None
        self.selected_video = ""
        self.player_windows = {}
        self.sentence_player = None
        self.playing_sentence = None
        self.sentence_pending = False
        self.sentence_source_key = None
        self.resource_nodes = {}
        self.row_buttons = {}
        self.setAcceptDrops(True)
        self._build()
        self.refresh_voice_fields()
        if auto_check:
            QTimer.singleShot(0, self.restore_resources)

    def _build(self):
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(24, 18, 24, 18)
        self.tabs = QTabWidget()
        self.tabs.addTab(self._dubbing_page(), "视频配音")
        self.tabs.addTab(self._resources_page(), "资源配置")
        layout.addWidget(self.tabs, 1)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        self.progress.hide()
        status_row = QHBoxLayout()
        self.progress.setFixedWidth(150)
        status_row.addWidget(self.progress)
        status_row.addStretch()
        self.activity = QLabel("就绪")
        self.activity.setObjectName("muted")
        self.activity.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.activity.setWordWrap(True)
        status_row.addWidget(self.activity, 1)
        layout.addLayout(status_row)
        self.setCentralWidget(root)

    def button(self, text, callback, primary=False, lock=True):
        button = QPushButton(text)
        button.clicked.connect(callback)
        if primary:
            button.setObjectName("primary")
        if lock:
            self.busy_widgets.append(button)
        return button

    def path_row(self, label, mode, callback=None, filter_text="所有文件 (*)", show_label=True):
        row = QHBoxLayout()
        if show_label:
            row.addWidget(QLabel(label))
        edit = QLineEdit()
        edit.setAcceptDrops(False)
        self.busy_widgets.append(edit)
        row.addWidget(edit, 1)

        def choose():
            if mode == "dir":
                value = QFileDialog.getExistingDirectory(self, label, edit.text())
            elif mode == "save":
                value, _ = QFileDialog.getSaveFileName(self, label, edit.text(), filter_text)
            else:
                value, _ = QFileDialog.getOpenFileName(self, label, edit.text(), filter_text)
            if value:
                edit.setText(value)
                if callback:
                    callback(value)

        row.addWidget(self.button("选择…", choose))
        return row, edit

    def _dubbing_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(18, 18, 18, 18)
        row, self.video = self.path_row('输入视频', 'file', self.video_selected,
            '视频 (*.mp4 *.mov *.mkv *.avi *.webm *.m4v *.ts *.mpg *.mpeg *.wmv *.flv)')
        row.itemAt(row.count()-1).widget().setText('浏览')
        self.project_folder_button = self.button('打开项目文件夹', self.open_project_folder, lock=False)
        self.project_folder_button.setEnabled(False)
        row.addWidget(self.project_folder_button)
        self.clear_project_button = self.button('清空项目', self.clear_project)
        self.clear_project_button.setEnabled(False)
        row.addWidget(self.clear_project_button)
        self.audio_track_box = QComboBox()
        self.audio_track_box.setFixedWidth(170)
        self.audio_track_box.addItem('音轨', 0)
        self.audio_track_box.setEnabled(False)
        self.audio_track_box.setToolTip('选择用于识别和配音的原声音轨；切换后保留各自结果。')
        self.audio_track_box.currentIndexChanged.connect(self.change_audio_track)
        self.busy_widgets.append(self.audio_track_box)
        row.addWidget(self.audio_track_box)
        self.source_language = QComboBox()
        self.source_language.addItem('英文 → 中文', 'en')
        self.source_language.addItem('日文 → 中文', 'ja')
        self.source_language.setCurrentIndex(self.source_language.findData(self.cfg.get('source_lang', 'en')))
        self.source_language.currentIndexChanged.connect(self.change_source_language)
        self.busy_widgets.append(self.source_language)
        row.addWidget(self.source_language)
        layout.addLayout(row)
        voice_row = QHBoxLayout()
        voice_row.addWidget(QLabel('音色参考'))
        self.uniform_voice = QRadioButton('统一音色')
        self.individual_voice = QRadioButton('逐句音色')
        self.role_voice = QRadioButton('分角色音色')
        self.uniform_voice.setToolTip('适合单人')
        self.individual_voice.setToolTip('适合多人')
        self.role_voice.setToolTip('减少同一角色不同句子的音色差异')
        self.voice_group = QButtonGroup(self)
        for button in (self.uniform_voice, self.individual_voice, self.role_voice):
            self.voice_group.addButton(button)
        self.individual_voice.setChecked(True)
        voice_row.addWidget(self.uniform_voice)
        voice_row.addWidget(QLabel('第'))
        self.reference_sentence = ReferenceSentenceBox()
        self.reference_sentence.setRange(0, 0)
        self.reference_sentence.setKeyboardTracking(False)
        self.reference_sentence.setEnabled(False)
        voice_row.addWidget(self.reference_sentence)
        voice_row.addWidget(QLabel('句'))
        voice_row.addSpacing(18)
        voice_row.addWidget(self.individual_voice)
        voice_row.addWidget(self.role_voice)
        self.role_manager_button = self.button('角色管理…', self.manage_roles)
        voice_row.addWidget(self.role_manager_button)
        self.full_segmentation_button = self.button('手动分句...', self.open_full_segmentation)
        self.full_segmentation_button.setToolTip('分离后可用。带入已有句子边界；尚未识别时从空白开始。')
        voice_row.addStretch()
        self.segmentation_range = None
        self.manual_segmentation_button = self.button('局部微调...', self.open_selected_segmentation)
        self.manual_segmentation_button.hide()
        self.release_model_button = self.button('释放模型...', self.release_models)
        self.release_model_button.setToolTip('查看已加载模型，选择释放；下次使用时重新加载。')
        self.release_model_button.setEnabled(False)
        voice_row.addWidget(self.release_model_button)
        self.model_event.connect(self.refresh_model_button)
        self.voice_group.buttonToggled.connect(lambda button, checked: self.change_voice_selection() if checked else None)
        self.reference_sentence.valueChanged.connect(self.change_voice_selection)
        self.busy_widgets.extend((self.uniform_voice, self.individual_voice, self.role_voice, self.reference_sentence))
        layout.addLayout(voice_row)
        self.video.setReadOnly(True)
        self.video.setAcceptDrops(False)
        self.video.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self.video.setPlaceholderText("将英文或日文视频拖到窗口，或点击「选择…」")
        self.table = SentenceTable(0, 7)
        self.table.set_range_button(self.manual_segmentation_button)
        self.table.setHorizontalHeaderLabels(["时间", "原文", "译文", "", "", "角色", ""])
        self.table.timeRangeSelected.connect(self.select_segmentation_range)
        self.table.itemSelectionChanged.connect(self.refresh_segmentation_selection)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        for column in (1, 2):
            self.table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(3, 74)
        # Keep text column indices stable; display original-audio controls after the source text.
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(4, 72)
        self.table.horizontalHeader().moveSection(4, 2)
        self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(5, 116)
        self.table.horizontalHeader().moveSection(5, 1)
        self.table.setColumnHidden(5, True)
        self.table.horizontalHeader().setSectionResizeMode(6, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(6, 36)
        self.table.horizontalHeader().setSectionsClickable(False)
        self.table.verticalHeader().setVisible(True)
        self.table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        for column in (1, 2):
            self.table.setItemDelegateForColumn(column, TranslationDelegate(self.table))
        self.table.itemChanged.connect(self.changed_translation)
        self.table.setEditTriggers(QTableWidget.EditTrigger.SelectedClicked | QTableWidget.EditTrigger.EditKeyPressed)
        self.table_edit_triggers = self.table.editTriggers()
        self.table.setWordWrap(False)
        self.table.verticalHeader().setDefaultSectionSize(36)
        layout.addWidget(self.table, 1)
        actions = QHBoxLayout()
        self.separate_button = self.button('分离', lambda: self.start_pipeline('separate'))
        self.separate_button.setToolTip('提取所选音轨，分离人声与背景；已有匹配结果时直接复用。')
        actions.addWidget(self.separate_button)
        self.recognize_button = self.button("自动分句", lambda: self.start_pipeline("recognize"))
        self.recognize_button.setToolTip('识别已分离的人声；Qwen 自动续跑。应用自动分句会替换手动分句和译文，需重新配音。')
        actions.addWidget(self.recognize_button)
        actions.addWidget(self.full_segmentation_button)
        self.translate_button = self.button("全文翻译", lambda: self.start_pipeline('translate'))
        self.translate_button.setEnabled(False)
        actions.addWidget(self.translate_button)
        self.dub_button = self.button("全文配音", lambda: self.start_pipeline('dub'))
        self.dub_button.setEnabled(False)
        actions.addWidget(self.dub_button)
        self.export_button = self.button("导出", lambda: self.start_pipeline('export'))
        self.export_button.setEnabled(False)
        actions.addWidget(self.export_button)
        self.one_click_button = self.button('一键完成', lambda: self.start_pipeline('auto'), primary=True)
        actions.addWidget(self.one_click_button)
        self.cancel_button = self.button("停止", self.cancel_task, lock=False)
        self.cancel_button.setEnabled(False)
        actions.addWidget(self.cancel_button)
        actions.addStretch()
        self.play_output_button = self.button("播放导出视频", self.play_exported_video)
        actions.addWidget(self.play_output_button)
        layout.addLayout(actions)
        self.logs = ReadableLog()
        self.logs.setReadOnly(True)
        self.logs.setMaximumBlockCount(400)
        self.logs.setMaximumHeight(105)
        self.logs.setPlaceholderText("处理进度会显示在这里；长任务不会阻塞界面。")
        layout.addWidget(self.logs)
        return page

    def _resources_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        toolbar, self.storage = self.path_row("资源目录", "dir", self.change_storage, show_label=False)
        self.resource_toolbar = toolbar
        toolbar.itemAt(1).widget().setText("选择")
        self.storage.setAccessibleName("资源目录")
        self.storage.setText(str(resource_root(self.cfg)))
        self.storage.setToolTip('')
        self.storage.setReadOnly(True)
        self.try_mirrors = QCheckBox("优先镜像站")
        self.try_mirrors.setChecked(self.cfg.get("try_mirrors", True))
        self.try_mirrors.setToolTip("优先尝试普通依赖和 Hugging Face 模型的镜像，失败后改用原站；其他资源使用原站。")
        self.try_mirrors.toggled.connect(self.save_mirror_preference)
        self.busy_widgets.append(self.try_mirrors)
        toolbar.addWidget(self.try_mirrors)
        self.install_all_button = self.button("一键准备", lambda: self.install_resources(), primary=True)
        toolbar.addWidget(self.install_all_button)
        toolbar.addWidget(self.button("检查", self.save_and_inspect))
        self.install_cancel_button = self.button("停止", self.cancel_task, lock=False)
        self.install_cancel_button.setEnabled(False)
        toolbar.addWidget(self.install_cancel_button)
        layout.addLayout(toolbar)
        self.resource_tree = QTreeWidget()
        self.resource_tree.setStyleSheet("QTreeWidget::item { padding: 3px 2px; } QTreeWidget QPushButton { padding: 3px 10px; }")
        self.resource_tree.setColumnCount(6)
        self.resource_tree.setHeaderLabels(["检查项", "空间", "状态", "", "", ""])
        self.resource_tree.header().setStretchLastSection(False)
        self.resource_tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column, width in ((1, 125), (2, 85), (3, 100), (4, 100), (5, 100)):
            self.resource_tree.header().setSectionResizeMode(column, QHeaderView.ResizeMode.Fixed)
            self.resource_tree.setColumnWidth(column, width)
        self.resource_tree.setRootIsDecorated(True)
        self.resource_tree.setUniformRowHeights(True)
        self.resource_tree.itemSelectionChanged.connect(self.show_resource)
        layout.addWidget(self.resource_tree, 1)
        self.install_logs = ReadableLog()
        self.install_logs.setReadOnly(True)
        self.install_logs.setMaximumBlockCount(500)
        self.install_logs.setMaximumHeight(120)
        self.install_logs.setPlaceholderText("选中检查项查看说明")
        layout.addWidget(self.install_logs)
        return page

    def refresh_settings_fields(self):
        self.cfg["tts"]["emotion_reference"] = True
        self.storage.setText(str(resource_root(self.cfg)))
        self.storage.setToolTip('')
        self.try_mirrors.blockSignals(True)
        self.try_mirrors.setChecked(self.cfg.get("try_mirrors", True))
        self.try_mirrors.blockSignals(False)

    def save_mirror_preference(self, checked):
        previous = self.cfg.get("try_mirrors", True)
        self.cfg["try_mirrors"] = checked
        try:
            self.save_settings()
        except (OSError, ValueError, VoxlateError) as exc:
            self.cfg["try_mirrors"] = previous
            self.refresh_settings_fields()
            QMessageBox.warning(self, "设置未保存", str(exc))

    def edit_resource_settings(self, key):
        if self.task or key not in ("runtime", "qwen", "separator", "tts"):
            return
        resource = self.display_resources[key]
        dialog = ResourceSettingsDialog(key, self.cfg, self.defaults, resource, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            updated = dialog.settings()
            if updated == self.cfg:
                return
            previous = self.cfg
            self.cfg = updated
            try:
                self.save_settings()
            except (OSError, ValueError, VoxlateError) as exc:
                self.cfg = previous
                QMessageBox.warning(self, "设置未保存", str(exc))
                return
            self.refresh_resource_requirements()
            self.notify("设置已保存，使用时检查。")

    def change_storage(self, value):
        try:
            self.save_settings()
            remember_root(self.config_path, resource_root(self.cfg))
            self.cfg = relocate(self.cfg, value)
            remember_root(self.config_path, resource_root(self.cfg))
            self.save_settings()
            self.refresh_settings_fields()
            self.restore_resources()
        except (OSError, ValueError, VoxlateError) as exc:
            QMessageBox.warning(self, "无法更换存放位置", str(exc))

    def selected_resource_key(self):
        item = self.resource_tree.currentItem()
        return item.data(0, Qt.ItemDataRole.UserRole) if item else None

    def cleanup_protected(self):
        return [p for p in (self.video.text().strip(), self.output_path,
                           self.project_path.parent if self.project_path else None) if p]

    def review_cleanup(self, key=None):
        if self.task:
            return
        cfg = copy.deepcopy(self.cfg)
        protected = self.cleanup_protected()
        def collect(emit):
            emit("正在统计资源目录和已占用大小…")
            entries = cleanup_inventory(cfg, self.config_path, protected, keys={key} if key else None)
            entries = [e for e in entries if e.key == key] if key else entries
            removable = [e for e in entries if e.deletable]
            emit(f"统计完成：{format_bytes(sum(e.size for e in removable))}，{len(entries)} 个目录。")
            return entries
        self.start_task(collect, lambda entries: QTimer.singleShot(0, lambda: self.show_cleanup_dialog(entries)))

    def show_cleanup_dialog(self, entries):
        if self.task:
            QTimer.singleShot(50, lambda: self.show_cleanup_dialog(entries))
            return
        if not entries:
            QMessageBox.information(self, "删除资源", "未找到资源文件。")
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("删除 · " + entries[0].title)
        dialog.setMinimumWidth(620)
        layout = QVBoxLayout(dialog)
        choices = []
        for entry in entries:
            purpose = QLabel(PURPOSES[entry.key])
            purpose.setWordWrap(True)
            layout.addWidget(purpose)
            row = QHBoxLayout()
            choice = None
            if len(entries) > 1:
                choice = QCheckBox()
                choice.setEnabled(entry.deletable)
                choice.setChecked(entry.deletable and entry.root.resolve() == resource_root(self.cfg))
                row.addWidget(choice)
            path = QLabel(str(entry.path))
            path.setTextFormat(Qt.TextFormat.PlainText)
            path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            path.setWordWrap(True)
            path.setMinimumWidth(480)
            path.setToolTip(str(entry.path))
            row.addWidget(path, 1)
            row.addWidget(QLabel(format_bytes(entry.size) if entry.deletable else "不可删除"))
            layout.addLayout(row)
            if entry.reason:
                reason = QLabel(entry.reason)
                reason.setWordWrap(True)
                layout.addWidget(reason)
            choices.append((entry, choice))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        remove = buttons.addButton("确认删除", QDialogButtonBox.ButtonRole.AcceptRole)
        remove.setAutoDefault(False)
        def selected_entries():
            return [entry for entry, choice in choices if entry.deletable and (choice is None or choice.isChecked())]
        remove.setEnabled(bool(selected_entries()))
        for _, choice in choices:
            if choice is not None:
                choice.toggled.connect(lambda: remove.setEnabled(bool(selected_entries())))
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        selected = selected_entries()
        if selected:
            cfg = copy.deepcopy(self.cfg)
            cached = copy.deepcopy(self.resources)
            affected = {entry.key for entry in selected}
            protected = self.cleanup_protected()
            self.inspect_after_task = True
            self.inspect_after_keys = affected
            self.inspect_after_size_keys = affected
            def action(emit):
                count = clean_resources(selected, cfg, self.config_path, emit, protected)
                emit(f"已清理 {count} 项资源，正在重新检查…")
                return check_resources(cfg, self.config_path, keys=affected, cached=cached)
            def complete(results):
                self.inspect_after_task = False
                self.inspect_after_keys = None
                self.inspect_after_size_keys = None
                self.resources_checked(results)
            self.start_task(action, complete, keep_tts=not bool(affected & {'tts', 'tts_model', 'python', 'python_bin'}), keep_translation=False,
                            release_reason='删除音色克隆相关资源')

    def install_resources(self, selected=None):
        if self.task:
            return
        try:
            self.save_settings()
        except (OSError, ValueError, VoxlateError) as exc:
            QMessageBox.warning(self, "设置未保存", str(exc))
            return
        cfg = copy.deepcopy(self.cfg)
        track = cfg.pop('audio_track', 0)
        track_count = cfg.pop('audio_track_count', 0)
        cached = copy.deepcopy(self.resources)
        affected = {selected} if selected else {r.key for r in cached if not r.ready}
        self.inspect_after_keys = affected
        self.inspect_after_size_keys = affected | {"download_cache", "prepare_env", "python", "python_bin", "uv"}
        self.installing = True
        self.tabs.setCurrentIndex(1)
        self.set_status("正在联网准备资源。可停止并保留已完成步骤，完成后会自动检查。")

        def action(emit, progress):
            emit("确认缺失资源…")
            current = check_resources(cfg, self.config_path, keys=affected, cached=cached, size_keys=set())
            stages = planned_stages(current, selected)
            # Keep the failure/cancellation refresh limited to the attempted resources.
            affected.update(stages)
            if stages:
                updated = Installer(cfg, self.config_path, emit, progress=progress).install(stages)
            else:
                emit("所需资源已经就绪，无需下载")
                updated = cfg
            emit("准备步骤结束，正在验证运行环境和模型文件…")
            progress({"percent": 95, "detail": "检查资源"})
            return updated, check_resources(updated, self.config_path, keys=set(stages), cached=current,
                                            size_keys=set(stages) | {"download_cache", "prepare_env", "python", "python_bin", "uv"})

        def complete(result):
            self.cfg, results = result
            self.cfg['audio_track'] = track
            self.cfg['audio_track_count'] = track_count
            self.refresh_settings_fields()
            self.resources_checked(results)
            self.install_outcome = "完成" if all(r.ready for r in results if (selected is None and r.required) or r.key == selected or (selected == "ffmpeg" and r.key == "ffprobe")) else "准备结束，仍有项目未就绪"
            self.install_percent = 100
            self.installing = False
            self.inspect_after_keys = None
            self.inspect_after_size_keys = None

        self.start_task(action, complete, cancellable=True, with_progress=True,
                        keep_tts=not bool(affected & {'tts', 'tts_model', 'python', 'python_bin'}), keep_translation=False,
                        release_reason='安装音色克隆相关资源')

    def notify(self, message):
        self.set_status(message)
        self.logs.appendPlainText(message)
        self.install_logs.appendPlainText(message)

    def set_status(self, message):
        self.status_message = message
        self.render_status()

    def install_elapsed(self):
        seconds = max(0, int(time.monotonic() - self.install_started)) if self.install_started is not None else 0
        hours, seconds = divmod(seconds, 3600)
        minutes, seconds = divmod(seconds, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"

    def render_status(self):
        message = self.status_message.replace("\n", " ")[:120]
        if self.install_started is not None:
            detail = self.install_detail or message
            message = f"约 {int(self.install_percent)}% · 已用 {self.install_elapsed()} · {detail}"
        self.activity.setText(message)
        self.activity.setToolTip('')

    def update_install_progress(self, update):
        if update.get("stage") == "sentence_timing":
            if self.project:
                for row, segment in enumerate(self.project["segments"]):
                    if segment["id"] == update["id"]:
                        segment.update(generated_duration=update["generated_duration"], timing_text=update["timing_text"])
                        self.update_sentence_style(row)
                        break
            return
        if update.get("stage") == "project_ready":
            self.load_project(Path(update["path"]), project=update["project"])
            return
        if update.get("stage") == "tts":
            self.progress.setRange(0, max(1, update["total"]))
            self.progress.setValue(update["completed"])
            self.set_status(update["detail"])
            return
        if update.get("stage") == "processing":
            self.progress.setRange(0, 0)
            if update.get('detail'):
                self.set_status(update['detail'])
            return
        if self.install_started is None:
            return
        node = self.resource_nodes.get(update.get("resource_key"))
        if node is not None and "resource_bytes" in update:
            node.setText(1, format_bytes(update["resource_bytes"]))
        self.install_percent = max(self.install_percent, min(99, float(update["percent"])))
        self.install_detail = update.get("detail", "")
        self.progress.setValue(int(self.install_percent))
        self.render_status()

    def start_task(self, action, complete, cancellable=False, with_progress=False, keep_tts=True, keep_translation=True,
                   release_reason='开始识别翻译，腾出显存'):
        if self.task:
            return
        if not keep_tts:
            self.tts_session.close(release_reason)
        if not keep_translation:
            self.translation_session.close(release_reason)
        self.task = TaskThread(action, with_progress=with_progress)
        for widget in self.busy_widgets:
            widget.setEnabled(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        for row in range(self.table.rowCount()):
            if combo := self.table.cellWidget(row, 5):
                combo.setEnabled(False)
        self.refresh_sentence_buttons()
        self.set_resource_controls_enabled(False)
        self.cancel_button.setEnabled(cancellable)
        self.install_cancel_button.setEnabled(cancellable and self.installing)
        self.progress.setRange(0, 0)
        if with_progress:
            self.install_started = time.monotonic()
            self.install_percent = 0
            self.install_detail = "检查待准备资源"
            self.install_outcome = ""
            self.progress.setRange(0, 100)
            self.progress.setValue(0)
            self.install_timer.start()
            self.render_status()
        self.progress.show()
        self.task.status.connect(self.notify)
        self.task.progress.connect(self.update_install_progress)
        self.task.result.connect(complete)
        self.task.failure.connect(self.failed)
        self.task.finished.connect(self.task_finished)
        self.task.start()

    def task_finished(self):
        if self.install_started is not None:
            elapsed = self.install_elapsed()
            self.install_started = None
            self.install_timer.stop()
            completed = "100% · " if self.install_percent == 100 else ""
            self.notify(f"{completed}{self.install_outcome or '准备结束'} · 用时 {elapsed}。{self.status_message}")
        self.task.deleteLater()
        self.task = None
        self.progress.setRange(0, 1)
        self.progress.setValue(1)
        self.progress.hide()
        self.cancel_button.setEnabled(False)
        self.install_cancel_button.setEnabled(False)
        for widget in self.busy_widgets:
            widget.setEnabled(True)
        self.table.setEditTriggers(self.table_edit_triggers)
        self.refresh_export_state()
        self.set_resource_controls_enabled(True)
        if self.close_pending:
            self.close()
        elif self.inspect_after_task:
            self.inspect_after_task = False
            keys = self.inspect_after_keys
            size_keys = self.inspect_after_size_keys
            self.inspect_after_keys = None
            self.inspect_after_size_keys = None
            QTimer.singleShot(0, lambda: self.inspect(keys=keys, size_keys=size_keys))

    def failed(self, text):
        if self.install_started is not None:
            self.install_outcome = "已停止" if "已取消" in text else "准备未完成"
            self.install_detail = self.install_outcome
        if self.installing:
            if "已取消" in text:
                text = "已取消准备。已完成文件和未完成下载均保留；再次安装或下载会尝试续传。"
            self.installing = False
            track = selected_track(self.cfg)
            track_count = self.cfg.get('audio_track_count', 0)
            self.cfg = load_config(self.config_path)
            self.cfg['audio_track'] = track
            self.cfg['audio_track_count'] = track_count
            self.refresh_settings_fields()
            self.inspect_after_task = True
        if self.project_path and self.project_path.exists():
            self.load_project(self.project_path)
        self.set_status("处理未完成：" + text)
        self.notify(text)
        if "已取消" not in text:
            detail = text + "\n\n已完成的步骤保留，解决上述问题后可继续处理。"
            if self.project_path:
                detail += f"\n详细日志位置：{self.project_path.parent}"
            QMessageBox.warning(self, "需要处理的问题", detail)

    def inspect(self, *, keys=None, size_keys=None):
        self.set_status(self.config_error or ("正在检查变动项…" if keys is not None else "正在检查程序、模型资源与计算设备…"))
        cfg = copy.deepcopy(self.cfg)
        cached = copy.deepcopy(self.resources)
        self.start_task(lambda emit: check_resources(cfg, self.config_path,
                        report=lambda item: emit(f"检查 {item.title}：{'通过' if item.ready else ('需要配置' if item.required else '可选，未准备')}"),
                        keys=keys, cached=cached, size_keys=size_keys), self.resources_checked)

    def resources_checked(self, results, *, source='checked'):
        self.resources = results
        try:
            save_resource_cache(self.cfg, self.config_path, results)
        except OSError as exc:
            self.install_logs.appendPlainText(f"检查结果未能保存：{exc}")
        sizes = getattr(results, "sizes", {})
        estimates = getattr(results, "estimates", {})
        if getattr(results, "summary", ""):
            self.install_logs.appendPlainText(results.summary)
        previous = self.selected_resource_key()
        self.resource_tree.blockSignals(True)
        self.row_buttons.clear()
        self.resource_tree.clear()
        self.combined_asr = None
        if hasattr(self, "model_group"):
            self.model_group.deleteLater()
        self.model_group = QButtonGroup(self)
        self.model_buttons = {}
        if hasattr(self, "asr_model_group"):
            self.asr_model_group.deleteLater()
        self.asr_model_group = QButtonGroup(self)
        self.asr_model_buttons = {}
        self.resource_nodes = {}
        self.display_resources = {r.key: r for r in results if r.key not in ("ffmpeg", "ffprobe")}
        programs = [r for r in results if r.key in ("ffmpeg", "ffprobe")]
        if programs and not all(r.ready for r in programs):
            self.display_resources["ffmpeg"] = ResourceStatus("ffmpeg", CATALOG["ffmpeg"].title, False,
                "\n".join(r.detail for r in programs if not r.ready), programs[0].instructions, programs[0].url)
        extra_paths = extra_resource_paths(self.cfg, self.config_path)
        descriptions = {
            "download_cache": "可删除以释放空间；下次安装可能需要重新下载。",
            "prepare_env": "模型下载工具；旧版转换用的 PyTorch 也计入此项。删除不影响配音。",
            "python": "运行环境共用的 Python；仍被使用时不能删除。",
            "python_bin": "用于启动共享 Python。",
            "uv": "用于安装环境，删除后需要时会自动下载。",
        }
        for key, paths in extra_paths.items():
            if paths:
                self.display_resources[key] = ResourceStatus(key, EXTRAS[key].title, True,
                    descriptions[key] + "\n" + "\n".join(str(p) for p in paths), "删除前会显示占用空间，并再次确认。")
        present_paths = tracked_resource_paths(self.cfg, self.config_path)
        catalog = {**CATALOG, **EXTRAS}
        for category in ("程序", "环境", "模型", "其它"):
            keys = [k for k, info in catalog.items() if info.category == category and k in self.display_resources]
            if not keys:
                continue
            group = QTreeWidgetItem(self.resource_tree, [category])
            group.setSizeHint(0, QSize(0, 38))
            group.setFlags(group.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            model_sections = {}
            for key in keys:
                parent = group
                if key in ASR_MODELS or (key in MODELS and len(MODELS) > 1):
                    section = "识别模型" if key in ASR_MODELS else "翻译模型"
                    if section not in model_sections:
                        heading = QTreeWidgetItem(group, [section])
                        heading.setFlags(heading.flags() & ~Qt.ItemFlag.ItemIsSelectable)
                        heading.setForeground(0, QColor("#61728a"))
                        heading.setExpanded(True)
                        model_sections[section] = heading
                    parent = model_sections[section]
                resource, info = self.display_resources[key], catalog[key]
                status = "已存在" if key in EXTRAS else ("已就绪" if resource.ready else "待准备")
                space = format_bytes(sizes[key]) if key in sizes else estimates.get(key, info.estimate)
                if key in sizes and not resource.ready:
                    space = "已有 " + space
                title = f'翻译模型 · {info.title}' if key in MODELS and len(MODELS) == 1 else info.title
                node = QTreeWidgetItem(parent, [title, space, status])
                node.setData(0, Qt.ItemDataRole.UserRole, key)
                node.setForeground(2, QColor("#22805e" if resource.ready else "#b26113"))
                node.setToolTip(1, '')
                node.setToolTip(2, resource.detail + "\n" + resource.instructions)
                if key in ASR_MODELS or (key in MODELS and len(MODELS) > 1):
                    recognition = key in ASR_MODELS
                    choice = QRadioButton(info.title)
                    choice.setChecked(key == (self.recognition_choice() if recognition else selected_key(self.cfg)))
                    choice.setToolTip("选择当前使用的识别模型。切换后使用独立项目，原译文和配音保留。" if recognition else "选择当前使用的翻译模型；未下载时请点击本行「下载」。")
                    (self.asr_model_group if recognition else self.model_group).addButton(choice)
                    (self.asr_model_buttons if recognition else self.model_buttons)[key] = choice
                    node.setText(0, "")
                    self.resource_tree.setItemWidget(node, 0, choice)
                    choice.clicked.connect(lambda checked=False, item=node: self.resource_tree.setCurrentItem(item))
                    choice.clicked.connect(lambda checked=False, k=key: self.change_recognition_model(k) if k in ASR_MODELS else self.change_translation_model(k))
                    if not resource.required:
                        node.setText(2, "已下载" if resource.ready else "可选")
                self.resource_nodes[key] = node
                actions = {}
                if key in CATALOG and not resource.ready:
                    actions["install"] = (3, "下载" if info.category == "模型" else "安装", lambda checked=False, k=key: self.install_resources(k))
                if key in ("runtime", "qwen", "separator", "tts"):
                    actions["settings"] = (4, "设置…", lambda checked=False, k=key: self.edit_resource_settings(k))
                if present_paths.get(key):
                    actions["delete"] = (5, "删除…", lambda checked=False, k=key: self.review_cleanup(k))
                self.row_buttons[key] = {}
                for operation, (column, title, action) in actions.items():
                    button = self.button(title, action, lock=False)
                    self.resource_tree.setItemWidget(node, column, button)
                    self.row_buttons[key][operation] = button
            if '识别模型' in model_sections:
                heading = model_sections['识别模型']
                combined_node = QTreeWidgetItem()
                turbo = self.resource_nodes.get('asr_turbo_model')
                heading.insertChild(heading.indexOfChild(turbo) + 1 if turbo is not None else heading.childCount(), combined_node)
                container = QWidget()
                row = QHBoxLayout(container)
                row.setContentsMargins(0, 0, 0, 0)
                self.combined_asr = QRadioButton('综合识别（turbo + v3）')
                self.asr_model_group.addButton(self.combined_asr)
                self.asr_model_buttons['asr_combined'] = self.combined_asr
                self.combined_asr.setChecked(self.cfg['asr'].get('combined', False))
                self.combined_asr.setToolTip('同时使用 v3 和 turbo，复查疑似漏句；仅合并两者一致的可靠补句。')
                row.addWidget(self.combined_asr)
                hint = QLabel('稍慢，尝试减少漏识别')
                hint.setObjectName('muted')
                row.addWidget(hint)
                row.addStretch()
                self.resource_tree.setItemWidget(combined_node, 0, container)
                self.combined_asr.clicked.connect(lambda: self.change_recognition_model('asr_combined'))
            group.setExpanded(True)
        self.set_resource_controls_enabled(self.task is None)
        self.resource_tree.blockSignals(False)
        missing = sum(not r.ready and r.required for r in self.display_resources.values())
        self.tabs.setTabText(1, "资源配置")
        ready_message = {'checked': '资源检查通过。', 'cached': '资源就绪（沿用上次检查）。',
                         'quick': '资源就绪（快速检查）。'}[source]
        source_note = '（沿用上次检查）' if source == 'cached' else ''
        self.notify(self.config_error or (f"{missing} 项待准备{source_note}，点击「一键准备」。" if missing else ready_message))
        if missing or self.config_error:
            self.tabs.setCurrentIndex(1)
        key = previous if previous in self.resource_nodes else next((k for k, r in self.display_resources.items() if not r.ready and r.required), next(iter(self.resource_nodes), None))
        if key:
            self.resource_tree.setCurrentItem(self.resource_nodes[key])

    def set_resource_controls_enabled(self, enabled):
        for actions in self.row_buttons.values():
            for button in actions.values():
                button.setEnabled(enabled)
        for button in getattr(self, "model_buttons", {}).values():
            button.setEnabled(enabled)
        for button in getattr(self, "asr_model_buttons", {}).values():
            button.setEnabled(enabled)

    def restore_resources(self):
        cached = load_resource_cache(self.cfg, self.config_path)
        if cached is not None:
            self.resources_checked(cached, source='cached')
        else:
            self.start_task(lambda emit: check_resources(self.cfg, self.config_path, quick=True),
                            self.resources_restored)

    def resources_restored(self, results):
        self.resources_checked(results, source='quick')

    def recognition_choice(self):
        return 'asr_combined' if self.cfg['asr'].get('combined', False) else selected_asr_key(self.cfg)

    def sync_recognition_choice(self):
        button = getattr(self, 'asr_model_buttons', {}).get(self.recognition_choice())
        if button is not None:
            button.setChecked(True)

    def change_recognition_model(self, key):
        if self.task or key == self.recognition_choice():
            self.sync_recognition_choice()
            return
        old = copy.deepcopy(self.cfg)
        if not self.confirm_discard():
            self.sync_recognition_choice()
            return
        if key == 'asr_combined':
            if selected_asr_key(self.cfg) == 'asr_qwen_model':
                select_asr_model(self.cfg, 'asr_turbo_model', resource_root(self.cfg))
        else:
            select_asr_model(self.cfg, key, resource_root(self.cfg))
        self.cfg['asr']['combined'] = key == 'asr_combined'
        try:
            self.save_settings()
        except (OSError, ValueError, VoxlateError) as exc:
            self.cfg = old
            self.sync_recognition_choice()
            QMessageBox.warning(self, "设置未保存", str(exc))
            return
        self.project = self.project_hash = None
        self.dirty = False
        self.table.setRowCount(0)
        self.restore_voice_selection()
        self.project_path = self.default_project_path(self.video.text()) if self.video.text().strip() else None
        if self.project_path and self.project_path.exists():
            self.load_project(self.project_path)
        self.refresh_export_state()
        self.sync_recognition_choice()
        title = '综合识别（turbo + v3）' if key == 'asr_combined' else ASR_MODELS[key]['title']
        self.notify("已选择 " + title + "；原模型的项目已保留，请点击「识别」。")
        self.refresh_resource_requirements()

    def refresh_resource_requirements(self):
        """Update selection and requirements without probing or walking resources."""
        for resource in self.resources:
            resource.required = resource_required(self.cfg, resource.key)
            if resource.key in getattr(self, 'display_resources', {}):
                self.display_resources[resource.key] = resource
            node = getattr(self, 'resource_nodes', {}).get(resource.key)
            if node is not None:
                node.setText(2, ('已就绪' if resource.required else '已下载') if resource.ready
                             else ('待准备' if resource.required else '可选'))
                node.setForeground(2, QColor('#22805e' if resource.ready else '#b26113'))
                node.setToolTip(2, resource.detail + '\n' + resource.instructions)
        self.sync_recognition_choice()
        try:
            save_resource_cache(self.cfg, self.config_path, self.resources)
        except OSError as exc:
            self.install_logs.appendPlainText(f'检查结果未能保存：{exc}')

    def change_translation_model(self, key):
        if self.task or key == selected_key(self.cfg):
            return
        old = copy.deepcopy(self.cfg)
        select_model(self.cfg, key, resource_root(self.cfg))
        try:
            self.save_settings()
        except (OSError, ValueError, VoxlateError) as exc:
            self.cfg = old
            self.model_buttons[selected_key(old)].setChecked(True)
            QMessageBox.warning(self, "设置未保存", str(exc))
            return
        self.notify("已选择 " + MODELS[key]["title"] + "；下次翻译将使用此模型。")
        self.refresh_export_state()
        self.refresh_resource_requirements()

    def show_resource(self):
        resource = getattr(self, "display_resources", {}).get(self.selected_resource_key())
        if resource:
            key = resource.key
            paths = extra_resource_paths(self.cfg, self.config_path).get(key, []) if key in EXTRAS else configured_paths(self.cfg, key)
            if key == "tts":
                paths = ["程序目录：" + str(Path(self.cfg["tts"]["repo_path"])),
                         "运行环境：" + str(Path(self.cfg["tts"]["python"]).parent.parent)]
            notes = {
                "download_cache": "删除后可能需要重新下载。",
                "prepare_env": "模型准备工具；删除不影响配音。",
                "python": "运行环境共用，使用中不能删除。",
                "python_bin": "共享 Python 的启动文件。",
                "uv": "环境安装工具，可重新下载。",
                "tts": "用于克隆音色；也提供识别所需的 GPU 库。",
                "separator": "分离人声与背景。",
                "runtime": "识别与翻译共用的运行环境。",
            }
            detail = notes.get(key, "")
            if key in ASR_MODELS:
                detail = "支持英文、日文。"
            elif key in MODELS:
                detail = "英文、日文直接译成中文。"
            if not resource.ready and resource.required:
                detail += "\n未就绪，可安装后重新检查。"
            title = CATALOG[key].title if key in CATALOG else resource.title
            self.install_logs.appendPlainText("\n".join(s for s in
                [f"[{title}]", *(str(path) for path in paths), detail.strip()] if s))

    def save_settings(self):
        cfg = relocate(self.cfg, resource_root(self.cfg))
        # Per-video choices belong to the video project, never global settings.
        track = selected_track(self.cfg)
        track_count = cfg.pop('audio_track_count', 0)
        cfg.pop('audio_track', None)
        if self.config_error and self.config_path.exists():
            backup = self.config_path.with_name("config.invalid.json")
            if not backup.exists():
                backup.write_bytes(self.config_path.read_bytes())
        write_json(self.config_path, cfg)
        self.cfg = load_config(self.config_path)
        self.cfg['audio_track'] = track
        self.cfg['audio_track_count'] = track_count
        self.config_error = ""

    def save_and_inspect(self):
        try:
            self.save_settings()
        except (OSError, ValueError, VoxlateError) as exc:
            QMessageBox.warning(self, "设置未保存", str(exc))
            return
        self.inspect()

    def open_local(self, value):
        path = Path(str(value))
        if not str(value).strip() or not path.exists():
            QMessageBox.information(self, "文件尚未准备好", "请先选择文件或完成导出。")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.resolve())))

    def confirm_discard(self):
        return not self.dirty or self.save_translations()

    def default_project_path(self, video):
        return existing_project_directory(video, self.cfg) / "project.json"

    def play_exported_video(self):
        path = self.output_path
        if (not path or not Path(path).is_file()) and self.project:
            recorded = self.project.get('output')
            if recorded and Path(recorded).is_file():
                path = Path(recorded)
        self.play_video(path)

    def play_video(self, path):
        if not path or not Path(path).is_file():
            QMessageBox.information(self, '请选择视频', '先选择视频或完成导出。')
            return
        from .player import VideoPlayer
        key = (str(Path(path).resolve()), False, None)
        if key in self.player_windows:
            player = self.player_windows[key]
            player.showNormal()
            player.raise_()
            player.activateWindow()
            return
        original = self.project_path.parent/'original.wav' if self.project_path else None
        from .app_settings import player_volume
        player = VideoPlayer(path, self, select_reference=False, original_audio=original,
                             volume=player_volume(self.data_dir))
        player.volumeChanged.connect(self.remember_player_volume)
        self.player_windows[key] = player
        def finished(result):
            self.player_windows.pop(key, None)
            player.deleteLater()
        player.finished.connect(finished)
        player.show()

    def remember_player_volume(self, value):
        from .app_settings import save_player_volume
        try:
            save_player_volume(self.data_dir, value)
        except OSError as exc:
            self.notify('播放器音量未能保存：' + str(exc))

    def close_output_players(self, output):
        for key, player in list(self.player_windows.items()):
            if key[0] == str(Path(output).resolve()):
                player.close()

    def restore_voice_selection(self):
        mode, ident = voice_selection(self.project) if self.project is not None else ('individual', None)
        if automatic_reference(self.project):
            ident = None
        segments = (self.project or {}).get('segments', [])
        self.voice_group.blockSignals(True)
        self.reference_sentence.blockSignals(True)
        self.uniform_voice.setChecked(mode == 'uniform')
        self.individual_voice.setChecked(mode == 'individual')
        self.role_voice.setChecked(mode == 'roles')
        self.reference_sentence.setRange(0, max((s['id'] for s in segments), default=0))
        self.reference_sentence.setValue(ident or 0)
        self.reference_sentence.refresh_steps()
        self.voice_group.blockSignals(False)
        self.reference_sentence.blockSignals(False)
        if self.project is not None:
            self.project.update(voice_mode=mode, reference_sentence_id=ident)
            if mode == 'roles':
                ensure_roles(self.project)
                validate_roles(self.project, allow_legacy_names=True)
        self.refresh_role_column()
        self.refresh_voice_fields()

    def change_voice_selection(self, value=None):
        if self.task is not None:
            return
        self.stop_sentence_audio()
        if self.project:
            ident = self.reference_sentence.value()
            if ident and ident not in {s['id'] for s in self.project['segments']}:
                self.restore_voice_selection()
                return
            self.dirty = True
            if not self.save_translations():
                self.restore_voice_selection()
                return
        self.refresh_role_column()
        self.refresh_voice_fields()
        if self.role_voice.isChecked() and self.project and self.project.get('segments') and not self.project.get('roles_initialized'):
            QTimer.singleShot(0, self.auto_assign_roles)

    def selected_voice_mode(self):
        return 'roles' if self.role_voice.isChecked() else ('uniform' if self.uniform_voice.isChecked() else 'individual')

    def refresh_role_column(self):
        self.table.setColumnHidden(5, not self.role_voice.isChecked())
        roles = (self.project or {}).get('roles', [])
        for row, segment in enumerate((self.project or {}).get('segments', [])):
            if row >= self.table.rowCount():
                break
            combo = CenteredComboBox()
            for role in roles:
                combo.addItem(role['name'], role['id'])
                combo.setItemData(combo.count()-1, Qt.AlignmentFlag.AlignCenter, Qt.ItemDataRole.TextAlignmentRole)
            combo.setCurrentIndex(combo.findData(segment.get('role_id')))
            combo.setToolTip('自动分组存疑，请试听确认。' if segment.get('role_uncertain') else '')
            if segment.get('role_uncertain'):
                combo.setStyleSheet('QComboBox {color:#9a6100;}')
            combo.setEnabled(self.task is None)
            combo.currentIndexChanged.connect(lambda index, r=row: self.assign_role(r))
            self.table.setCellWidget(row, 5, combo)

    def assign_role(self, row):
        if self.task is not None or not self.project:
            return
        self.stop_sentence_audio()
        self.dirty = True
        assignments = {self.project['segments'][row]['id']: dict(role_id=self.table.cellWidget(row, 5).currentData(), role_uncertain=False)}
        self.save_translations(assignments=assignments, roles_initialized=True)
        self.refresh_role_column()

    def manage_roles(self):
        if self.task is not None or not self.project or not self.project.get('segments'):
            return
        if not self.save_translations():
            return
        candidate = copy.deepcopy(self.project)
        ensure_roles(candidate)
        def preview(ident):
            row = next(i for i, s in enumerate(self.project['segments']) if s['id'] == ident)
            self.play_sentence(row, original=True)
        dialog = RoleDialog(candidate, preview, self)
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        self.stop_sentence_audio()
        if accepted:
            if dialog.auto_requested:
                self.auto_assign_roles()
            else:
                self.dirty = True
                self.save_translations(roles=dialog.roles, roles_initialized=True)
                self.refresh_role_column()

    def auto_assign_roles(self):
        if self.task is not None or not self.project or not self.project.get('segments'):
            return
        if not self.save_translations():
            return
        project, path, cfg = copy.deepcopy(self.project), self.project_path, copy.deepcopy(self.cfg)
        def action(emit):
            with project_lock(path.parent):
                pipeline = VideoDubPipeline(cfg, tts_session=self.tts_session, translation_session=self.translation_session)
                pipeline.work, pipeline.project = path.parent, project
                _, vocals, _ = pipeline.cached_media()
                emit('正在自动分组（CAMPPlus，本地 CPU）…')
                return pipeline.analyze_reference_voices(vocals, refresh=True)
        def complete(result):
            assignments = {int(k): v for k, v in result['assignments'].items()}
            self.dirty = True
            previous_mode = self.selected_voice_mode()
            if self.save_translations(roles=result['roles'], assignments=assignments, roles_initialized=True,
                                      automatic_role_count=len(result['roles'])):
                self.restore_voice_selection()
                uncertain = sum(bool(v.get('role_uncertain')) for v in assignments.values())
                if previous_mode != self.selected_voice_mode():
                    self.notify(f"识别到 {len(result['roles'])} 个角色，已切换逐句音色；仍可手动改回。")
                else:
                    self.notify(f"已分为 {len(result['roles'])} 个角色，{uncertain} 句待确认；可在角色管理中调整。")
        self.start_task(action, complete, cancellable=True)

    def refresh_voice_fields(self):
        self.refresh_export_state()

    def refresh_model_button(self, *_):
        self.release_model_button.setEnabled(self.task is None and (self.tts_session.is_alive or self.translation_session.is_alive))

    def release_models(self):
        if self.task is not None:
            self.refresh_model_button()
            return
        dialog = QDialog(self)
        dialog.setWindowTitle('释放模型')
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel('勾选要释放的模型，下次使用时会重新加载。'))
        choices = []
        for session in (self.translation_session, self.tts_session):
            if session.is_alive:
                checkbox = QCheckBox(session.name)
                checkbox.setChecked(True)
                layout.addWidget(checkbox)
                choices.append((checkbox, session))
        if not choices:
            layout.addWidget(QLabel('当前没有保留的模型。'))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        confirm = buttons.button(QDialogButtonBox.StandardButton.Ok)
        confirm.setText('释放所选')
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('取消')
        def update():
            confirm.setEnabled(any(box.isChecked() for box, _ in choices))
        for box, _ in choices:
            box.toggled.connect(update)
        update()
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            selected = [session for box, session in choices if box.isChecked()]
            self.start_task(lambda emit: [session.close('手动释放') for session in selected],
                            lambda result: self.refresh_model_button())

    def translate_sentence(self, row):
        if self.task is not None or not self.project or not 0 <= row < len(self.project['segments']):
            return
        if not self.save_translations():
            return
        from .segmentation import preview_translations
        project, path, cfg = copy.deepcopy(self.project), self.project_path, copy.deepcopy(self.cfg)
        segment = project['segments'][row]
        sentence = dict(index=row, start=segment['start'], end=segment['end'], text=segment['source_text'])
        def action(emit):
            return preview_translations(path, digest(project), cfg, [sentence],
                context=[s['source_text'] for s in project['segments']],
                tts_session=self.tts_session, translation_session=self.translation_session)
        def complete(result):
            if not result or not result[0]['target_text']:
                return
            self.table.blockSignals(True)
            self.table.item(row,2).setText(result[0]['target_text'])
            self.table.blockSignals(False)
            self.dirty = True
            self.save_translations()
        self.start_task(action, complete, cancellable=True)

    def open_project_folder(self):
        if self.project is not None and self.project_path and self.project_path.is_file():
            self.open_local(project_root(self.project['input']))

    def clear_project(self):
        if self.task is not None or not self.video.text().strip():
            return
        video = Path(self.video.text()).resolve()
        root = project_root(video)
        if not root.is_dir():
            return
        if QMessageBox.question(self, '清空项目',
                f'删除该视频的全部项目资料（含各模型、音色版本）？\n{root}\n原视频和导出视频保留，删除后无法恢复。',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        self.stop_sentence_audio()
        for player in list(self.player_windows.values()):
            player.close()
        def action(emit):
            try:
                clear_video_project(video)
            except (OSError, VoxlateError) as exc:
                return str(exc)
        def complete(result):
            if result:
                self.set_status('项目未完全清空。')
                QMessageBox.warning(self, '清空未完成', str(result))
                if self.project_path and self.project_path.exists():
                    return
            self.project = self.project_hash = None
            self.project_path = self.default_project_path(video)
            self.dirty = False
            self.table.setRowCount(0)
            self.restore_voice_selection()
            self.logs.clear()
            self.install_logs.clear()
            self.notify('项目未完全清空，可重试。' if result else '项目已清空，可重新识别翻译。')
        self.start_task(action, complete, keep_tts=False, keep_translation=False, release_reason='清空项目')

    def translation_ready(self, sentence_ids=None):
        if not self.project or not self.project.get("segments"):
            return False
        settings = dict(self.cfg["translator"], source_lang=self.cfg.get("source_lang", "en"))
        if self.project.get("translation_config_key") not in (None, digest(settings)):
            return False
        segments = self.project["segments"]
        if sentence_ids is not None:
            segments = [s for s in segments if s['id'] in sentence_ids and s.get('enabled', True)]
            if len(segments) != len(set(sentence_ids)) or not segments:
                return False
        return (bool(self.project.get("translation_config_key")) or any(s.get("target_text", "").strip() for s in segments)) and all(
            s.get("target_text", "").strip() for s in segments if s.get("enabled", True))

    def refresh_export_state(self, rows=None):
        self.refresh_model_button()
        self.audio_track_box.setEnabled(self.task is None and len(self.audio_tracks) > 1)
        self.translate_button.setEnabled(self.task is None and bool((self.project or {}).get('segments')))
        self.one_click_button.setEnabled(self.task is None)
        self.role_manager_button.setEnabled(self.task is None and bool((self.project or {}).get('segments')))
        self.full_segmentation_button.setEnabled(self.task is None and bool((self.project or {}).get('prepared_audio_key')))
        for row in (range(self.table.rowCount()) if rows is None else rows):
            combo = self.table.cellWidget(row, 5)
            if combo:
                combo.setEnabled(self.task is None)
        self.project_folder_button.setEnabled(self.project is not None and self.project_path is not None
                                              and self.project_path.is_file())
        self.clear_project_button.setEnabled(self.task is None and bool(self.video.text().strip())
                                             and project_root(self.video.text()).is_dir())
        translated = self.translation_ready()
        active = any(s.get("enabled", True) for s in (self.project or {}).get("segments", []))
        voice = not active or self.role_voice.isChecked() or self.individual_voice.isChecked() or self.reference_sentence.value() == 0 or self.reference_sentence.value() in {
            s['id'] for s in (self.project or {}).get('segments', [])}
        if self.role_voice.isChecked() and not (self.project or {}).get('roles_initialized'):
            voice = False
        self.reference_sentence.setEnabled(self.task is None and self.uniform_voice.isChecked()
                                           and bool((self.project or {}).get('segments')))
        recommendation = voice_selection(self.project)[1] if automatic_reference(self.project) else None
        self.reference_sentence.setToolTip(
            f'自动推荐第 {recommendation} 句，可填写其他句号。' if recommendation else
            '留空自动推荐；首次配音时分析，可填写句号指定。')
        self.dub_button.setEnabled(self.task is None and translated and voice and not self.dirty)
        self.dub_button.setToolTip('')
        ready = dubbing_ready(self.project, self.cfg)
        self.export_button.setEnabled(self.task is None and ready and not self.dirty)
        self.export_button.setToolTip('')
        self.refresh_sentence_buttons(rows)

    def make_sentence_buttons(self, row):
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(2, 0, 2, 0)
        layout.setSpacing(3)
        widget.dub = QPushButton('●')
        widget.dub.setStyleSheet('QPushButton {color:#dc4545; padding:0; font-size:18px;} QPushButton:disabled {color:#c9ced7;}')
        widget.play = QPushButton()
        widget.play.setIconSize(QSize(16, 16))
        widget.play_icons = (sentence_play_icon(), sentence_play_icon(stopped=True))
        widget.play.setIcon(widget.play_icons[0])
        widget.play.setAccessibleName('试听本句配音')
        widget.dub.setAccessibleName('生成本句配音')
        widget.play.setStyleSheet('QPushButton {padding:0; font-size:13px;}')
        for button in (widget.dub, widget.play):
            button.setFixedSize(30, 26)
            layout.addWidget(button)
        widget.dub.clicked.connect(lambda checked=False, r=row: self.dub_sentence(r))
        widget.dub.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        widget.dub.customContextMenuRequested.connect(lambda pos, r=row: self.choose_sentence_voice(r))
        widget.play.clicked.connect(lambda checked=False, r=row: self.play_sentence(r))
        self.table.setCellWidget(row, 3, widget)
        original = QWidget()
        original_layout = QHBoxLayout(original)
        original_layout.setContentsMargins(2, 0, 2, 0)
        original.play = QPushButton()
        original.play.setFixedSize(30, 26)
        original.play.setStyleSheet('QPushButton {padding:0;}')
        original.play.setIconSize(QSize(16, 16))
        original.play.setIcon(widget.play_icons[0])
        original.play.setAccessibleName('试听本句原人声')
        original.play.clicked.connect(lambda checked=False, r=row: self.play_sentence(r, original=True))
        original_layout.setSpacing(3)
        original_layout.addWidget(original.play)
        original.translate = action_button(action_icon('translate'), '翻译本句',
            lambda checked=False,r=row: self.translate_sentence(r))
        original_layout.addWidget(original.translate)
        self.table.setCellWidget(row, 4, original)
        toggle = action_button(action_icon('include'), '舍弃或选取本句',
            lambda checked=False,r=row: self.toggle_sentence(r,6))
        self.table.setCellWidget(row,6,action_cell(toggle))

    def original_sentence_audio(self, segment):
        clip = segment.get('source_audio')
        if audio_exists(clip):
            return clip, 0, None
        if not self.project_path:
            return None
        # Recognition already separated the vocals, even before any dubbing was generated.
        root = self.project_path.parent / 'separated'
        model = self.project.get('separator_model')
        candidates = [root/model/'original'/'vocals.wav'] if model else list(root.glob('*/original/vocals.wav'))
        if len(candidates) == 1 and audio_exists(candidates[0]):
            return str(candidates[0]), round(segment['start']*1000), round(segment['end']*1000)
        return None

    def sentence_audio(self, segment):
        aligned = segment.get('aligned_audio')
        if (segment.get('enabled', True) and segment.get('tts_text') is not None
                and aligned != segment.get('source_audio')
                and segment.get('timing_text') == segment.get('tts_text') and audio_exists(aligned)):
            return aligned
        return segment.get('tts_audio') if audio_exists(segment.get('tts_audio')) else None

    def refresh_sentence_buttons(self, rows=None):
        if not self.project:
            return
        contexts = voice_contexts(self.project,self.cfg)
        settings = dict(self.cfg['translator'], source_lang=self.cfg.get('source_lang', 'en'))
        config_matches = self.project.get('translation_config_key') in (None,digest(settings))
        segments = self.project.get('segments', [])
        for row in (range(len(segments)) if rows is None else rows):
            segment = segments[row]
            if row >= self.table.rowCount():
                break
            widget = self.table.cellWidget(row, 3)
            if widget is None:
                continue
            ready = sentence_ready(segment, contexts[segment['id']])
            widget.dub.setEnabled(self.task is None and not self.dirty and segment.get('enabled', True)
                                  and (not self.role_voice.isChecked() or self.project.get('roles_initialized', False))
                                  and config_matches and bool(segment.get('target_text','').strip()))
            reference_id = segment.get('voice_reference_sentence_id')
            widget.dub.setToolTip(('重新配音本句' if ready else '生成本句配音') + '；右击选择音色' +
                (f'（参考第 {reference_id} 句）' if reference_id is not None else ''))
            widget.play.setEnabled(bool(self.sentence_audio(segment)))
            widget.play.setToolTip('试听本句配音' if ready else '试听上次配音；当前修改尚未生成')
            original = self.table.cellWidget(row, 4)
            original.play.setEnabled(bool(self.original_sentence_audio(segment)))
            original.translate.setEnabled(self.task is None and not self.dirty)
            toggle = self.table.cellWidget(row,6).button
            toggle.setEnabled(self.task is None)
            enabled = segment.get('enabled',True)
            toggle.setIcon(action_icon('include' if enabled else 'exclude'))
            toggle.setToolTip('舍弃此句，保留原声' if enabled else '选取此句参与配音')
            original.play.setToolTip('试听本句原人声（分离后，无背景混合，原始语速）')
            self.refresh_sentence_play_icons(row)

    def refresh_sentence_play_icons(self, row):
        widget = self.table.cellWidget(row, 3) if 0 <= row < self.table.rowCount() else None
        if widget is None:
            return
        for column, original, label in ((3, False, '试听本句配音'), (4, True, '试听本句原人声')):
            controls = self.table.cellWidget(row, column)
            if controls is not None:
                active = self.playing_sentence == (row, original)
                controls.play.setIcon(widget.play_icons[1 if active else 0])
                controls.play.setAccessibleName('停止试听' if active else label)

    def stop_sentence_audio(self, release=True):
        previous = self.playing_sentence
        self.playing_sentence = None
        self.sentence_pending = False
        self.sentence_end = None
        if hasattr(self, 'sentence_timer'):
            self.sentence_timer.stop()
        if self.sentence_player:
            self.sentence_player.stop()
            if release:
                self.sentence_player.setSource(QUrl())
                self.sentence_source_key = None
        self.table.set_playback_progress()
        if previous:
            self.refresh_sentence_play_icons(previous[0])

    def sentence_playback_state_changed(self, state):
        if not self.playing_sentence:
            return
        if state == self.sentence_player.PlaybackState.StoppedState and not self.sentence_pending:
            self.stop_sentence_audio(release=False)
        else:
            self.refresh_sentence_play_icons(self.playing_sentence[0])

    def sentence_playback_error(self, error, text):
        self.stop_sentence_audio()
        self.notify('单句试听失败：' + text)

    def begin_sentence_audio(self, status):
        from PySide6.QtMultimedia import QMediaPlayer
        if self.sentence_pending and status in (QMediaPlayer.MediaStatus.LoadedMedia, QMediaPlayer.MediaStatus.BufferedMedia,
                                               QMediaPlayer.MediaStatus.EndOfMedia):
            self.sentence_pending = False
            self.sentence_player.setPosition(self.sentence_start)
            self.sentence_player.play()
            if self.playing_sentence:
                self.table.set_playback_progress(self.playing_sentence[0], 0.)
                self.sentence_timer.start()

    def check_sentence_end(self):
        if not self.playing_sentence or self.sentence_pending:
            return
        end = self.sentence_end if self.sentence_end is not None else self.sentence_player.duration()
        if end > self.sentence_start:
            position = self.sentence_player.position()
            if position >= end:
                self.stop_sentence_audio(release=False)
            else:
                self.table.set_playback_progress(self.playing_sentence[0],
                    (position-self.sentence_start)/(end-self.sentence_start))

    def play_sentence(self, row, original=False):
        if not self.project or not 0 <= row < len(self.project['segments']):
            return
        segment = self.project['segments'][row]
        selection = self.original_sentence_audio(segment) if original else (self.sentence_audio(segment), 0, None)
        if not selection or not selection[0]:
            return
        audio, start, end = selection
        if self.sentence_player is None:
            from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
            self.sentence_player = QMediaPlayer(self)
            self.sentence_output = QAudioOutput(self)
            self.sentence_output.setVolume(1.)
            self.sentence_player.setAudioOutput(self.sentence_output)
            self.sentence_player.playbackStateChanged.connect(self.sentence_playback_state_changed)
            self.sentence_player.mediaStatusChanged.connect(self.begin_sentence_audio)
            self.sentence_player.errorOccurred.connect(self.sentence_playback_error)
            self.sentence_timer = QTimer(self)
            self.sentence_timer.setInterval(30)
            self.sentence_timer.timeout.connect(self.check_sentence_end)
        if self.playing_sentence == (row, original) and (self.sentence_pending or self.sentence_player.playbackState() == self.sentence_player.PlaybackState.PlayingState):
            self.stop_sentence_audio(release=False)
        else:
            self.stop_sentence_audio(release=False)
            path = Path(audio).resolve()
            try:
                stat = path.stat()
            except OSError as exc:
                self.notify('单句试听失败：' + str(exc))
                return
            source_key = (str(path), stat.st_size, stat.st_mtime_ns)
            self.playing_sentence = (row, original)
            self.sentence_start, self.sentence_end = start, end
            self.sentence_pending = True
            if source_key != self.sentence_source_key:
                # A regenerated take can overwrite the same filename.
                if self.sentence_player.source() == QUrl.fromLocalFile(str(path)):
                    self.sentence_player.setSource(QUrl())
                self.sentence_source_key = source_key
                self.sentence_player.setSource(QUrl.fromLocalFile(str(path)))
            self.begin_sentence_audio(self.sentence_player.mediaStatus())
        self.refresh_sentence_play_icons(row)

    def dub_sentence(self, row):
        if self.task or not self.project or row >= len(self.project['segments']):
            return
        self.start_pipeline('dub', sentence_ids=[self.project['segments'][row]['id']], force_tts=True)

    def choose_sentence_voice(self, row):
        if self.task or not self.project or not 0 <= row < len(self.project['segments']):
            return
        if not self.table.cellWidget(row, 3).dub.isEnabled():
            return
        from .sentence_voice_dialog import SentenceVoiceDialog
        rows = {s['id']: i for i, s in enumerate(self.project['segments'])}
        dialog = SentenceVoiceDialog(self.project, row,
            lambda ident: self.play_sentence(rows[ident], original=True), self)
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        self.stop_sentence_audio()
        if accepted and self.save_translations(sentence_references={
                self.project['segments'][row]['id']: dialog.reference.currentData()}):
            self.dub_sentence(row)

    def change_source_language(self):
        old = self.cfg.get("source_lang", "en")
        language = self.source_language.currentData()
        if language == old:
            return
        if not self.confirm_discard():
            self.source_language.blockSignals(True)
            self.source_language.setCurrentIndex(self.source_language.findData(old))
            self.source_language.blockSignals(False)
            return
        self.tts_session.close('切换源语言')
        self.translation_session.close('切换源语言')
        self.cfg["source_lang"] = language
        try:
            self.save_settings()
        except (OSError, ValueError, VoxlateError) as exc:
            self.cfg["source_lang"] = old
            self.source_language.blockSignals(True)
            self.source_language.setCurrentIndex(self.source_language.findData(old))
            self.source_language.blockSignals(False)
            QMessageBox.warning(self, "设置未保存", str(exc))
            return
        self.project = self.project_hash = None
        self.dirty = False
        self.table.setRowCount(0)
        self.table.horizontalHeaderItem(2).setText('译文')
        self.restore_voice_selection()
        self.project_path = self.default_project_path(Path(self.video.text())) if self.video.text().strip() else None
        self.refresh_export_state()
        if self.project_path and self.project_path.exists():
            self.load_project(self.project_path)

    def refresh_audio_tracks(self, video, preferred=0, *, fallback=True, known_count=0):
        video = Path(video).resolve()
        if self.audio_tracks_video != video or not self.audio_tracks:
            self.audio_tracks_video = video
            try:
                self.audio_tracks = read_audio_tracks(video, self.cfg)
            except VoxlateError as exc:
                self.audio_tracks = []
                self.set_status(str(exc))
        if fallback and self.audio_tracks and preferred >= len(self.audio_tracks):
            preferred = 0
        self.cfg['audio_track'] = preferred
        self.cfg['audio_track_count'] = len(self.audio_tracks) or known_count
        self.audio_track_box.blockSignals(True)
        self.audio_track_box.clear()
        for track in self.audio_tracks:
            label = track['label']
            self.audio_track_box.addItem(label[:100], track['index'])
            self.audio_track_box.setItemData(self.audio_track_box.count()-1, label, Qt.ItemDataRole.ToolTipRole)
        if not self.audio_tracks or preferred >= len(self.audio_tracks):
            self.audio_track_box.addItem(f'音轨 {preferred+1}（未读取）', preferred)
        self.audio_track_box.setCurrentIndex(self.audio_track_box.findData(preferred))
        self.audio_track_box.blockSignals(False)
        self.audio_track_box.setEnabled(self.task is None and len(self.audio_tracks) > 1)

    def change_audio_track(self):
        old = selected_track(self.cfg)
        track = self.audio_track_box.currentData()
        if track == old:
            return
        if self.task or track is None or not self.confirm_discard():
            self.audio_track_box.blockSignals(True)
            self.audio_track_box.setCurrentIndex(self.audio_track_box.findData(old))
            self.audio_track_box.blockSignals(False)
            return
        video = Path(self.video.text()).resolve()
        try:
            write_json(project_root(video)/'audio-selection.json', {'audio_track':track})
        except (OSError, ValueError) as exc:
            self.audio_track_box.blockSignals(True)
            self.audio_track_box.setCurrentIndex(self.audio_track_box.findData(old))
            self.audio_track_box.blockSignals(False)
            QMessageBox.warning(self, '音轨未切换', str(exc))
            return
        self.stop_sentence_audio()
        self.tts_session.close('切换音轨')
        self.translation_session.close('切换音轨')
        self.cfg['audio_track'] = track
        self.project = self.project_hash = None
        self.dirty = False
        self.table.setRowCount(0)
        self.segmentation_range = None
        self.table.button_range = None
        self.manual_segmentation_button.hide()
        self.restore_voice_selection()
        self.update_output(video)
        self.project_path = self.default_project_path(video)
        if self.project_path.exists():
            self.load_project(self.project_path)
        self.refresh_export_state()
        self.notify(f'已切换音轨 {track+1}；其他音轨的结果保留。')

    def video_selected(self, value):
        if not self.confirm_discard():
            self.video.setText(self.selected_video)
            return False
        self.stop_sentence_audio()
        self.tts_session.close('切换视频')
        self.translation_session.close('切换视频')
        video = Path(value).resolve()
        self.selected_video = str(video)
        self.video.setText(str(video))
        try:
            preferred = selected_track(read_json(project_root(video)/'audio-selection.json'))
        except (OSError, ValueError, TypeError, VoxlateError):
            preferred = 0
        self.refresh_audio_tracks(video, preferred)
        self.update_output(video)
        self.project = None
        self.project_hash = None
        self.dirty = False
        self.table.setRowCount(0)
        self.restore_voice_selection()
        self.project_path = self.default_project_path(video)
        if self.project_path.exists():
            self.load_project(self.project_path)
        self.refresh_voice_fields()
        self.tabs.setCurrentIndex(0)
        return True

    def update_output(self, video):
        suffix = track_suffix(self.cfg)
        self.output_path = Path(video).with_name(Path(video).stem + suffix + ".zh.mp4")
        self.play_output_button.setToolTip(str(self.output_path))

    def dropped_video(self, mime):
        if self.task or not mime.hasUrls() or len(mime.urls()) != 1:
            return None
        url = mime.urls()[0]
        if not url.isLocalFile():
            return None
        path = Path(url.toLocalFile())
        return path if path.is_file() and path.suffix.lower() in {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".ts", ".mpg", ".mpeg", ".wmv", ".flv"} else None

    def dragEnterEvent(self, event):
        if self.dropped_video(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        video = self.dropped_video(event.mimeData())
        if video and self.video_selected(video):
            event.acceptProposedAction()
        else:
            event.ignore()

    def new_project(self):
        if not self.confirm_discard():
            return
        self.tts_session.close('新建项目')
        self.translation_session.close('新建项目')
        if not self.video.text().strip():
            QMessageBox.information(self, "请选择视频", "先选择视频，再新建处理项目。")
            return
        video = Path(self.video.text()).resolve()
        self.project = None
        self.project_hash = None
        self.dirty = False
        self.table.setRowCount(0)
        self.restore_voice_selection()
        default = default_project_directory(video, self.cfg)/'project.json'
        self.project_path = default.parent.with_name(default.parent.name + "-" + uuid.uuid4().hex[:8]) / "project.json"
        self.refresh_export_state()

    def open_project(self):
        if not self.confirm_discard():
            return
        directory = self.project_path.parent if self.project_path else (
            Path(self.video.text()).resolve().parent if self.video.text().strip() else Path.cwd())
        path, _ = QFileDialog.getOpenFileName(self, "打开 voxlate 项目", str(directory), "项目 (project.json)")
        if path:
            self.load_project(Path(path))

    def load_project(self, path, project=None):
        self.segmentation_range = None
        self.table.button_range = None
        self.table.clearSelection()
        self.manual_segmentation_button.hide()
        self.stop_sentence_audio()
        if self.task is None and self.project_path != Path(path):
            self.tts_session.close('切换项目')
            self.translation_session.close('切换项目')
        try:
            project = read_json(path) if project is None else project
            original_hash = digest(project)
            if project.get("schema_version") != 1 or project.get("name") != "voxlate":
                raise VoxlateError("这不是支持的 voxlate 项目")
            language = project.get("source_lang", "en")
            if language not in ("en", "ja") or project.get("target_lang", "zh") != "zh":
                raise VoxlateError("仅支持英文或日文翻译成中文的项目")
            from .pipeline import validate_segments
            validate_segments(project["segments"], project["duration"])
            if project.get('voice_mode') == 'roles':
                ensure_roles(project)
                validate_roles(project, allow_legacy_names=True)
            recognition = project.get("recognition_model")
            if recognition not in ASR_MODELS and recognition not in (None, "asr_model", 'asr_combined'):
                raise VoxlateError("此项目使用了不支持的识别模型")
            project = relocate_saved_project(project, path, expected_hash=original_hash)
            original_hash = digest(project)
            combined = recognition == 'asr_combined'
            if combined and selected_asr_key(self.cfg) == 'asr_qwen_model':
                select_asr_model(self.cfg, 'asr_turbo_model', resource_root(self.cfg))
            if combined != self.cfg['asr'].get('combined', False):
                self.cfg['asr']['combined'] = combined
                self.save_settings()
                self.set_resource_controls_enabled(self.task is None)
            if recognition in ASR_MODELS and recognition != selected_asr_key(self.cfg):
                select_asr_model(self.cfg, recognition, resource_root(self.cfg))
                self.save_settings()
            self.refresh_resource_requirements()
            self.cfg["source_lang"] = language
            self.refresh_audio_tracks(project['input'], selected_track(project), fallback=False,
                                      known_count=project.get('audio_track_count', 0))
            self.source_language.blockSignals(True)
            self.source_language.setCurrentIndex(self.source_language.findData(language))
            self.source_language.blockSignals(False)
            self.table.horizontalHeaderItem(2).setText('译文')
            self.project = project
            self.project_path = Path(path)
            self.project_hash = original_hash
            self.video.setText(project["input"])
            self.selected_video = project["input"]
            self.update_output(Path(project["input"]))

            self.table.blockSignals(True)
            self.table.setRowCount(len(project["segments"]))
            for row, s in enumerate(project["segments"]):
                self.table.setVerticalHeaderItem(row, QTableWidgetItem(str(s["id"])))
                for column, text in enumerate((f"{self.timestamp(s['start'])} – {self.timestamp(s['end']).rsplit(':', 1)[-1]}", s["source_text"], s.get("target_text", ""))):
                    item = QTableWidgetItem(text)
                    if column != 2:
                        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                    if column == 0:
                        item.setData(Qt.ItemDataRole.UserRole, s.get("enabled", True))
                    self.table.setItem(row, column, item)
                self.table.setRowHeight(row, 36)
                self.make_sentence_buttons(row)
                self.update_sentence_style(row)
            self.dirty = False
            self.restore_voice_selection()
        except (OSError, ValueError, KeyError, TypeError, VoxlateError) as exc:
            QMessageBox.warning(self, "项目无法打开", str(exc))
        finally:
            self.table.blockSignals(False)
            self.refresh_voice_fields()

    def changed_translation(self, item):
        if item.column() == 2:
            self.update_sentence_style(item.row())
            self.dirty = True
            self.save_translations()

    @staticmethod
    def timestamp(seconds):
        whole = int(seconds)
        return f"{whole // 3600:02d}:{whole // 60 % 60:02d}:{whole % 60:02d}"

    @staticmethod
    def precise_timestamp(seconds):
        whole, milliseconds = divmod(round(seconds * 1000), 1000)
        return f"{whole // 3600:02d}:{whole // 60 % 60:02d}:{whole % 60:02d}.{milliseconds:03d}"

    def toggle_sentence(self, row, column):
        if self.task is not None or column != 6 or not self.project:
            return
        if self.table.selecting_text:
            return
        was_dirty = self.dirty
        item = self.table.item(row, 0)
        enabled = not item.data(Qt.ItemDataRole.UserRole)
        item.setData(Qt.ItemDataRole.UserRole, enabled)
        self.table.cellWidget(row,6).button.setIcon(action_icon('include' if enabled else 'exclude'))
        self.update_sentence_style(row)
        self.dirty = True
        self.save_translations(changed_rows=None if was_dirty else {row})

    def refresh_segmentation_selection(self):
        if not any(index.column() == 0 for index in self.table.selectedIndexes()):
            self.segmentation_range = None
            self.table.button_range = None
            self.manual_segmentation_button.hide()

    def select_segmentation_range(self, first, last):
        if self.task is None and self.project:
            self.segmentation_range = (first, last)
            self.table.show_range_button(first, last)

    def open_selected_segmentation(self):
        if self.segmentation_range:
            self.edit_segmentation(*self.segmentation_range)

    def open_full_segmentation(self):
        if self.project and self.project.get('prepared_audio_key'):
            self.edit_segmentation(0, len(self.project['segments'])-1, full=True)

    def edit_segmentation(self, first, last, *, full=False):
        if self.task is not None or not self.project or (not full and not 0 <= first <= last < len(self.project['segments'])):
            return
        if not self.save_translations():
            return
        from .segmentation import apply_segmentation, segmentation_needs_models, preview_translations
        from .paired_segments import PairedSegmentPlan
        from .segmentation_dialog import SegmentationDialog, cached_waveform, waveform_peaks
        from .app_settings import player_volume, save_player_volume
        path, cfg, project = self.project_path, copy.deepcopy(self.cfg), copy.deepcopy(self.project)
        expected = digest(project)
        draft = path.parent/'.temp'/('segmentation-full-draft.json' if full else 'segmentation-draft.json')
        blocks = None
        original_selected = True
        try:
            validate_project_directory(project['input'], path.parent)
            if not draft.resolve().is_relative_to(path.parent.resolve()):
                raise VoxlateError('项目临时目录指向项目外，请移除目录链接后重试。')
            if draft.is_file():
                saved = read_json(draft)
                if (saved.get('project_hash') == expected and saved.get('blocks') and (full or (
                        saved['blocks'][0]['start'] <= project['segments'][last]['end']
                        and saved['blocks'][-1]['end'] >= project['segments'][first]['start']))):
                    blocks = saved['blocks']
                    original_selected = saved.get('use_original', True)
            plan = PairedSegmentPlan(project, first, last, blocks, full=full)
        except (OSError, ValueError, KeyError, VoxlateError) as exc:
            QMessageBox.warning(self, '无法编辑分句', str(exc))
            return
        self.stop_sentence_audio()
        self.set_status('正在读取波形…')
        def prepare(emit):
            pipeline = VideoDubPipeline(cfg)
            pipeline.work, pipeline.project = path.parent, project
            original, vocals, _ = pipeline.cached_media()
            waves = [cached_waveform(track, path.parent, plan.start, min(plan.end,plan.start+30) if full else plan.end) for track in (original,vocals)]
            overview = [cached_waveform(track,path.parent,plan.start,plan.end,limit=20000) for track in (original,vocals)] if full else waves
            return original, vocals, waves, overview
        def prepared(value):
            def open_when_idle():
                if self.task is not None:
                    QTimer.singleShot(25, open_when_idle)
                    return
                if self.project_path != path or digest(self.project) != expected:
                    return
                original, vocals, waves, overview = value
                def load_waves(start, end):
                    if full:
                        return [waveform_peaks(track, start=start, end=end) for track in (original, vocals)]
                    return [cached_waveform(track, path.parent, start, end) for track in (original, vocals)]
                def translate_selection(sentences, context, use_original):
                    return preview_translations(path, expected, cfg, sentences, context=context,
                        use_original=use_original, tts_session=self.tts_session, translation_session=self.translation_session)
                dialog = SegmentationDialog(project, first, last, original, vocals, waves,
                    blocks=blocks, volume=player_volume(self.data_dir),
                    load_waves=load_waves, parent=self, full=full, overview_waves=overview,
                    translate_selection=translate_selection)
                dialog.source.setCurrentIndex(0 if original_selected else 1)
                dialog.volumeChanged.connect(lambda volume: save_player_volume(self.data_dir, volume))
                accepted = dialog.exec() == QDialog.DialogCode.Accepted
                self.refresh_model_button()
                if not accepted:
                    dialog.deleteLater()
                    return
                selected_blocks = dialog.export_blocks()
                needs_models = segmentation_needs_models(project, selected_blocks)
                use_original = dialog.source.currentIndex() == 0
                dialog.deleteLater()
                try:
                    write_json(draft, dict(project_hash=expected, blocks=selected_blocks, use_original=use_original))
                except OSError as exc:
                    QMessageBox.warning(self, '无法保存草稿', str(exc))
                    return
                def action(emit):
                    return apply_segmentation(path, expected, cfg, selected_blocks, use_original=use_original, full=full,
                        tts_session=self.tts_session, translation_session=self.translation_session)
                def complete(result):
                    self.load_project(path)
                    skipped = len(result.get('skipped', []))
                    self.set_status('分句未变化。' if result.get('unchanged') else result['message'])
                    if skipped:
                        message = f"更新 {result['updated']} 句，保留 {result['reused']} 句；{skipped} 段未识别到文字，已舍弃并保留原声。"
                        if result['reference_changed']:
                            message += '\n原参考句已变化，已改为自动选择。'
                        QMessageBox.information(self, '分句已更新', message)
                    elif result['reference_changed']:
                        QMessageBox.information(self, '请确认参考音色', '部分参考句被重新切分，已改为自动选择。请确认参考句；相关音色的配音可能需要重做。')
                self.set_status('正在识别翻译改动句…' if needs_models else '正在保存分句…')
                self.start_task(action, complete, cancellable=True, keep_tts=not needs_models,
                    release_reason='手动分句识别翻译，腾出显存')
            QTimer.singleShot(0, open_when_idle)
        self.start_task(prepare, prepared, cancellable=True, keep_tts=True)

    def update_sentence_style(self, row):
        previous = self.table.blockSignals(True)
        try:
            enabled = bool(self.table.item(row, 0).data(Qt.ItemDataRole.UserRole))
            segment = self.project["segments"][row] if self.project else None
            ratio = acceleration(dict(segment, enabled=enabled), self.table.item(row, 2).text()) if segment else None
            for column in range(self.table.columnCount()):
                item = self.table.item(row, column)
                if item is not None:
                    item.setForeground(QColor("#23324a" if enabled else "#a0a8b4"))
                    item.setToolTip('')
                    font = item.font()
                    font.setItalic(not enabled)
                    item.setFont(font)
                    if column == 0 and segment:
                        item.setToolTip(f"{self.precise_timestamp(segment['start'])} – {self.precise_timestamp(segment['end'])}")
                        if segment.get('recognition_warning'):
                            choice = '已手动选取配音。' if enabled else '自动保留原声，不参与配音。'
                            item.setToolTip(item.toolTip() + '\n' + segment['recognition_warning'] + '；' + choice)
                        elif segment.get('timing_fallback'):
                            item.setToolTip(item.toolTip() + '\n此段未能精确对齐，仅显示音频块范围；默认保留原声。')
                        elif segment.get('timing_uncertain'):
                            item.setToolTip(item.toolTip() + '\n此句对齐不确定，建议试听原声。')
                    if column == 2:
                        item.setData(TIMING_ROLE, ratio)
                        if ratio is not None and ratio > GRADIENT_START:
                            item.setToolTip(f"需 {ratio:.2f}× 加速：配音 {segment['generated_duration']:.2f} 秒 / 原句 {segment['end'] - segment['start']:.2f} 秒。")
        finally:
            self.table.blockSignals(previous)

    def save_translations(self, *, roles=None, assignments=None, roles_initialized=None, automatic_role_count=None, report_errors=True, changed_rows=None, sentence_references=None):
        if not self.project or not self.project_path:
            return True
        try:
            validate_project_directory(self.project['input'], self.project_path.parent)
            with project_lock(self.project_path.parent):
                current = read_json(self.project_path)
                if digest(current) != self.project_hash:
                    raise VoxlateError("项目已被其他进程修改。请先备份当前译文并重新打开项目，避免覆盖。")
                if changed_rows is not None:
                    # A standalone include/exclude click changes no voice or text settings.
                    for row in changed_rows:
                        current['segments'][row]['enabled'] = bool(self.table.item(row, 0).data(Qt.ItemDataRole.UserRole))
                else:
                    for row, s in enumerate(current["segments"]):
                        # Preserve the original measurement's text when editing legacy projects.
                        if "generated_duration" in s and "timing_text" not in s:
                            s["timing_text"] = s.get("target_text", "")
                        s["target_text"] = self.table.item(row, 2).text().strip()
                        s["enabled"] = bool(self.table.item(row, 0).data(Qt.ItemDataRole.UserRole))
                    current.update(voice_mode=self.selected_voice_mode(),
                        reference_sentence_id=self.reference_sentence.value() or None,
                        reference_auto_recommend=self.reference_sentence.value() == 0,
                        speaker_reference='', reference_range=None, auto_reference=True)
                    current.pop('reference_auto_longest', None)
                    if roles is not None:
                        current['roles'] = copy.deepcopy(roles)
                    if current['voice_mode'] == 'roles' or current.get('roles'):
                        ensure_roles(current)
                        for segment in current['segments']:
                            if assignments and segment['id'] in assignments:
                                segment.update(assignments[segment['id']])
                        validate_roles(current, allow_legacy_names=roles is None)
                    if roles_initialized is not None:
                        current['roles_initialized'] = roles_initialized
                    if automatic_role_count is not None:
                        apply_automatic_voice_mode(current, automatic_role_count)
                    if sentence_references is not None:
                        ids = {s['id'] for s in current['segments']}
                        if any(ident not in ids or (ref is not None and (type(ref) is not int or ref not in ids))
                               for ident, ref in sentence_references.items()):
                            raise VoxlateError('音色参考句已变化，请重新选择。')
                        for segment in current['segments']:
                            if segment['id'] in sentence_references:
                                reference_id = sentence_references[segment['id']]
                                if reference_id is None:
                                    segment.pop('voice_reference_sentence_id', None)
                                else:
                                    segment['voice_reference_sentence_id'] = reference_id
                    select_voice_version(current, self.cfg)
                write_json(self.project_path, current)
                self.project = current
                self.project_hash = digest(current)
            self.dirty = False
            for row in (range(len(self.project['segments'])) if changed_rows is None else changed_rows):
                self.update_sentence_style(row)
            self.refresh_export_state(changed_rows)
            return True
        except (OSError, ValueError, VoxlateError) as exc:
            if report_errors:
                QMessageBox.warning(self, "保存失败", str(exc))
            else:
                self.notify('退出时未能保存项目：' + str(exc))
            return False

    def start_pipeline(self, stop_after, *, sentence_ids=None, force_tts=False):
        stop_after = 'export' if stop_after is None else stop_after
        if self.task is not None:
            return
        if stop_after == 'dub' and self.role_voice.isChecked() and not (self.project or {}).get('roles_initialized'):
            QMessageBox.information(self, '请先确认角色', '请先在「角色管理…」中自动分组或手动分配角色。')
            return
        if stop_after == 'dub' and not self.translation_ready(sentence_ids):
            QMessageBox.information(self, "请先翻译", "先完成翻译，查看译文后再生成配音。")
            return
        if stop_after == 'translate' and not (self.project or {}).get('segments'):
            QMessageBox.information(self, '请先识别', '先识别对白，再翻译。')
            return
        if stop_after == 'export' and not dubbing_ready(self.project, self.cfg):
            QMessageBox.information(self, '请先生成配音', '先生成所有已启用句子的配音，再导出视频。')
            return
        video = Path(self.video.text().strip()).resolve()
        if not self.video.text().strip() or not video.is_file():
            QMessageBox.information(self, "请选择视频", "先选择本地英文或日文视频。")
            return
        if self.project and Path(self.project["input"]).resolve() != video:
            QMessageBox.warning(self, "输入与项目不匹配", "请使用「选择…」切换视频，或打开对应项目。")
            return
        if self.project is None and self.project_path and self.project_path.exists():
            existing = read_json(self.project_path)
            if Path(existing["input"]).resolve() != video:
                self.project_path = None
        force_translation = stop_after == 'translate'
        force_recognition = stop_after == 'recognize'
        segments = (self.project or {}).get('segments', [])
        active = [s for s in segments if s.get('enabled', True)
                  and (sentence_ids is None or s['id'] in sentence_ids)]
        message = None
        if stop_after == 'recognize' and (segments or (self.project or {}).get('manual_edits')):
            manual = bool((self.project or {}).get('manual_edits')) or any(s.get('manual_boundary') for s in segments)
            message = ('自动分句将覆盖当前手动分句，继续？' if manual else '重新识别整个视频？') + '\n分句、选弃和译文修改将被自动结果替换，需重新配音。\n原项目会自动备份。'
        elif stop_after == 'translate' and any(s.get('target_text', '').strip() for s in segments):
            message = '已有译文，重新翻译？\n包括已舍弃句子；保留原文、分句和选弃状态。\n已选句子译文变化后需重新配音，原项目会自动备份。'
        elif stop_after == 'dub' and sentence_ids is None and any(audio_exists(s.get('tts_audio')) for s in active):
            mode_name = {'uniform': '统一音色', 'individual': '逐句音色', 'roles': '分角色音色'}[self.selected_voice_mode()]
            message = f'已有{mode_name}配音，重新生成并覆盖？\n仅替换当前音色方案的已选句子，其他音色方案保留。'
            force_tts = True
        if message and QMessageBox.question(self, '确认覆盖', message,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        self.update_output(video)
        output = self.output_path.resolve()
        if output.exists() and stop_after in ('export', 'auto'):
            if QMessageBox.question(self, "替换导出文件", f"将替换已有导出文件：\n{output}\n是否继续？") != QMessageBox.StandardButton.Yes:
                return
        if not self.save_translations():
            return
        try:
            self.save_settings()
        except (OSError, ValueError, VoxlateError) as exc:
            QMessageBox.warning(self, "设置有误", str(exc))
            return
        if not self.project_path:
            self.project_path = self.default_project_path(video)
        try:
            validate_project_directory(video, self.project_path.parent)
        except VoxlateError as exc:
            QMessageBox.warning(self, '项目位置不正确', str(exc))
            return
        cfg = copy.deepcopy(self.cfg)
        mode = self.selected_voice_mode()
        reference_id = self.reference_sentence.value() or None
        work = self.project_path.parent
        separation_keys = {'ffmpeg', 'ffprobe', 'separator', 'separator_model'}
        recognition_keys = {'ffmpeg', 'ffprobe',
                            'qwen' if self.recognition_choice() == 'asr_qwen_model' else 'runtime',
                            *(key for key in ASR_MODELS if required_model(cfg, key))}
        translation_keys = {'runtime', 'llm_engine', *MODELS}
        dubbing_keys = {'ffmpeg', 'ffprobe', 'tts', 'tts_model'}
        needed = {'separate': separation_keys, 'recognize': recognition_keys, 'translate': translation_keys,
                  'dub': dubbing_keys, 'export': set()}.get(stop_after)
        if stop_after == 'auto' and segments:
            needed = {'ffmpeg', 'ffprobe'}
            if active and not self.translation_ready():
                needed |= translation_keys
            if active and (not dubbing_ready(self.project, cfg) or
                           mode == 'roles' and not self.project.get('roles_initialized')):
                needed |= dubbing_keys

        cached_resources = copy.deepcopy(self.resources)

        def action(emit):
            emit("处理前检查资源…")
            results = [] if stop_after == 'export' else check_resources(cfg, self.config_path,
                keys=needed, cached=cached_resources, quick=True)
            missing = [r for r in results if not r.ready and r.required and (needed is None or r.key in needed)]
            if missing:
                return {"resources": results}
            result = VideoDubPipeline(cfg, tts_session=self.tts_session, translation_session=self.translation_session).process(video, output, work, None, None if stop_after in ('export', 'auto') else stop_after,
                require_translated=stop_after == 'dub', auto_reference=True, voice_mode=mode, reference_sentence_id=reference_id, export_only=stop_after == 'export',
                sentence_ids=sentence_ids, force_tts=force_tts, force_translation=force_translation,
                force_recognition=force_recognition, recognition_only=stop_after == 'recognize',
                translate_only=stop_after == 'translate', auto_export=stop_after == 'auto')
            return {"path": str(result), "checked_resources": results}

        def complete(result):
            if "resources" in result:
                self.resources_checked(result["resources"])
                return
            if result.get('checked_resources'):
                self.resources = result['checked_resources']
                self.refresh_resource_requirements()
            self.load_project(self.project_path)
            self.set_status({'separate':'分离完成，可以识别人声。', 'recognize':'识别完成，可检查分句后翻译。', 'translate':'翻译完成，可修改译文后生成配音。', 'dub':'配音完成，可逐句试听后导出。', 'export':'视频已导出，可以打开播放。', 'auto':'视频已导出，可以打开播放。'}[stop_after])
            self.notify("完成：" + result["path"])
            if stop_after == 'translate' and self.role_voice.isChecked() and not self.project.get('roles_initialized'):
                def group_when_idle():
                    if self.project_path != work/'project.json' or not self.role_voice.isChecked():
                        return
                    if self.task is not None:
                        QTimer.singleShot(50, group_when_idle)
                    else:
                        self.auto_assign_roles()
                QTimer.singleShot(0, group_when_idle)

        self.stop_sentence_audio()
        if stop_after in ('export', 'auto'):
            # Release the old export's file handle before Windows replaces it.
            self.close_output_players(output)
        self.start_task(action, complete, cancellable=True, keep_tts=stop_after in ('dub', 'export', 'auto'))

    def cancel_task(self):
        if self.task:
            if self.install_started is not None:
                self.install_detail = "正在停止"
            self.task.cancel.set()
            self.cancel_button.setEnabled(False)
            self.install_cancel_button.setEnabled(False)
            self.notify("正在停止当前步骤并保留已完成结果…")

    def closeEvent(self, event):
        if self.task:
            QMessageBox.information(self, "任务仍在运行", "请先停止处理，或等待资源检查结束，再关闭窗口。")
            event.ignore()
        else:
            if self.dirty and not self.save_translations(report_errors=False):
                # A broken path must not trap the user in the application. Keep a
                # recovery copy beside the project if that directory still exists.
                try:
                    directory = self.project_path.parent.resolve()
                    root = next((p for p in (directory, *directory.parents)
                                 if p.name == Path(self.project['input']).name + '.voxlate'), None)
                    if root is not None and directory.is_dir():
                        snapshot = copy.deepcopy(self.project)
                        for row, segment in enumerate(snapshot['segments']):
                            segment['target_text'] = self.table.item(row, 2).text().strip()
                            segment['enabled'] = bool(self.table.item(row, 0).data(Qt.ItemDataRole.UserRole))
                        recovery = directory/'.temp'/('unsaved-edits-' + uuid.uuid4().hex + '.json')
                        if recovery.resolve().is_relative_to(root):
                            write_json(recovery, snapshot)
                except (OSError, ValueError, KeyError, TypeError, AttributeError):
                    pass
            self.tts_session.close('关闭程序')
            self.translation_session.close('关闭程序')
            self.stop_sentence_audio()
            for player in list(self.player_windows.values()):
                player.close()
            event.accept()


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--smoke-test", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--player-smoke-video", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if os.name == "nt":
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("voxlate.desktop")
    app = QApplication(sys.argv[:1])
    from .runtime import bundle_root
    app.setWindowIcon(QIcon(str(bundle_root() / "assets/voxlate.ico")))
    # The offscreen Windows plugin does not enumerate system fonts reliably.
    # Register local fonts for headless QA as well as stripped-down Windows installs.
    if os.name == "nt":
        fonts = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
        for name in ("segoeui.ttf", "msyh.ttc", "msyhbd.ttc"):
            if (fonts / name).is_file():
                QFontDatabase.addApplicationFont(str(fonts / name))
    app.setApplicationName("voxlate")
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    try:
        window = MainWindow(args.data_dir, auto_check=not args.smoke_test)
    except Exception as exc:
        if args.smoke_test:
            write_json(args.smoke_test, {"error": repr(exc)})
            return 1
        QMessageBox.critical(None, "voxlate 无法启动", f"无法读取或创建配置文件。请检查文件夹权限。\n\n{exc}")
        return 1
    window.show()
    if args.smoke_test:
        import ssl
        def finish(results):
            window.resources_checked(results)
            def export():
                window.grab().save(str(args.smoke_test.with_suffix(".png")))
                window.tabs.setCurrentIndex(0)
                window.grab().save(str(args.smoke_test.with_name(args.smoke_test.stem + "-dubbing.png")))
                write_json(args.smoke_test, {"frozen": bool(getattr(sys, "frozen", False)),
                           "window_icon": not window.windowIcon().isNull(),
                           "title": window.windowTitle(), "rows": len(results),
                           "missing": [r.key for r in results if not r.ready],
                           "checks": {r.key: r.detail for r in results},
                           "tls": ssl.OPENSSL_VERSION,
                           "install_button": window.install_all_button.text(),
                           "resource_groups": [window.resource_tree.topLevelItem(i).text(0) for i in range(window.resource_tree.topLevelItemCount())],
                           "program_configuration_hidden": "ffmpeg" not in window.resource_nodes,
                           "columns": [window.resource_tree.headerItem().text(i) for i in range(6)],
                           "tab": window.tabs.tabText(1),
                           "try_mirrors": window.try_mirrors.isChecked(),
                           "source_languages": [window.source_language.itemData(i) for i in range(window.source_language.count())],
                           "has_guidance": bool(window.install_logs.toPlainText())})
                if args.player_smoke_video:
                    from .player import VideoPlayer
                    player = VideoPlayer(args.player_smoke_video, window, [1, 4])
                    player.show()
                    player.player.play()
                    started = time.monotonic()
                    timer = QTimer(player)
                    def check_player():
                        if player.player.position() >= 500 or time.monotonic() - started >= 15:
                            timer.stop()
                            report = read_json(args.smoke_test)
                            report["player"] = dict(duration=player.player.duration(), position=player.player.position(), error=player.error.text())
                            player.player.pause()
                            player.player.setPosition(2000)
                            player.grab().save(str(args.smoke_test.with_name(args.smoke_test.stem + "-player.png")))
                            if player.player.duration() >= 4000:
                                player.apply_range()
                            else:
                                player.reject()
                            report["player"]["selected_range"] = player.selected_range
                            write_json(args.smoke_test, report)
                            app.quit()
                    timer.timeout.connect(check_player)
                    timer.start(100)
                else:
                    app.quit()
            QTimer.singleShot(250, export)
        window.start_task(lambda emit: check_resources(window.cfg, window.config_path), finish)
    return app.exec()
