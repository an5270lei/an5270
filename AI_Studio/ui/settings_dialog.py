"""设置窗口：聊天与生图分别配置，API Key 单独加密保存。

Key 只在保存时写入 ``secrets.dat``（DPAPI 加密），不进入 config.json。
"""

from __future__ import annotations

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QSpinBox, QTabWidget, QVBoxLayout, QWidget,
)

from config.config_manager import ConfigManager, DEFAULT_CHAT_HEADERS
from core.chat_engine import ChatEngine, ChatError
from modules.image_generator import (
    IMAGE_MODEL_OPTIONS, SIZE_PRESETS, ImageError, ImageGenerator,
)
from ui.model_combo import current_model_id, fill_model_combo


class ProbeWorker(QThread):
    """在后台真发一次请求，避免测试连接时把界面卡住。"""

    done = Signal(bool, str)

    def __init__(self, base_url, api_key, headers, timeout, model, parent=None):
        super().__init__(parent)
        self.base_url = base_url
        self.api_key = api_key
        self.headers = headers
        self.timeout = timeout
        self.model = model

    def run(self):
        try:
            engine = ChatEngine(
                base_url=self.base_url,
                api_key=self.api_key,
                extra_headers=self.headers,
                timeout=self.timeout,
            )
            ok, message = engine.test_connection(self.model)
            self.done.emit(ok, message)
        except ChatError as exc:
            self.done.emit(False, exc.message)
        except Exception as exc:  # noqa: BLE001
            self.done.emit(False, f"测试失败：{exc}")


class ImageProbeWorker(QThread):
    """真出一张图（要花钱、约 30-60 秒），用于验证生图 Key 和分组。"""

    done = Signal(bool, str)

    def __init__(self, base_url, api_key, headers, timeout, model, size, parent=None):
        super().__init__(parent)
        self.base_url = base_url
        self.api_key = api_key
        self.headers = headers
        self.timeout = timeout
        self.model = model
        self.size = size

    def run(self):
        generator = None
        try:
            generator = ImageGenerator(
                base_url=self.base_url,
                api_key=self.api_key,
                extra_headers=self.headers,
                timeout=self.timeout,
            )
            result = generator.generate(
                prompt="一只橘猫坐在窗台上，写实摄影风格", size=self.size, model=self.model
            )
            self.done.emit(
                True,
                f"出图成功 · {result.width}×{result.height} · {result.elapsed:.0f}s · {result.path.name}",
            )
        except ImageError as exc:
            self.done.emit(False, exc.message)
        except Exception as exc:  # noqa: BLE001
            self.done.emit(False, f"测试失败：{exc}")
        finally:
            if generator is not None:
                generator.close()


class SettingsDialog(QDialog):
    settings_saved = Signal()

    def __init__(self, config: ConfigManager, parent=None):
        super().__init__(parent)
        self.config = config
        self._probe = None
        self._image_probe = None

        self.setWindowTitle("AI Studio 设置")
        self.setMinimumWidth(560)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(12)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_chat_tab(), "聊天")
        self.tabs.addTab(self._build_image_tab(), "生图")
        layout.addWidget(self.tabs)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._load_values()

    # ------------------------------------------------------------------ 聊天
    def _build_chat_tab(self):
        page = QWidget()
        form = QFormLayout(page)
        form.setContentsMargins(4, 12, 4, 12)
        form.setSpacing(10)

        self.chat_base = QLineEdit()
        self.chat_base.setPlaceholderText("https://api.example.com/v1")

        self.header_name = QLineEdit()
        self.header_name.setPlaceholderText("x-openai-actor-authorization")
        self.header_value = QLineEdit()
        self.header_value.setPlaceholderText("local-image-extension")
        header_row = QHBoxLayout()
        header_row.setContentsMargins(0, 0, 0, 0)
        header_row.setSpacing(8)
        header_row.addWidget(self.header_name, 1)
        header_row.addWidget(self.header_value, 1)
        header_box = QWidget()
        header_box.setLayout(header_row)

        self.chat_model = QComboBox()
        fill_model_combo(self.chat_model, self.config.default_model)

        self.chat_timeout = QSpinBox()
        self.chat_timeout.setRange(15, 600)
        self.chat_timeout.setSuffix(" 秒")
        self.chat_timeout.setFixedWidth(120)
        self.chat_timeout.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)

        self.context_window = QSpinBox()
        self.context_window.setRange(2, 50)
        self.context_window.setSuffix(" 条")
        self.context_window.setFixedWidth(120)
        self.context_window.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)

        self.chat_key = QLineEdit()
        self.chat_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.show_key = QCheckBox("显示")
        self.show_key.toggled.connect(
            lambda checked: self.chat_key.setEchoMode(
                QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password
            )
        )
        key_row = QHBoxLayout()
        key_row.setContentsMargins(0, 0, 0, 0)
        key_row.setSpacing(8)
        key_row.addWidget(self.chat_key, 1)
        key_row.addWidget(self.show_key)
        key_box = QWidget()
        key_box.setLayout(key_row)

        self.test_button = QPushButton("测试连接")
        self.test_button.setObjectName("secondaryButton")
        self.test_button.setFixedWidth(110)
        self.test_button.clicked.connect(self._test_connection)
        self.test_result = QLabel("")
        self.test_result.setObjectName("muted")
        self.test_result.setWordWrap(True)
        test_row = QHBoxLayout()
        test_row.setContentsMargins(0, 0, 0, 0)
        test_row.setSpacing(10)
        test_row.addWidget(self.test_button)
        test_row.addWidget(self.test_result, 1)
        test_box = QWidget()
        test_box.setLayout(test_row)

        self.auto_image = QCheckBox("允许 AI 自动调用生图")
        self.auto_image.setChecked(True)

        self.summary_enabled = QCheckBox("长对话自动压缩成摘要")
        self.summary_enabled.setChecked(True)

        form.addRow("聊天 API 地址", self.chat_base)
        form.addRow("额外请求头", header_box)
        form.addRow("默认模型", self.chat_model)
        form.addRow("读取超时", self.chat_timeout)
        form.addRow("上下文窗口", self.context_window)
        form.addRow("生图权限", self.auto_image)
        form.addRow("历史摘要", self.summary_enabled)
        form.addRow("聊天 API Key", key_box)
        form.addRow("", test_box)
        return page

    # ------------------------------------------------------------------ 生图
    def _build_image_tab(self):
        page = QWidget()
        form = QFormLayout(page)
        form.setContentsMargins(4, 12, 4, 12)
        form.setSpacing(10)

        self.image_base = QLineEdit()
        self.image_base.setPlaceholderText("https://api.example.com/v1")

        self.image_model = QComboBox()
        self.image_model.setEditable(True)
        self.image_model.addItems(IMAGE_MODEL_OPTIONS)

        self.image_size = QComboBox()
        for label, value in SIZE_PRESETS:
            self.image_size.addItem(label, value)

        self.image_key = QLineEdit()
        self.image_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.show_image_key = QCheckBox("显示")
        self.show_image_key.toggled.connect(
            lambda checked: self.image_key.setEchoMode(
                QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password
            )
        )
        image_key_row = QHBoxLayout()
        image_key_row.setContentsMargins(0, 0, 0, 0)
        image_key_row.setSpacing(8)
        image_key_row.addWidget(self.image_key, 1)
        image_key_row.addWidget(self.show_image_key)
        image_key_box = QWidget()
        image_key_box.setLayout(image_key_row)

        self.image_test_button = QPushButton("测试出图")
        self.image_test_button.setObjectName("secondaryButton")
        self.image_test_button.setFixedWidth(110)
        self.image_test_button.clicked.connect(self._test_image)
        self.image_test_result = QLabel("")
        self.image_test_result.setObjectName("muted")
        self.image_test_result.setWordWrap(True)
        image_test_row = QHBoxLayout()
        image_test_row.setContentsMargins(0, 0, 0, 0)
        image_test_row.setSpacing(10)
        image_test_row.addWidget(self.image_test_button)
        image_test_row.addWidget(self.image_test_result, 1)
        image_test_box = QWidget()
        image_test_box.setLayout(image_test_row)

        form.addRow("生图 API 地址", self.image_base)
        form.addRow("生图模型", self.image_model)
        form.addRow("生成尺寸", self.image_size)
        form.addRow("生图 API Key", image_key_box)
        form.addRow("", image_test_box)
        return page

    # ------------------------------------------------------------------ 数据
    def _load_values(self):
        self.chat_base.setText(self.config.chat_base_url)
        headers = self.config.chat_headers or dict(DEFAULT_CHAT_HEADERS)
        if headers:
            name, value = next(iter(headers.items()))
            self.header_name.setText(name)
            self.header_value.setText(value)
        self.chat_timeout.setValue(int(self.config.chat_timeout))
        self.context_window.setValue(self.config.context_window)
        fill_model_combo(self.chat_model, self.config.default_model)

        if self.config.chat_api_key:
            self.chat_key.setPlaceholderText("已保存，留空则不修改")
        if self.config.image_api_key:
            self.image_key.setPlaceholderText("已保存，留空则不修改")

        self.image_base.setText(str(self.config.get("image.base_url", "")))
        model = self.config.image_model
        position = self.image_model.findText(model)
        if position >= 0:
            self.image_model.setCurrentIndex(position)
        else:
            self.image_model.setEditText(model)
        size_index = self.image_size.findData(self.config.image_size)
        self.image_size.setCurrentIndex(size_index if size_index >= 0 else 0)
        self.auto_image.setChecked(self.config.auto_image)
        self.summary_enabled.setChecked(self.config.summary_enabled)

    def _collect_headers(self):
        name = self.header_name.text().strip()
        value = self.header_value.text().strip()
        return {name: value} if name else {}

    def _test_connection(self):
        if self._probe is not None and self._probe.isRunning():
            return
        key = self.chat_key.text().strip() or self.config.chat_api_key
        if not key:
            self.test_result.setText("请先填写聊天 API Key")
            return
        self.test_button.setEnabled(False)
        self.test_result.setText("测试中…")
        self._probe = ProbeWorker(
            base_url=self.chat_base.text().strip(),
            api_key=key,
            headers=self._collect_headers(),
            timeout=float(self.chat_timeout.value()),
            model=current_model_id(self.chat_model),
            parent=self,
        )
        self._probe.done.connect(self._on_probe_done)
        self._probe.start()

    def _on_probe_done(self, ok, message):
        self.test_button.setEnabled(True)
        self.test_result.setText(("✅ " if ok else "❌ ") + message)
        self._probe = None

    def _test_image(self):
        if self._image_probe is not None and self._image_probe.isRunning():
            return
        key = self.image_key.text().strip() or self.config.image_api_key
        if not key:
            self.image_test_result.setText("请先填生图 API Key")
            return
        self.image_test_button.setEnabled(False)
        self.image_test_result.setText("出图中，约 30–60 秒…")
        self._image_probe = ImageProbeWorker(
            base_url=self.image_base.text().strip() or self.config.chat_base_url,
            api_key=key,
            headers=self._collect_headers(),
            timeout=float(self.config.image_timeout),
            model=self.image_model.currentText().strip(),
            size=self.image_size.currentData() or "1024x1024",
            parent=self,
        )
        self._image_probe.done.connect(self._on_image_probe_done)
        self._image_probe.start()

    def _on_image_probe_done(self, ok, message):
        self.image_test_button.setEnabled(True)
        self.image_test_result.setText(("✅ " if ok else "❌ ") + message)
        self._image_probe = None

    def _save(self):
        base_url = self.chat_base.text().strip()
        if not base_url:
            self.chat_base.setFocus()
            self.test_result.setText("聊天 API 地址不能为空")
            return

        self.config.set("chat.base_url", base_url)
        self.config.set("chat.extra_headers", self._collect_headers())
        self.config.set("chat.default_model", current_model_id(self.chat_model))
        self.config.set("chat.timeout", int(self.chat_timeout.value()))
        self.config.set("chat.context_window", int(self.context_window.value()))
        self.config.set("chat.auto_image", bool(self.auto_image.isChecked()))
        self.config.set("chat.summary_enabled", bool(self.summary_enabled.isChecked()))

        image_base = self.image_base.text().strip()
        if image_base:
            self.config.set("image.base_url", image_base)
        image_model = self.image_model.currentText().strip()
        if image_model:
            self.config.set("image.model", image_model)
        self.config.set("image.size", self.image_size.currentData() or "1024x1024")

        typed_key = self.chat_key.text().strip()
        if typed_key:
            self.config.chat_api_key = typed_key
        typed_image_key = self.image_key.text().strip()
        if typed_image_key:
            self.config.image_api_key = typed_image_key

        try:
            self.config.save()
        except Exception as exc:  # noqa: BLE001
            self.test_result.setText(f"保存失败：{exc}")
            return

        self.settings_saved.emit()
        self.accept()

    def closeEvent(self, event):  # noqa: N802 - Qt 命名
        for worker in (self._probe, self._image_probe):
            if worker is not None and worker.isRunning():
                try:
                    worker.done.disconnect()
                except RuntimeError:
                    pass
                worker.wait(2000)
        super().closeEvent(event)
