"""Small editor for names and fixed reference sentences."""
import copy
import uuid
import re
from pathlib import Path
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, QTableWidget,
    QLineEdit, QPushButton, QDialogButtonBox, QMessageBox, QHeaderView, QFileDialog)
from .common import VoxlateError
from .roles import MAX_ROLES, MAX_NAME, validate_roles
from .ui_controls import CenteredComboBox, action_button, action_cell, action_icon
from .player import playback_icon


class RoleDialog(QDialog):
    def __init__(self, project, play_reference, parent=None, *, export_reference=None, playing_reference=None):
        super().__init__(parent)
        self.project = copy.deepcopy(project)
        self.play_reference = play_reference
        self.export_reference = export_reference
        self.playing_reference = playing_reference
        self.export_worker = None
        self.previewed_row = self.previewed_id = self.playing_row = None
        self.roles = copy.deepcopy(project['roles'])
        self.auto_requested = False
        self.setWindowTitle('音色管理')
        self.resize(560, 460)
        self.setMinimumSize(500, 320)
        layout = QVBoxLayout(self)
        hint = QLabel('默认参考综合音色相似度、时长和人声占比选择，可手动更换。\n'
            f'棕色表示分组存疑，建议试听确认。角色名最多 {MAX_NAME} 字、不可重名；最多 {MAX_ROLES} 个角色。')
        hint.setWordWrap(True)
        layout.addWidget(hint)
        # Long videos share one list across all roles instead of duplicating thousands of items.
        self.reference_model = QStandardItemModel(self)
        automatic = QStandardItem('自动选本角色最长句')
        automatic.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        self.reference_model.appendRow(automatic)
        for segment in self.project['segments']:
            item = QStandardItem(f"第 {segment['id']} 句 · {segment['end']-segment['start']:.1f} 秒")
            item.setData(segment['id'], Qt.ItemDataRole.UserRole)
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.reference_model.appendRow(item)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(['角色名', '参考句', '', '', ''])
        self.table.verticalHeader().hide()
        header = self.table.horizontalHeader()
        header.setSectionsClickable(False)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for column in (0, 2, 3, 4):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(0, 128)
        for column in (2, 3, 4):
            self.table.setColumnWidth(column, 36)
        self.table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        layout.addWidget(self.table)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.hide()
        layout.addWidget(self.status)
        actions = QHBoxLayout()
        add = self.add_button = QPushButton('添加角色')
        add.clicked.connect(self.add_role)
        actions.addWidget(add)
        auto = self.auto_button = QPushButton('重新自动分组')
        auto.setToolTip('重新分析原人声，替换当前角色分配。')
        auto.clicked.connect(self.request_auto)
        actions.addWidget(auto)
        actions.addStretch()
        buttons = self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText('保存')
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('取消')
        buttons.accepted.connect(self.accept_roles)
        buttons.rejected.connect(self.reject)
        actions.addWidget(buttons)
        layout.addLayout(actions)
        self.render()
        self.play_timer = QTimer(self)
        self.play_timer.setInterval(150)
        self.play_timer.timeout.connect(self.refresh_playing)
        if playing_reference is not None:
            self.play_timer.start()

    def collect(self):
        for row, role in enumerate(self.roles):
            role['name'] = self.table.cellWidget(row, 0).text().strip()
            role['reference_sentence_id'] = self.table.cellWidget(row, 1).currentData()

    def render(self):
        self.previewed_row = self.previewed_id = self.playing_row = None
        self.add_button.setEnabled(len(self.roles) < MAX_ROLES)
        self.table.setRowCount(len(self.roles))
        for row, role in enumerate(self.roles):
            name = QLineEdit(role['name'])
            name.setMaxLength(MAX_NAME)
            name.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setCellWidget(row, 0, name)
            reference = CenteredComboBox()
            reference.setModel(self.reference_model)
            reference.setMaxVisibleItems(8)
            reference.setStyleSheet('QComboBox {combobox-popup: 0;}')
            reference.view().setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            reference.view().setMaximumHeight(240)
            reference.setCurrentIndex(max(0, reference.findData(role.get('reference_sentence_id'))))
            self.table.setCellWidget(row, 1, reference)
            play = action_button(playback_icon(False), '试听参考原人声', lambda checked=False, r=row: self.preview(r))
            play.setToolTip('试听参考原人声')
            self.table.setCellWidget(row, 2, action_cell(play))
            export = action_button(action_icon('export'), '导出音色', lambda checked=False, r=row: self.export_voice(r))
            export.setToolTip('导出参考音频（WAV，最多 15 秒）')
            export.setEnabled(self.export_reference is not None)
            self.table.setCellWidget(row, 3, action_cell(export))
            delete = action_button(action_icon('delete'), '删除角色', lambda checked=False, r=row: self.remove_role(r))
            delete.setEnabled(len(self.roles) > 1 and not any(s.get('role_id') == role['id'] for s in self.project['segments']))
            delete.setToolTip('删除角色' if delete.isEnabled() else '先将使用此角色的句子改选其他角色。')
            self.table.setCellWidget(row, 4, action_cell(delete))
            self.table.setRowHeight(row, 38)

    def preview(self, row):
        from .roles import reference_segment
        self.collect()
        candidate = dict(self.project, roles=self.roles)
        try:
            segment = reference_segment(candidate, self.roles[row]['id'])
        except VoxlateError as exc:
            QMessageBox.information(self, '参考句', str(exc))
            return
        self.play_reference(segment['id'])
        self.previewed_row, self.previewed_id = row, segment['id']
        self.refresh_playing()

    def refresh_playing(self):
        active = self.previewed_row if self.playing_reference and self.previewed_id is not None and self.playing_reference() == self.previewed_id else None
        if active == self.playing_row:
            return
        for row in (self.playing_row, active):
            if row is not None and row < self.table.rowCount():
                button = self.table.cellWidget(row, 2).button
                button.setIcon(playback_icon(row == active))
                button.setToolTip('停止试听' if row == active else '试听参考原人声')
        self.playing_row = active

    def export_voice(self, row):
        if self.export_worker is not None or self.export_reference is None:
            return
        from .roles import reference_segment
        self.collect()
        try:
            segment = reference_segment(dict(self.project, roles=self.roles), self.roles[row]['id'])
        except VoxlateError as exc:
            QMessageBox.information(self, '参考句', str(exc))
            return
        video = Path(self.project['input'])
        name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', self.roles[row]['name']).strip(' .') or '音色'
        destination, _ = QFileDialog.getSaveFileName(self, '导出音色',
            str(video.parent/(video.stem[:60]+'-'+name+'-音色.wav')), 'WAV 音频 (*.wav)')
        if not destination:
            return
        if not Path(destination).suffix:
            destination += '.wav'
        from .gui import TaskThread
        self.table.setEnabled(False)
        self.add_button.setEnabled(False)
        self.auto_button.setEnabled(False)
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setEnabled(False)
        self.status.setText('正在导出音色…')
        self.status.show()
        worker = self.export_worker = TaskThread(lambda emit: self.export_reference(segment, destination))
        worker.result.connect(lambda result: self.status.setText('已导出：'+Path(result).name))
        worker.failure.connect(self.status.setText)
        def finished():
            self.export_worker = None
            worker.deleteLater()
            self.table.setEnabled(True)
            self.add_button.setEnabled(len(self.roles) < MAX_ROLES)
            self.auto_button.setEnabled(True)
            self.buttons.button(QDialogButtonBox.StandardButton.Save).setEnabled(True)
            if hasattr(self, 'pending_result'):
                self.done(self.pending_result)
        worker.finished.connect(finished)
        worker.start()

    def done(self, result):
        if self.export_worker is not None:
            self.pending_result = result
            self.export_worker.cancel.set()
            return
        self.play_timer.stop()
        super().done(result)

    def closeEvent(self, event):
        if self.export_worker is not None:
            self.done(QDialog.DialogCode.Rejected)
            event.ignore()
            return
        super().closeEvent(event)

    def add_role(self):
        self.collect()
        if len(self.roles) >= MAX_ROLES:
            QMessageBox.information(self, '角色数量', f'最多 {MAX_ROLES} 个角色。')
            return
        names = {r['name'] for r in self.roles}
        name = next(f'角色{i}' for i in range(1, MAX_ROLES+2) if f'角色{i}' not in names)
        self.roles.append(dict(id='role_'+uuid.uuid4().hex[:12], name=name, reference_sentence_id=None))
        self.render()

    def remove_role(self, row):
        self.collect()
        if not 0 <= row < len(self.roles) or len(self.roles) <= 1:
            return
        if any(s.get('role_id') == self.roles[row]['id'] for s in self.project['segments']):
            return
        self.roles.pop(row)
        self.render()

    def accept_roles(self):
        self.collect()
        try:
            validate_roles(dict(self.project, roles=self.roles))
        except VoxlateError as exc:
            QMessageBox.information(self, '请调整角色', str(exc))
            return
        self.accept()

    def request_auto(self):
        if QMessageBox.question(self, '重新自动分组', '重新分组会替换角色名称和分配，已有配音保留。继续？',
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            self.auto_requested = True
            self.accept()
