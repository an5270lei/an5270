"""主窗口。

在原有布局上只做加法：顶栏多一个模型下拉框，其余控件位置、间距、
样式都保持原样。聊天从演示回复换成真实流式请求。

关于"停止生成"的安全边界：
- 线程只发信号不写库；
- 停止时先作废当前 generation_id（旧信号一律丢弃），再置位停止标志；
- 已生成的内容由主线程一次性写入，状态记为 stopped / error，
  因此不会留下半条残缺记录，也不会有残留数据包串到新对话里。
"""

from __future__ import annotations

import json
import shutil
import time
import uuid
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QImage
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QFrame, QHBoxLayout, QInputDialog, QLabel,
    QListWidgetItem, QMainWindow, QMessageBox, QPushButton, QVBoxLayout, QWidget,
)

from config.config_manager import ConfigManager
from core.chat_engine import ChatEngine, ChatError
from core.conversation_manager import ConversationManager
from core.context_manager import ContextManager
from core.data_manager import DataError, DataManager
from core.models import supports_tools, supports_vision
from core.tool_router import ToolRouter, build_tools, select_edit_base
from modules.image_generator import SIZE_PRESETS
from ui.chat_panel import ChatPanel
from ui.input_panel import InputPanel
from ui.model_combo import current_model_id, fill_model_combo
from ui.settings_dialog import SettingsDialog
from ui.sidebar import Sidebar
from ui.styles import THEME_LABELS, build_palette, build_stylesheet, normalize_theme
from workers.chat_worker import ChatWorker
from workers.summary_worker import SummaryWorker

IMAGE_DIR = Path(__file__).resolve().parents[1] / "data" / "images"  # 旧默认，仅作兜底


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("AI Studio")
        self.resize(1280, 820)
        self.setMinimumSize(980, 650)

        # 数据层先就绪：数据库、图片、配置的位置都由它决定
        self.data = DataManager()
        self._data_error = ""
        try:
            self.data.ensure_ready()
            self.data.adopt_legacy_config()
        except DataError as exc:
            self._data_error = str(exc)

        self.config = ConfigManager(self.data.config_dir)
        self.conversations = ConversationManager(self.data.db_path)
        self.context = ContextManager(
            self.conversations,
            window_size=self.config.context_window,
            system_prompt=self.config.system_prompt,
            summary_enabled=self.config.summary_enabled,
            summary_min_messages=self.config.summary_min_messages,
        )

        self.current_conversation_id = None
        self.worker = None
        self._retired_workers = []
        self.generation_id = 0
        self._active_conversation_id = None
        self._pending_model = None
        self._pending_text = ""
        self._bubble = None
        self._summary_workers = []
        self._summarizing = set()
        self._tool_name = ""
        self._generation_meta = {}

        self._theme = normalize_theme(self.config.get("ui.theme", "dark"))
        self._gen_started_at = 0.0
        self._tool_phase = False
        self._tool_started_at = 0.0
        self._pending_images = []
        self._status_token = 0
        self._status_timer = QTimer(self)
        self._status_timer.setInterval(1000)
        self._status_timer.timeout.connect(self._tick_generation_status)

        self._build_ui()
        self._apply_theme(self._theme)
        self._connect_signals()
        self._load_conversation_list()
        self._update_key_status()
        self._sync_image_controls()

        existing = self.conversations.list_conversations()
        if existing:
            self._open_conversation(existing[0]["id"])
        else:
            self._create_conversation()

    # ------------------------------------------------------------------ 构建
    def _build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        outer = QHBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.sidebar = Sidebar()
        outer.addWidget(self.sidebar)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)

        topbar = QFrame()
        topbar.setObjectName("topbar")
        top_layout = QHBoxLayout(topbar)
        top_layout.setContentsMargins(24, 12, 24, 12)

        self.page_title = QLabel("新对话")
        self.page_title.setObjectName("pageTitle")
        self.status_label = QLabel("")
        self.status_label.setObjectName("muted")

        self.model_combo = QComboBox()
        self.model_combo.setObjectName("modelCombo")
        self.model_combo.setMinimumWidth(210)
        fill_model_combo(self.model_combo, self.config.default_model)

        self.settings_button = QPushButton("设置")
        self.settings_button.setObjectName("secondaryButton")

        self.theme_button = QPushButton(THEME_LABELS[self._theme])
        self.theme_button.setObjectName("secondaryButton")
        self.theme_button.setToolTip("切换白天 / 黑夜模式")

        top_layout.addWidget(self.page_title)
        top_layout.addStretch()
        top_layout.addWidget(self.status_label)
        top_layout.addSpacing(14)
        top_layout.addWidget(self.model_combo)
        top_layout.addSpacing(10)
        top_layout.addWidget(self.theme_button)
        top_layout.addSpacing(8)
        top_layout.addWidget(self.settings_button)
        right_layout.addWidget(topbar)

        self.chat_panel = ChatPanel()
        right_layout.addWidget(self.chat_panel, 1)

        self.input_panel = InputPanel()
        right_layout.addWidget(self.input_panel)

        outer.addWidget(right, 1)

    def _connect_signals(self):
        self.sidebar.new_chat_requested.connect(self.new_conversation)
        self.sidebar.conversation_selected.connect(self._on_conversation_selected)
        self.sidebar.conversation_delete_requested.connect(self._delete_conversation)
        self.sidebar.conversation_rename_requested.connect(self._rename_conversation)
        self.input_panel.send_requested.connect(self._send_message)
        self.input_panel.stop_requested.connect(self._stop_generation)
        self.input_panel.size_changed.connect(self._on_image_size_changed)
        self.model_combo.currentIndexChanged.connect(self._on_model_changed)
        self.settings_button.clicked.connect(self._open_settings)
        self.theme_button.clicked.connect(self._toggle_theme)
        self.chat_panel.content_margin_changed.connect(self.input_panel.set_side_margin)
        self.input_panel.set_side_margin(self.chat_panel.side_margin())

    # ------------------------------------------------------------------ 会话
    def _load_conversation_list(self, select_id=None):
        self.sidebar.clear_items()
        for conversation in self.conversations.list_conversations():
            item = QListWidgetItem(conversation["title"])
            item.setData(Qt.ItemDataRole.UserRole, conversation["id"])
            self.sidebar.add_conversation_item(item)
        if select_id is not None:
            item = self.sidebar.find_item_by_id(select_id)
            if item:
                self.sidebar.select_item(item)

    def _create_conversation(self):
        conversation_id = self.conversations.create_conversation()
        self._load_conversation_list(select_id=conversation_id)
        self._open_conversation(conversation_id)
        return conversation_id

    def new_conversation(self):
        self._abort_generation()
        self._create_conversation()

    def _on_conversation_selected(self, item):
        conversation_id = item.data(Qt.ItemDataRole.UserRole)
        if conversation_id is None or conversation_id == self.current_conversation_id:
            return
        self._abort_generation()
        self._open_conversation(conversation_id)

    def _open_conversation(self, conversation_id):
        conversation = self.conversations.get_conversation(conversation_id)
        if not conversation:
            return
        self.current_conversation_id = conversation_id
        self.page_title.setText(conversation["title"])
        item = self.sidebar.find_item_by_id(conversation_id)
        if item:
            self.sidebar.select_item(item)

        self.chat_panel.clear_messages()
        messages = self.conversations.get_messages(conversation_id)
        if not messages:
            self.chat_panel.add_assistant_message(
                "你好！我是 AI Studio。你可以在这里开始新的对话。"
            )
        else:
            image_map = self.conversations.get_images(conversation_id)
            for message in messages:
                photos = [row["path"] for row in image_map.get(message["id"], [])]
                if message["role"] == "user":
                    self.chat_panel.add_user_message(message["content"], images=photos)
                elif message["role"] == "assistant":
                    self.chat_panel.add_assistant_message(
                        message["content"], model=message.get("model"), images=photos
                    )

    def _rename_conversation(self, conversation_id):
        conversation = self.conversations.get_conversation(conversation_id)
        if not conversation:
            return
        name, ok = QInputDialog.getText(
            self, "重命名对话", "新名称", text=conversation["title"]
        )
        name = name.strip()
        if not ok or not name or name == conversation["title"]:
            return
        self.conversations.update_title(conversation_id, name)
        item = self.sidebar.find_item_by_id(conversation_id)
        if item:
            item.setText(name)
        if conversation_id == self.current_conversation_id:
            self.page_title.setText(name)

    def _delete_conversation(self, conversation_id):
        if conversation_id == self.current_conversation_id:
            self._abort_generation()

        # 先把图片文件清掉，再删数据库记录，避免留下永远没人认领的孤儿图片
        image_map = self.conversations.get_images(conversation_id)
        for rows in image_map.values():
            for row in rows:
                self.data.remove_image(row["path"])

        self.conversations.delete_conversation(conversation_id)
        self.current_conversation_id = None
        self._load_conversation_list()

        remaining = self.conversations.list_conversations()
        if remaining:
            self._open_conversation(remaining[0]["id"])
        else:
            self._create_conversation()

    # ------------------------------------------------------------------ 发送
    def _send_message(self, message):
        message = (message or "").strip()
        attachments = self.input_panel.take_attachments()
        if not message and not attachments:
            return
        if self.current_conversation_id is None or self.worker is not None:
            return
        if not self._ensure_api_key():
            return

        conversation_id = self.current_conversation_id
        message_id = self.conversations.add_message(conversation_id, "user", message)

        uploaded_assets = []
        for source in attachments:
            stored = self._store_upload(source, message_id, conversation_id)
            if stored:
                uploaded_assets.append(stored)

        self.chat_panel.add_user_message(
            message, images=[item["path"] for item in uploaded_assets]
        )
        self.input_panel.clear_input()
        self._maybe_autotitle(conversation_id, message or "图片")
        self._start_generation(conversation_id, uploaded_assets)

    def _store_upload(self, source_path, message_id, conversation_id):
        """把用户选的图片复制进数据目录，标注角色并挂到这条用户消息上。

        本会话第一张上传图视为产品主体（product），之后的视为参考图（reference）。
        role_source='inferred' 表示这是系统推断，用户后续可以改。
        """
        source = Path(source_path)
        if not source.is_file():
            return None
        images_dir = self.data.images_dir
        images_dir.mkdir(parents=True, exist_ok=True)
        target = images_dir / f"{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}{source.suffix.lower() or '.png'}"
        try:
            shutil.copyfile(source, target)
        except OSError:
            return None

        role = "product" if self.conversations.count_uploads(conversation_id) == 0 else "reference"

        image = QImage(str(target))
        width, height = (image.width(), image.height()) if not image.isNull() else (0, 0)
        self.conversations.add_image(
            message_id, conversation_id, str(target),
            prompt="", model="", size="", width=width, height=height,
            kind="upload", role=role, role_source="inferred",
        )
        return {
            "path": str(target), "role": role, "kind": "upload",
            "message_id": message_id, "width": width, "height": height,
        }

    def _maybe_autotitle(self, conversation_id, message):
        conversation = self.conversations.get_conversation(conversation_id)
        if not conversation or conversation["title"] != "新对话":
            return
        title = message.replace("\n", " ").strip()
        if len(title) > 22:
            title = title[:22] + "…"
        self.conversations.update_title(conversation_id, title)
        self.page_title.setText(title)
        item = self.sidebar.find_item_by_id(conversation_id)
        if item:
            item.setText(title)

    def _start_generation(self, conversation_id, uploaded_assets=None):
        uploaded_assets = uploaded_assets or []
        model_id = current_model_id(self.model_combo)
        self.generation_id += 1
        current = self.generation_id

        # 登记这一轮属于哪个会话：即使用户中途切走，迟到的图片也知道该归谁
        self._generation_meta[current] = conversation_id

        self._active_conversation_id = conversation_id
        self._pending_model = model_id
        self._pending_text = ""
        self._pending_images = []
        self._tool_phase = False
        self._tool_name = ""
        self._bubble = self.chat_panel.begin_assistant_message(model_id)
        self._bubble.start_duration("生成中")
        self.input_panel.set_streaming(True)
        self._start_status_timer()

        messages = self.context.build_messages(
            conversation_id, vision=supports_vision(model_id)
        )
        try:
            engine = ChatEngine(
                base_url=self.config.chat_base_url,
                api_key=self.config.chat_api_key,
                extra_headers=self.config.chat_headers,
                timeout=self.config.chat_timeout,
                connect_timeout=self.config.chat_connect_timeout,
            )
        except ChatError as exc:
            self._discard_empty_reply()
            self._restore_idle()
            QMessageBox.warning(self, "无法发送", exc.message)
            return

        tools, router = self._build_tooling(model_id, uploaded_assets, conversation_id)
        worker = ChatWorker(
            engine, messages, model_id,
            tools=tools, router=router,
            attachments=[item["path"] for item in uploaded_assets],
            conversation_id=conversation_id,
        )
        worker.token.connect(lambda piece, gid=current: self._on_token(gid, piece))
        worker.notice.connect(lambda text, gid=current: self._on_notice(gid, text))
        worker.tool_started.connect(
            lambda name, prompt, size, gid=current: self._on_tool_started(gid, name, prompt, size)
        )
        worker.image_ready.connect(
            lambda path, prompt, model, size, w, h, elapsed, gid=current:
            self._on_image_ready(gid, path, prompt, model, size, w, h, elapsed)
        )
        worker.task_ready.connect(
            lambda cid, task_json, gid=current: self._on_task_ready(cid, task_json)
        )
        worker.completed.connect(
            lambda text, model, status, gid=current: self._on_completed(gid, text, model, status)
        )
        worker.failed.connect(
            lambda code, msg, partial, gid=current: self._on_failed(gid, code, msg, partial)
        )
        worker.finished.connect(lambda w=worker: self._on_worker_finished(w))

        worker.generation_id = current
        self.worker = worker
        self._retired_workers.append(worker)
        worker.start()

    def _build_tooling(self, model_id, uploaded_assets, conversation_id):
        """决定这一轮挂哪些工具，并按 5 级优先级选出编辑底图。"""
        if not self.config.auto_image:
            return None, None
        if not supports_tools(model_id):
            return None, None
        if not self.config.has_image_key:
            return None, None

        product_asset = self.conversations.latest_image_by_role(conversation_id, "product")
        generated_asset = self.conversations.latest_image_by_role(conversation_id, "generated")
        base, reason = select_edit_base(uploaded_assets, product_asset, generated_asset)

        edit_base_info = None
        reference_images: list[str] = []
        if base is not None:
            edit_base_info = {
                "type": base.get("role"),
                "source": "本轮上传" if base in uploaded_assets else "历史资产",
                "message_id": base.get("message_id"),
                "path": base.get("path"),
                "reason": reason,
            }
            # 底图是产品原图时，把最近一张成品作为风格参考一起传
            if base.get("role") == "product" and generated_asset:
                reference_images = [generated_asset["path"]]

        generated_count = sum(
            len(rows) for rows in self.conversations.get_images(conversation_id).values()
        )
        router = ToolRouter(
            base_url=self.config.image_base_url,
            api_key=self.config.image_api_key,
            extra_headers=self.config.image_headers,
            model=self.config.image_model,
            size=self.config.image_size,
            timeout=self.config.image_timeout,
            edit_base=base["path"] if base else None,
            edit_base_info=edit_base_info,
            reference_images=reference_images,
            conversation_id=conversation_id,
            generated_count=generated_count,
        )
        return build_tools(
            with_edit=base is not None, with_task=True
        ), router

    # ------------------------------------------------------------------ 流式
    def _on_token(self, generation_id, piece):
        if generation_id != self.generation_id or self._bubble is None:
            return
        self._pending_text += piece
        self._bubble.append_text(piece)

    def _on_notice(self, generation_id, text):
        if generation_id != self.generation_id:
            return
        self._flash_status(text, 3200)

    def _on_tool_started(self, generation_id, name, prompt, size):
        if generation_id != self.generation_id:
            return
        self._tool_phase = True
        self._tool_name = name
        self._tool_started_at = time.monotonic()
        if self._bubble is not None:
            self._bubble.set_duration_phase("改图中" if name == "edit_image" else "生成图片中")
        self._update_gen_status()

    def _on_image_ready(self, generation_id, path, prompt, model, size, width, height, elapsed):
        if generation_id != self.generation_id:
            # 迟到的图片：用户已经切走会话，但请求已经花出去了，
            # 直接落到原会话的历史里，不让钱白花。
            self._save_late_image(generation_id, path, prompt, model, size, width, height)
            return
        self._pending_images.append(
            {
                "path": path,
                "prompt": prompt,
                "model": model,
                "size": size,
                "width": width,
                "height": height,
            }
        )
        if self._bubble is not None:
            self._bubble.add_image(path)
            self.chat_panel.notify_growth()
        self._tool_phase = False
        self._flash_status(f"图片已生成 · {width}×{height} · {elapsed:.0f} 秒", 2500)

    def _on_task_ready(self, conversation_id, task_json):
        """模型通过 remember_task 汇报的任务进度，由主线程校验后落库。"""
        try:
            task = json.loads(task_json)
        except ValueError:
            return
        if not isinstance(task, dict):
            return
        self.conversations.set_task(conversation_id, task)
        if conversation_id == self.current_conversation_id:
            done = len(task.get("completed") or [])
            self._flash_status(f"任务进度已更新 · 已完成 {done} 项", 2500)

    def _save_late_image(self, generation_id, path, prompt, model, size, width, height):
        conversation_id = self._generation_meta.get(generation_id)
        if conversation_id is None:
            return
        try:
            message_id = self.conversations.add_message(
                conversation_id, "assistant", "", model=model, status="complete"
            )
            self.conversations.add_image(
                message_id, conversation_id, path,
                prompt=prompt, model=model, size=size,
                width=width, height=height,
            )
        except Exception:  # noqa: BLE001 - 落库失败不该影响正在进行的对话
            return

    def _on_completed(self, generation_id, text, model, status):
        if generation_id != self.generation_id:
            return
        final_text = text if text is not None else self._pending_text
        conversation_id = self._active_conversation_id

        if self._bubble is not None:
            self._bubble.stop_duration()
        self._save_reply(conversation_id, final_text, model, status)

        self._restore_idle()
        if status == "stopped":
            self._flash_status("已停止")
        else:
            self._update_key_status()
        if conversation_id is not None:
            self._load_conversation_list(select_id=conversation_id)
            self._maybe_summarize(conversation_id)

    def _save_reply(self, conversation_id, text, model, status):
        """统一落库：一条助手消息 + 它带的图片。没有内容也没图就撤掉空气泡。"""
        text = text or ""
        if not text.strip() and not self._pending_images:
            self._discard_empty_reply()
            return None
        if conversation_id is None:
            return None

        message_id = self.conversations.add_message(
            conversation_id, "assistant", text, model=model, status=status
        )
        for image in self._pending_images:
            self.conversations.add_image(
                message_id,
                conversation_id,
                image["path"],
                prompt=image["prompt"],
                model=image["model"],
                size=image["size"],
                width=image["width"],
                height=image["height"],
            )
        if self._bubble is not None:
            self._bubble.set_pending(False)
        return message_id

    def _on_failed(self, generation_id, code, message, partial):
        if generation_id != self.generation_id:
            return
        final_text = partial or self._pending_text
        conversation_id = self._active_conversation_id
        model = self._pending_model

        if self._bubble is not None:
            self._bubble.stop_duration()
        self._save_reply(conversation_id, final_text, model, "error")

        self._restore_idle()
        self._status_timer.stop()
        self.status_label.setText("生成失败")
        if conversation_id is not None:
            self._load_conversation_list(select_id=conversation_id)
        QMessageBox.warning(self, "生成失败", message)

    def _stop_generation(self):
        if self.worker is None or not self.worker.isRunning():
            return
        self.worker.request_stop()
        self._status_timer.stop()
        self.status_label.setText("正在停止…")

    # ------------------------------------------------------------------ 收尾
    def _discard_empty_reply(self):
        if self._bubble is not None:
            self.chat_panel.remove_message(self._bubble)
            self._bubble = None

    def _restore_idle(self):
        self.input_panel.set_streaming(False)
        self._bubble = None
        self._pending_text = ""
        self._pending_images = []
        self._pending_model = None
        self._active_conversation_id = None
        self._tool_phase = False

    def _abort_generation(self):
        """切换/新建/删除会话或关闭窗口前调用：落盘已生成内容并停线程。"""
        has_generation = self.worker is not None or self._bubble is not None
        if not has_generation:
            return

        # 先作废旧 generation，之后到达的 token / 终态信号一律丢弃。
        self.generation_id += 1
        worker = self.worker
        self.worker = None
        if worker is not None:
            worker.request_stop()
            worker.wait(5000)

        text = self._pending_text
        conversation_id = self._active_conversation_id
        if self._bubble is not None:
            self._bubble.stop_duration()
        self._save_reply(conversation_id, text, self._pending_model, "stopped")
        self._restore_idle()
        self._status_timer.stop()
        self._status_token += 1
        self._update_key_status()

    def _on_worker_finished(self, worker):
        if worker in self._retired_workers:
            self._retired_workers.remove(worker)
        if self.worker is worker:
            self.worker = None
        self._generation_meta.pop(getattr(worker, "generation_id", None), None)
        worker.deleteLater()

    # ------------------------------------------------------------------ 摘要
    def _maybe_summarize(self, conversation_id):
        """长对话到量了就后台压一次摘要，不阻塞聊天。"""
        if conversation_id is None or conversation_id in self._summarizing:
            return
        plan = self.context.summary_plan(conversation_id)
        if not plan:
            return

        worker = SummaryWorker(
            engine_kwargs=dict(
                base_url=self.config.chat_base_url,
                api_key=self.config.chat_api_key,
                extra_headers=self.config.chat_headers,
                timeout=self.config.chat_timeout,
                connect_timeout=self.config.chat_connect_timeout,
            ),
            model=self.config.default_model or current_model_id(self.model_combo),
            conversation_id=conversation_id,
            messages=self.context.build_summary_messages(plan),
            up_to_message_id=plan["up_to_message_id"],
            count=plan["count"],
            parent=self,
        )
        worker.done.connect(self._on_summary_done)
        worker.failed.connect(self._on_summary_failed)
        worker.finished.connect(lambda w=worker: self._on_summary_finished(w))

        self._summary_workers.append(worker)
        self._summarizing.add(conversation_id)
        worker.start()

    def _on_summary_done(self, conversation_id, summary, up_to_message_id, count):
        self.conversations.set_summary(conversation_id, summary, up_to_message_id)
        if conversation_id == self.current_conversation_id:
            self._flash_status(f"已把 {count} 条历史压缩成摘要", 2600)

    def _on_summary_failed(self, conversation_id, message):
        if conversation_id == self.current_conversation_id:
            self._flash_status("历史摘要更新失败", 2600)

    def _on_summary_finished(self, worker):
        self._summarizing.discard(worker.conversation_id)
        if worker in self._summary_workers:
            self._summary_workers.remove(worker)
        worker.deleteLater()

    # ------------------------------------------------------------------ 状态
    def _start_status_timer(self):
        self._status_token += 1
        self._gen_started_at = time.monotonic()
        self.status_label.setText("生成中 · 0 秒")
        self._status_timer.start()

    def _generation_active(self):
        return self._bubble is not None or (
            self.worker is not None and self.worker.isRunning()
        )

    def _update_gen_status(self):
        if self._tool_phase:
            seconds = int(time.monotonic() - self._tool_started_at)
            verb = "正在改图" if self._tool_name == "edit_image" else "正在生成图片"
            self.status_label.setText(f"{verb} · {seconds} 秒")
        else:
            seconds = int(time.monotonic() - self._gen_started_at)
            self.status_label.setText(f"生成中 · {seconds} 秒")

    def _tick_generation_status(self):
        if self._bubble is None and self.worker is None:
            self._status_timer.stop()
            return
        self._update_gen_status()

    def _flash_status(self, text, timeout_ms=2000):
        """短暂提示，过后自动回到原来的状态（生成中继续计时，空闲时回到默认）。"""
        self._status_token += 1
        token = self._status_token
        generating = self._generation_active()
        self.status_label.setText(text)
        if generating:
            # 只暂停"秒数刷新"，别让闪示文字被立刻覆盖。
            self._status_timer.stop()
        QTimer.singleShot(timeout_ms, lambda: self._after_flash(token, generating))

    def _after_flash(self, token, was_generating):
        if token != self._status_token:
            return
        if was_generating and self._generation_active():
            self._update_gen_status()
            self._status_timer.start()
        else:
            self._update_key_status()

    def _clear_status(self, token):
        if token == self._status_token:
            self._update_key_status()

    # ------------------------------------------------------------------ 设置
    def _ensure_api_key(self):
        if self.config.has_chat_key:
            return True
        if self._open_settings():
            return self.config.has_chat_key
        return False

    def _open_settings(self):
        root_before = self.data.root
        dialog = SettingsDialog(self.config, self, data=self.data)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._apply_settings()
            if self.data.root != root_before:
                QMessageBox.information(
                    self, "存储位置已切换",
                    f"新位置：\n{self.data.root}\n\n重启程序后生效，旧目录数据已原样保留。",
                )
            return True
        return False

    def _apply_settings(self):
        self.context.configure(
            window_size=self.config.context_window,
            system_prompt=self.config.system_prompt,
            summary_enabled=self.config.summary_enabled,
            summary_min_messages=self.config.summary_min_messages,
        )
        fill_model_combo(self.model_combo, self.config.default_model)
        self._sync_image_controls()
        self._update_key_status()

    def _sync_image_controls(self):
        """尺寸下拉跟随配置；没有生图能力时整块隐藏，不占位。"""
        self.input_panel.set_size_options(SIZE_PRESETS, self.config.image_size)
        self.input_panel.set_size_visible(
            bool(self.config.auto_image and self.config.has_image_key)
        )

    def _on_image_size_changed(self, value):
        self.config.set("image.size", value)
        try:
            self.config.save()
        except Exception:  # noqa: BLE001 - 保存失败不影响本次使用
            pass

    def _update_key_status(self):
        self.status_label.setText("" if self.config.has_chat_key else "未配置 API Key")

    # ------------------------------------------------------------------ 主题
    def _toggle_theme(self):
        theme = "light" if self._theme == "dark" else "dark"
        self._apply_theme(theme)
        self.config.set("ui.theme", theme)
        try:
            self.config.save()
        except Exception:  # noqa: BLE001 - 保存失败不影响本次使用
            pass

    def _apply_theme(self, theme):
        self._theme = normalize_theme(theme)
        app = QApplication.instance()
        if app is not None:
            app.setPalette(build_palette(self._theme))
        self.setStyleSheet(build_stylesheet(self._theme))
        self.theme_button.setText(THEME_LABELS[self._theme])
        if hasattr(self, "input_panel"):
            self.input_panel.refresh_style()

    def _on_model_changed(self, _index):
        model_id = current_model_id(self.model_combo)
        self.config.set("chat.default_model", model_id)
        try:
            self.config.save()
        except Exception:  # noqa: BLE001 - 保存失败不影响本次对话
            pass

    # ------------------------------------------------------------------ 关闭
    def closeEvent(self, event):  # noqa: N802 - Qt 命名
        # 生图/改图请求无法取消，退出等于放弃这一张（钱已经花了）。
        # 先问一声，别让用户莫名其妙丢掉等了一分多钟的图。
        if self.worker is not None and self.worker.isRunning() and self._tool_phase:
            answer = QMessageBox.question(
                self, "正在生成图片",
                "图片还在生成中，现在退出会放弃本次生成（请求无法退回）。\n确定退出吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return

        self._abort_generation()
        for worker in list(self._retired_workers):
            if worker.isRunning():
                worker.request_stop()
                worker.wait(2000)
        for worker in list(self._summary_workers):
            if worker.isRunning():
                worker.request_stop()
                worker.wait(2000)
        super().closeEvent(event)
