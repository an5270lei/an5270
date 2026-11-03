from PySide6.QtCore import Signal, Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QLabel, QPushButton, QListWidget,
    QMenu, QMessageBox
)


class Sidebar(QWidget):
    new_chat_requested = Signal()
    conversation_selected = Signal(object)
    conversation_delete_requested = Signal(int)
    conversation_rename_requested = Signal(int)  # 只发信号，改名逻辑在主窗口

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("sidebar")
        self.setFixedWidth(270)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 20, 16, 16)
        layout.setSpacing(14)

        brand = QLabel("AI Studio")
        brand.setObjectName("brand")
        layout.addWidget(brand)

        self.new_button = QPushButton("+  新建对话")
        self.new_button.setObjectName("newChatButton")
        layout.addWidget(self.new_button)

        section = QLabel("历史对话")
        section.setObjectName("sectionLabel")
        layout.addWidget(section)

        self.conversation_list = QListWidget()
        self.conversation_list.setObjectName("conversationList")
        self.conversation_list.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu
        )
        layout.addWidget(self.conversation_list, 1)

        footer = QLabel("第一阶段 · 真实聊天 API")
        footer.setObjectName("muted")
        layout.addWidget(footer)

        self.new_button.clicked.connect(self.new_chat_requested.emit)
        self.conversation_list.itemClicked.connect(self.conversation_selected.emit)
        self.conversation_list.customContextMenuRequested.connect(
            self._show_context_menu
        )

    def add_conversation_item(self, item):
        self.conversation_list.addItem(item)

    def select_item(self, item):
        self.conversation_list.setCurrentItem(item)

    def clear_items(self):
        self.conversation_list.clear()

    def find_item_by_id(self, conversation_id):
        for index in range(self.conversation_list.count()):
            item = self.conversation_list.item(index)
            if item.data(Qt.ItemDataRole.UserRole) == conversation_id:
                return item
        return None

    def _show_context_menu(self, position):
        item = self.conversation_list.itemAt(position)
        if item is None:
            return
        conversation_id = item.data(Qt.ItemDataRole.UserRole)
        menu = QMenu(self)
        rename_action = menu.addAction("重命名")
        delete_action = menu.addAction("删除对话")
        chosen = menu.exec(self.conversation_list.mapToGlobal(position))
        if chosen == rename_action:
            self.conversation_rename_requested.emit(conversation_id)
        elif chosen == delete_action:
            answer = QMessageBox.question(
                self,
                "删除对话",
                f"确定删除“{item.text()}”及其全部消息吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No
            )
            if answer == QMessageBox.StandardButton.Yes:
                self.conversation_delete_requested.emit(conversation_id)
