"""聊天消息展示区。

支持流式增量：``begin_assistant_message()`` 先插入一个空气泡，之后每来一个
分块调用 ``append_text()``，避免整段重设导致闪烁和滚动条乱跳。

生成出来的图片作为缩略图挂在同一个气泡里，点一下用系统看图打开原图。
"""

from pathlib import Path

from PySide6.QtCore import QEvent, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QPixmap
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QScrollArea, QFrame, QLabel, QSizePolicy
)

CURSOR = "▍"
MAX_CONTENT_WIDTH = 780
MIN_SIDE_MARGIN = 70
MAX_THUMB_WIDTH = 360
MAX_THUMB_HEIGHT = 320


class ImageThumb(QLabel):
    """聊天里的图片缩略图，点击打开原图。"""

    def __init__(self, path, parent=None):
        super().__init__(parent)
        self._path = str(path)
        self.setObjectName("imageThumb")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("点击打开原图")

        pixmap = QPixmap(self._path)
        if pixmap.isNull():
            self.setText("")
            self.setVisible(False)
            return
        if pixmap.width() > MAX_THUMB_WIDTH or pixmap.height() > MAX_THUMB_HEIGHT:
            pixmap = pixmap.scaled(
                MAX_THUMB_WIDTH, MAX_THUMB_HEIGHT,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        self.setPixmap(pixmap)
        self.setFixedSize(pixmap.size())

    def mousePressEvent(self, event):  # noqa: N802 - Qt 命名
        if event.button() == Qt.MouseButton.LeftButton:
            QDesktopServices.openUrl(QUrl.fromLocalFile(self._path))
        super().mousePressEvent(event)


class MessageBubble(QFrame):
    def __init__(self, role, content="", model=None, parent=None):
        super().__init__(parent)
        self.role = role
        self._text = content or ""
        self._pending = False
        self._images = []

        self.setObjectName("userBubble" if role == "user" else "assistantBubble")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(6)
        self._layout = layout

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(8)

        label = QLabel("你" if role == "user" else "AI Studio")
        label.setObjectName("messageRole")
        header.addWidget(label)

        self.model_tag = None
        if role == "assistant" and model:
            self.model_tag = QLabel(model)
            self.model_tag.setObjectName("modelTag")
            header.addWidget(self.model_tag)

        header.addStretch()
        layout.addLayout(header)

        self.body = QLabel(self._text)
        self.body.setObjectName("messageText")
        self.body.setWordWrap(True)
        self.body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.body)
        self.body.setVisible(bool(self._text))

    # ---------------------------------------------------------------- 内容更新
    def text(self):
        return self._text

    def set_text(self, text):
        self._text = text or ""
        self._render()

    def append_text(self, piece):
        if not piece:
            return
        self._text += piece
        self._render()

    def set_pending(self, pending):
        self._pending = bool(pending)
        self._render()

    def _render(self):
        self.body.setText(self._text + (CURSOR if self._pending else ""))
        self.body.setVisible(bool(self._text) or self._pending)

    # ------------------------------------------------------------------ 图片
    def add_image(self, path):
        if not path or not Path(path).exists():
            return None
        thumb = ImageThumb(path, self)
        self._layout.addWidget(thumb)
        self._images.append(thumb)
        return thumb

    def images(self):
        return list(self._images)


class ChatPanel(QWidget):
    content_margin_changed = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("chatPanel")
        self._last_margin = -1

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 20, 0, 20)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setObjectName("chatScroll")

        self.content = QWidget()
        self.messages_layout = QVBoxLayout(self.content)
        self.messages_layout.setContentsMargins(0, 8, 0, 8)
        self.messages_layout.setSpacing(14)
        self.messages_layout.addStretch()

        self.scroll.setWidget(self.content)
        outer.addWidget(self.scroll)
        # 视口的宽度要等布局跑完才准，所以直接监听视口自身的 Resize。
        self.scroll.viewport().installEventFilter(self)
        self._apply_content_margins()

    # -------------------------------------------------------------- 内容宽度
    def eventFilter(self, obj, event):  # noqa: N802 - Qt 命名
        if obj is self.scroll.viewport() and event.type() == QEvent.Type.Resize:
            self._apply_content_margins()
        return super().eventFilter(obj, event)

    def side_margin(self):
        width = self.scroll.viewport().width()
        return max(MIN_SIDE_MARGIN, int((width - MAX_CONTENT_WIDTH) / 2))

    def _apply_content_margins(self):
        """窗口越宽，两侧留白越大，正文最多 MAX_CONTENT_WIDTH 宽并保持居中。"""
        margin = self.side_margin()
        self.messages_layout.setContentsMargins(margin, 8, margin, 8)
        if margin != self._last_margin:
            self._last_margin = margin
            self.content_margin_changed.emit(margin)

    # ------------------------------------------------------------------ 基础
    def clear_messages(self):
        while self.messages_layout.count():
            item = self.messages_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()
        self.messages_layout.addStretch()

    def _insert(self, bubble):
        # stretch 位于末尾，消息插入到 stretch 之前。
        index = max(0, self.messages_layout.count() - 1)
        self.messages_layout.insertWidget(index, bubble)
        self._scroll_to_bottom(force=True)
        return bubble

    def _near_bottom(self):
        bar = self.scroll.verticalScrollBar()
        return bar.value() >= bar.maximum() - 80

    def _scroll_to_bottom(self, force=False):
        if not force and not self._near_bottom():
            return
        QTimer.singleShot(0, self._do_scroll)

    def _do_scroll(self):
        bar = self.scroll.verticalScrollBar()
        bar.setValue(bar.maximum())

    def remove_message(self, bubble):
        if bubble is None:
            return
        self.messages_layout.removeWidget(bubble)
        bubble.setParent(None)
        bubble.deleteLater()

    # ------------------------------------------------------------------ 消息
    def add_user_message(self, content, images=None):
        bubble = MessageBubble("user", content)
        for path in images or []:
            bubble.add_image(path)
        return self._insert(bubble)

    def add_assistant_message(self, content, model=None, images=None):
        bubble = MessageBubble("assistant", content, model)
        for path in images or []:
            bubble.add_image(path)
        return self._insert(bubble)

    def begin_assistant_message(self, model=None):
        """插入一个空的 AI 气泡，进入流式追加状态。"""
        bubble = MessageBubble("assistant", "", model)
        bubble.set_pending(True)
        return self._insert(bubble)

    def notify_growth(self):
        """内容变高（比如图片插入）后，必要时跟随到底部。"""
        self._scroll_to_bottom()
