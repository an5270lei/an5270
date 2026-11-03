"""设置窗口：聊天与生图分别配置，API Key 单独加密保存。

Key 只在保存时写入 ``secrets.dat``（DPAPI 加密），不进入 config.json。
"""

from __future__ import annotations

import time

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton,
    QSpinBox, QTabWidget, QVBoxLayout, QWidget,
)

from config.config_manager import ConfigManager, DEFAULT_CHAT_HEADERS
from core.chat_engine import ChatEngine, ChatError
from core.data_manager import DataError, MODE_CUSTOM, MODE_LABELS, MODES
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
        engine = None
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
        finally:
            if engine is not None:
                engine.close()


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

    def __init__(self, config: ConfigManager, parent=None, data=None):
        super().__init__(parent)
        self.config = config
        self.data = data
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
        if self.data is not None:
            self.tabs.addTab(self._build_storage_tab(), "存储")
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

    # ------------------------------------------------------------------ 存储
    def _build_storage_tab(self):
        page = QWidget()
        form = QFormLayout(page)
        form.setContentsMargins(4, 12, 4, 12)
        form.setSpacing(10)

        self.mode_combo = QComboBox()
        for mode in MODES:
            self.mode_combo.addItem(MODE_LABELS[mode], mode)
        self.mode_combo.setCurrentIndex(
            self.mode_combo.findData(self.data.mode)
        )

        self.data_root = QLineEdit(str(self.data.root))
        self.data_root.setReadOnly(True)

        self.browse_button = QPushButton("浏览…")
        self.browse_button.setObjectName("secondaryButton")
        self.browse_button.setFixedWidth(80)
        self.browse_button.clicked.connect(self._pick_custom_root)

        root_row = QHBoxLayout()
        root_row.setContentsMargins(0, 0, 0, 0)
        root_row.setSpacing(8)
        root_row.addWidget(self.data_root, 1)
        root_row.addWidget(self.browse_button)
        root_box = QWidget()
        root_box.setLayout(root_row)

        self.apply_mode_button = QPushButton("应用切换")
        self.apply_mode_button.setObjectName("secondaryButton")
        self.apply_mode_button.setFixedWidth(110)
        self.apply_mode_button.clicked.connect(self._apply_storage_mode)

        self.mode_result = QLabel("")
        self.mode_result.setObjectName("muted")
        self.mode_result.setWordWrap(True)
        mode_result_row = QHBoxLayout()
        mode_result_row.setContentsMargins(0, 0, 0, 0)
        mode_result_row.setSpacing(10)
        mode_result_row.addWidget(self.apply_mode_button)
        mode_result_row.addWidget(self.mode_result, 1)
        mode_result_box = QWidget()
        mode_result_box.setLayout(mode_result_row)

        self.backup_button = QPushButton("完整备份")
        self.backup_button.setObjectName("secondaryButton")
        self.backup_button.setFixedWidth(110)
        self.backup_button.clicked.connect(self._run_backup)

        self.restore_button = QPushButton("从备份恢复")
        self.restore_button.setObjectName("secondaryButton")
        self.restore_button.setFixedWidth(110)
        self.restore_button.clicked.connect(self._run_restore)

        backup_row = QHBoxLayout()
        backup_row.setContentsMargins(0, 0, 0, 0)
        backup_row.setSpacing(8)
        backup_row.addWidget(self.backup_button)
        backup_row.addWidget(self.restore_button)
        backup_row.addStretch()
        backup_box = QWidget()
        backup_box.setLayout(backup_row)

        self.storage_result = QLabel("")
        self.storage_result.setObjectName("muted")
        self.storage_result.setWordWrap(True)

        form.addRow("存储模式", self.mode_combo)
        form.addRow("数据目录", root_box)
        form.addRow("", mode_result_box)
        form.addRow("备份与恢复", backup_box)
        form.addRow("", self.storage_result)

        self._refresh_storage_state()
        return page

    def _refresh_storage_state(self):
        mode = self.mode_combo.currentData()
        self.browse_button.setEnabled(mode == MODE_CUSTOM)
        if mode == MODE_CUSTOM:
            if self.data.custom_root:
                self.data_root.setText(self.data.custom_root)
        else:
            target = self.data.anchor_dir / "data" if mode == "appdata" \
                else self.data.app_dir / "data"
            self.data_root.setText(str(target))

    def _pick_custom_root(self):
        current = self.data_root.text().strip() or self.data.root
        chosen = QFileDialog.getExistingDirectory(self, "选择数据目录", current)
        if chosen:
            self.data_root.setText(chosen)

    def _apply_storage_mode(self):
        mode = self.mode_combo.currentData()
        custom = self.data_root.text().strip() if mode == MODE_CUSTOM else None
        if mode == MODE_CUSTOM and not custom:
            self.mode_result.setText("请先选择数据目录")
            return

        try:
            target = self.data.plan_switch(mode, custom)
        except DataError as exc:
            self.mode_result.setText(str(exc))
            return

        answer = QMessageBox.question(
            self, "切换存储位置",
            f"把全部数据（数据库、图片、配置）复制到：\n{target}\n\n"
            f"复制完成并校验通过后才会切换，旧目录 {self.data.root} 原样保留。\n"
            "切换后需要重启程序。现在继续吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        force = False
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            report = self.data.switch(mode, custom)
        except DataError as exc:
            QApplication.restoreOverrideCursor()
            if "已有数据" not in str(exc):
                self.mode_result.setText(str(exc))
                return
            answer = QMessageBox.question(
                self, "目标目录已有数据",
                f"{exc}\n\n是否用当前数据覆盖它？（目标目录里原有的数据会被替换）",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            force = True
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            try:
                report = self.data.switch(mode, custom, force=True)
            except DataError as exc2:
                QApplication.restoreOverrideCursor()
                self.mode_result.setText(str(exc2))
                return
        QApplication.restoreOverrideCursor()

        self.mode_result.setText(
            f"切换成功 · 消息 {report['messages']} 条 · 图片 {report['images_total']} 张 · 重启后生效"
        )
        self.data_root.setText(str(target))

    def _run_backup(self):
        default_zip = self.data.root / f"AIStudio_backup_{time.strftime('%Y%m%d_%H%M%S')}.zip"
        zip_path, _ = QFileDialog.getSaveFileName(
            self, "保存备份", str(default_zip), "AI Studio 备份 (*.zip)"
        )
        if not zip_path:
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            report = self.data.backup(zip_path)
        except DataError as exc:
            QApplication.restoreOverrideCursor()
            self.storage_result.setText(str(exc))
            return
        QApplication.restoreOverrideCursor()
        self.storage_result.setText(
            f"备份完成 · 消息 {report['messages']} 条 · 图片 {report['image_files']} 张 · {report['size_mb']} MB"
        )

    def _run_restore(self):
        zip_path, _ = QFileDialog.getOpenFileName(
            self, "选择备份文件", "", "AI Studio 备份 (*.zip)"
        )
        if not zip_path:
            return
        answer = QMessageBox.question(
            self, "从备份恢复",
            "恢复会替换当前的对话记录和图片（当前数据会改名保留为 .bak）。\n现在继续吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            report = self.data.restore(zip_path)
        except DataError as exc:
            QApplication.restoreOverrideCursor()
            self.storage_result.setText(str(exc))
            return
        QApplication.restoreOverrideCursor()
        self.storage_result.setText(
            f"恢复完成 · 消息 {report['messages']} 条 · 图片 {report['image_files']} 张 · 重启后生效"
        )

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
