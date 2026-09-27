"""Small editor for names and fixed reference sentences."""
import copy
import uuid
from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, QTableWidget,
    QLineEdit, QPushButton, QDialogButtonBox, QMessageBox, QStyle, QHeaderView)
from .common import VoxlateError
from .roles import MAX_ROLES, MAX_NAME, validate_roles
from .ui_controls import CenteredComboBox


class RoleDialog(QDialog):
    def __init__(self, project, play_reference, parent=None):
        super().__init__(parent)
        self.project = copy.deepcopy(project)
        self.play_reference = play_reference
        self.roles = copy.deepcopy(project['roles'])
        self.auto_requested = False
        self.setWindowTitle('角色管理')
        self.resize(620, 460)
        self.setMinimumSize(540, 320)
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
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(['角色名', '参考句', '', ''])
        self.table.verticalHeader().hide()
        header = self.table.horizontalHeader()
        header.setSectionsClickable(False)
        for column in (0, 1):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Stretch)
        for column in (2, 3):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(2, 52)
        self.table.setColumnWidth(3, 68)
        self.table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        layout.addWidget(self.table)
        actions = QHBoxLayout()
        add = self.add_button = QPushButton('添加角色')
        add.clicked.connect(self.add_role)
        actions.addWidget(add)
        auto = QPushButton('重新自动分组')
        auto.setToolTip('重新分析原人声，替换当前角色分配。')
        auto.clicked.connect(self.request_auto)
        actions.addWidget(auto)
        actions.addStretch()
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText('保存')
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('取消')
        buttons.accepted.connect(self.accept_roles)
        buttons.rejected.connect(self.reject)
        actions.addWidget(buttons)
        layout.addLayout(actions)
        self.render()

    def collect(self):
        for row, role in enumerate(self.roles):
            role['name'] = self.table.cellWidget(row, 0).text().strip()
            role['reference_sentence_id'] = self.table.cellWidget(row, 1).currentData()

    def render(self):
        self.add_button.setEnabled(len(self.roles) < MAX_ROLES)
        self.table.setRowCount(len(self.roles))
        for row, role in enumerate(self.roles):
            name = QLineEdit(role['name'])
            name.setMaxLength(MAX_NAME)
            self.table.setCellWidget(row, 0, name)
            reference = CenteredComboBox()
            reference.setModel(self.reference_model)
            reference.setMaxVisibleItems(8)
            reference.setStyleSheet('QComboBox {combobox-popup: 0;}')
            reference.view().setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            reference.view().setMaximumHeight(240)
            reference.setCurrentIndex(max(0, reference.findData(role.get('reference_sentence_id'))))
            self.table.setCellWidget(row, 1, reference)
            play = QPushButton()
            play.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay))
            play.setIconSize(QSize(16, 16))
            play.setAccessibleName('试听参考原人声')
            play.setToolTip('试听参考原人声')
            play.clicked.connect(lambda checked=False, r=row: self.preview(r))
            self.table.setCellWidget(row, 2, play)
            delete = QPushButton('删除')
            delete.setEnabled(len(self.roles) > 1 and not any(s.get('role_id') == role['id'] for s in self.project['segments']))
            delete.setToolTip('先将使用此角色的句子改选其他角色。')
            delete.clicked.connect(lambda checked=False, r=row: self.remove_role(r))
            self.table.setCellWidget(row, 3, delete)
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
