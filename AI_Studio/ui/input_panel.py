"""输入区。

- 生成中把「发送」切成「停止」，点一下就是请求中断；此时按 Enter 不会重发。
- 可以附带图片（最多 4 张），发送后交给后端走 images.edit 改图。
"""

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QComboBox, QFileDialog, QHBoxLayout, QLabel, QPushButton, QSizePolicy,
    QTextEdit, QVBoxLayout, QWidget,
)

MAX_ATTACHMENTS = 4
THUMB_SIZE = 52


class AttachmentChip(QLabel):
    """输入框上方的小缩略图，点一下移除。"""

    def __init__(self, path, on_remove, parent=None):
        super().__init__(parent)
        self.path = str(path)
        self._on_remove = on_remove
        self.setObjectName("attachmentChip")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("点击移除")

        pixmap = QPixmap(self.path)
        if pixmap.isNull():
            self.setText("?")
        else:
            self.setPixmap(
                pixmap.scaled(
                    THUMB_SIZE, THUMB_SIZE,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        self.setFixedHeight(THUMB_SIZE)

    def mousePressEvent(self, event):  # noqa: N802 - Qt 命名
        if event.button() == Qt.MouseButton.LeftButton:
            self._on_remove(self.path)
        super().mousePressEvent(event)


class InputPanel(QWidget):
    send_requested = Signal(str)
    stop_requested = Signal()
    size_changed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("inputPanel")
        self._streaming = False
        self._attachments: list[str] = []
        self._chips: list[AttachmentChip] = []
        self._size_visible = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(34, 10, 34, 22)
        outer.setSpacing(8)
        self._outer = outer

        # 附件缩略图条：没有附件时整条隐藏，不占高度
        self.attach_strip = QWidget()
        self.attach_layout = QHBoxLayout(self.attach_strip)
        self.attach_layout.setContentsMargins(2, 0, 2, 0)
        self.attach_layout.setSpacing(8)
        self.attach_layout.addStretch()
        self.attach_strip.setVisible(False)
        outer.addWidget(self.attach_strip)

        self.editor = QTextEdit()
        self.editor.setObjectName("messageInput")
        self.editor.setPlaceholderText("向 AI Studio 发送消息……")
        self.editor.setToolTip("Enter 发送 · Shift + Enter 换行")
        self.editor.setFixedHeight(104)
        outer.addWidget(self.editor)

        bottom = QHBoxLayout()
        self.attach_button = QPushButton("添加图片")
        self.attach_button.setObjectName("secondaryButton")
        self.attach_button.setFixedWidth(88)
        self.attach_button.setToolTip("选一张图，让 AI 按你的要求改它")

        # 生图尺寸：完整名称，改完立刻生效
        self.size_combo = QComboBox()
        self.size_combo.setObjectName("sizeCombo")
        self.size_combo.setToolTip("生图 / 改图的画布尺寸")
        self.size_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToContents
        )
        self.size_combo.setVisible(False)

        self.send_button = QPushButton("发送")
        self.send_button.setObjectName("sendButton")
        self.send_button.setFixedWidth(94)

        bottom.addWidget(self.attach_button)
        bottom.addSpacing(8)
        bottom.addWidget(self.size_combo)
        bottom.addStretch()
        bottom.addWidget(self.send_button)
        outer.addLayout(bottom)

        self.send_button.clicked.connect(self._on_button_clicked)
        self.attach_button.clicked.connect(self._pick_images)
        self.size_combo.currentIndexChanged.connect(self._on_size_changed)
        self.editor.installEventFilter(self)

    # ------------------------------------------------------------------ 尺寸
    def set_size_options(self, presets, current_value=None):
        """presets: [(完整名称, 接口值), ...]，下拉里显示完整名称。"""
        self.size_combo.blockSignals(True)
        self.size_combo.clear()
        for label, value in presets:
            self.size_combo.addItem(label, value)
        target = self.size_combo.findData(current_value) if current_value else -1
        self.size_combo.setCurrentIndex(target if target >= 0 else 0)
        self.size_combo.blockSignals(False)

        # 按最长的一项算宽度，避免任何一条被截断
        metrics = self.size_combo.fontMetrics()
        widest = max((metrics.horizontalAdvance(label) for label, _ in presets), default=0)
        self.size_combo.setMinimumWidth(widest + 44)

    def set_size_visible(self, visible):
        self._size_visible = bool(visible)
        self.size_combo.setVisible(self._size_visible)

    def current_size(self):
        return self.size_combo.currentData()

    def _on_size_changed(self, _index):
        value = self.size_combo.currentData()
        if value:
            self.size_changed.emit(str(value))

    # ------------------------------------------------------------------ 状态
    def set_streaming(self, streaming):
        self._streaming = bool(streaming)
        self.send_button.setText("停止" if self._streaming else "发送")
        self.send_button.setObjectName("stopButton" if self._streaming else "sendButton")
        # objectName 变了要重新套一遍样式表，否则颜色不刷新。
        self.send_button.style().unpolish(self.send_button)
        self.send_button.style().polish(self.send_button)

    @property
    def is_streaming(self):
        return self._streaming

    def refresh_style(self):
        """切换主题后重新套用样式（objectName 未变但颜色来自全局样式表）。"""
        for widget in (self.send_button, self.editor, self.attach_button, self.size_combo):
            widget.style().unpolish(widget)
            widget.style().polish(widget)

    def set_side_margin(self, margin):
        """与消息区共用同一条内容栏，避免输入框比气泡更宽。"""
        margin = max(20, int(margin))
        content = self._outer.contentsMargins()
        if content.left() != margin:
            self._outer.setContentsMargins(margin, 10, margin, 22)

    # ------------------------------------------------------------------ 附件
    def _pick_images(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "选择图片", "", "图片 (*.png *.jpg *.jpeg *.webp *.bmp)"
        )
        for path in paths:
            self.add_attachment(path)

    def add_attachment(self, path):
        if not path or path in self._attachments:
            return
        if len(self._attachments) >= MAX_ATTACHMENTS:
            return
        self._attachments.append(str(path))
        self._rebuild_strip()

    def remove_attachment(self, path):
        if path in self._attachments:
            self._attachments.remove(path)
            self._rebuild_strip()

    def take_attachments(self):
        """取走并清空当前附件列表。"""
        paths = list(self._attachments)
        self._attachments = []
        self._rebuild_strip()
        return paths

    def clear_attachments(self):
        self._attachments = []
        self._rebuild_strip()

    def attachments(self):
        return list(self._attachments)

    def _rebuild_strip(self):
        for chip in self._chips:
            self.attach_layout.removeWidget(chip)
            chip.setParent(None)
            chip.deleteLater()
        self._chips.clear()

        for index, path in enumerate(self._attachments):
            chip = AttachmentChip(path, self.remove_attachment, self.attach_strip)
            self.attach_layout.insertWidget(index, chip)
            self._chips.append(chip)

        self._refresh_strip_visibility()

    def _refresh_strip_visibility(self):
        self.attach_strip.setVisible(bool(self._attachments))

    # ------------------------------------------------------------------ 交互
    def _on_button_clicked(self):
        if self._streaming:
            self.stop_requested.emit()
        else:
            self._send()

    def eventFilter(self, obj, event):
        if obj is self.editor and event.type() == QEvent.Type.KeyPress:
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                    return False
                if not self._streaming:
                    self._send()
                return True
        return super().eventFilter(obj, event)

    def _send(self):
        text = self.editor.toPlainText().strip()
        if text or self._attachments:
            self.send_requested.emit(text)

    def clear_input(self):
        self.editor.clear()

    def set_placeholder(self, text):
        self.editor.setPlaceholderText(text)
