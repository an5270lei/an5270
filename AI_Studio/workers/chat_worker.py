"""后台对话线程：流式出字 + 工具调用循环。

约定：

- 线程只负责取数据和执行工具，**不碰数据库**；写库统一回到主线程，
  这样"停止生成"永远不会写出半条残缺记录。
- 停止通过 ``request_stop()`` 置位 Event，``stream_chat`` 每块检查一次。
- 生图是一次不可取消的 HTTP 请求：用户中途点停止，仍然等图片回来并上报，
  不让已经花的钱白花；只是不再继续下一轮对话。
- 任何退出路径（正常/停止/异常）只发一次终态信号。
"""

from __future__ import annotations

import threading

from PySide6.QtCore import QThread, Signal

from core.chat_engine import ChatEngine, ChatError
from core.tool_router import ToolCall, ToolRouter, parse_arguments

MAX_ROUNDS = 3


class ChatWorker(QThread):
    token = Signal(str)
    notice = Signal(str)
    # name, prompt, size
    tool_started = Signal(str, str, str)
    # conversation_id, task_json
    task_ready = Signal(int, str)
    # path, prompt, model, size, width, height, elapsed
    image_ready = Signal(str, str, str, str, int, int, float)
    # full_text, model, status(complete / stopped)
    completed = Signal(str, str, str)
    # code, message, partial_text
    failed = Signal(str, str, str)

    def __init__(
        self,
        engine: ChatEngine,
        messages: list,
        model: str,
        tools: list | None = None,
        router: ToolRouter | None = None,
        max_rounds: int = MAX_ROUNDS,
        attachments: list | None = None,
        conversation_id: int | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self.engine = engine
        self.messages = list(messages)
        self.model = model
        self.tools = tools
        self.router = router
        self.max_rounds = max(1, int(max_rounds))
        self.attachments = list(attachments or [])
        self.conversation_id = conversation_id
        self._stop_event = threading.Event()

    def request_stop(self) -> None:
        self._stop_event.set()

    @property
    def stop_requested(self) -> bool:
        return self._stop_event.is_set()

    # ------------------------------------------------------------------ 主循环
    def run(self) -> None:
        conversation = [dict(message) for message in self.messages]
        # 让模型知道这一轮有图可改（图片本身不上传，走 images.edit 的模型侧不读图）
        if self.attachments and conversation and conversation[-1].get("role") == "user":
            conversation[-1]["content"] = (
                str(conversation[-1].get("content") or "")
                + "\n\n（用户之前上传的产品图已作为编辑底图，要修改或继续制作图片时直接调用 edit_image，不要让用户重新发图）"
            )
        rounds: list[str] = []
        try:
            for _round in range(self.max_rounds):
                text_parts: list[str] = []
                tool_calls = None

                for kind, payload in self.engine.stream_chat(
                    conversation,
                    self.model,
                    tools=self.tools,
                    stop_event=self._stop_event,
                ):
                    if self._stop_event.is_set():
                        break
                    if kind == "text":
                        text_parts.append(payload)
                        self.token.emit(payload)
                    elif kind == "notice":
                        self.notice.emit(payload)
                    elif kind == "tool_calls":
                        tool_calls = payload

                text = "".join(text_parts)
                if text.strip():
                    rounds.append(text)

                if self._stop_event.is_set():
                    self.completed.emit("\n\n".join(rounds), self.model, "stopped")
                    return

                if not tool_calls or self.router is None:
                    self.completed.emit("\n\n".join(rounds), self.model, "complete")
                    return

                # ---- 把工具调用按协议回填进对话，再继续下一轮 ----
                conversation.append(
                    {
                        "role": "assistant",
                        "content": text or "",
                        "tool_calls": [
                            {
                                "id": call.get("id") or f"call_{index}",
                                "type": "function",
                                "function": {
                                    "name": call.get("name", ""),
                                    "arguments": call.get("arguments", "{}"),
                                },
                            }
                            for index, call in enumerate(tool_calls)
                        ],
                    }
                )

                for index, raw_call in enumerate(tool_calls):
                    call_id = raw_call.get("id") or f"call_{index}"
                    name = raw_call.get("name", "")
                    arguments = parse_arguments(raw_call.get("arguments", ""))
                    self.tool_started.emit(
                        name,
                        str(arguments.get("prompt") or ""),
                        str(self.router.size if self.router else ""),
                    )
                    outcome = self.router.dispatch(
                        ToolCall(
                            id=call_id,
                            name=name,
                            arguments=arguments,
                            raw_arguments=raw_call.get("arguments", ""),
                        )
                    )
                    if outcome.image is not None:
                        image = outcome.image
                        self.image_ready.emit(
                            str(image.path),
                            image.prompt,
                            image.model,
                            image.size,
                            image.width,
                            image.height,
                            image.elapsed,
                        )
                    if outcome.task is not None and self.conversation_id is not None:
                        import json as _json
                        self.task_ready.emit(
                            self.conversation_id,
                            _json.dumps(outcome.task, ensure_ascii=False),
                        )
                    if not outcome.ok and outcome.image is None:
                        self.notice.emit("工具执行没有成功，已把原因告诉模型。")
                    conversation.append(
                        {
                            "role": "tool",
                            "tool_call_id": call_id,
                            "content": outcome.content,
                        }
                    )

                if self._stop_event.is_set():
                    self.completed.emit("\n\n".join(rounds), self.model, "stopped")
                    return

            # 轮数用完：直接收尾，避免无限循环
            self.completed.emit("\n\n".join(rounds), self.model, "complete")
        except ChatError as exc:
            self.failed.emit(exc.code, exc.message, "\n\n".join(rounds))
        except Exception as exc:  # noqa: BLE001 - 兜底，绝不让线程静默退出
            self.failed.emit("unknown", f"请求失败：{exc}", "\n\n".join(rounds))
        finally:
            # 释放 httpx 连接池，避免多次对话后句柄堆积。
            self.engine.close()
