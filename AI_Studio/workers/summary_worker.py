"""历史摘要线程。

只负责把一段对话压成文字，**不写数据库**；结果回主线程落库。
摘要是后台附加动作，不阻塞聊天，也不影响发送按钮状态。
"""

from __future__ import annotations

import threading

from PySide6.QtCore import QThread, Signal

from core.chat_engine import ChatEngine, ChatError

MAX_SUMMARY_CHARS = 600


class SummaryWorker(QThread):
    # conversation_id, summary, up_to_message_id, 覆盖的消息条数
    done = Signal(int, str, int, int)
    failed = Signal(int, str)

    def __init__(
        self,
        engine_kwargs: dict,
        model: str,
        conversation_id: int,
        messages: list,
        up_to_message_id: int,
        count: int,
        parent=None,
    ):
        super().__init__(parent)
        self.engine_kwargs = dict(engine_kwargs)
        self.model = model
        self.conversation_id = conversation_id
        self.messages = list(messages)
        self.up_to_message_id = int(up_to_message_id)
        self.count = int(count)
        self._stop_event = threading.Event()

    def request_stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        engine = None
        try:
            engine = ChatEngine(**self.engine_kwargs)
            parts = []
            for piece in engine.stream_reply(
                self.messages, self.model, stop_event=self._stop_event
            ):
                if self._stop_event.is_set():
                    break
                parts.append(piece)
            text = " ".join("".join(parts).split()).strip()
            if not text:
                self.failed.emit(self.conversation_id, "摘要内容为空")
                return
            if len(text) > MAX_SUMMARY_CHARS:
                text = text[:MAX_SUMMARY_CHARS]
            self.done.emit(self.conversation_id, text, self.up_to_message_id, self.count)
        except ChatError as exc:
            self.failed.emit(self.conversation_id, exc.message)
        except Exception as exc:  # noqa: BLE001 - 兜底
            self.failed.emit(self.conversation_id, f"摘要失败：{exc}")
        finally:
            if engine is not None:
                engine.close()
