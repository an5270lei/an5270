"""聊天引擎：OpenAI 兼容接口 + 真流式输出 + Function Calling。

设计要点：

1. ``stream=True`` 逐块产出，调用方拿到一块就能立刻上屏；
2. 停止生成靠外部传入的 ``threading.Event``，每个分块检查一次，命中即
   跳出循环并关闭底层连接——不使用 ``QThread.terminate()``；
3. 这个中转站的非标准错误（"分组暂不支持"、"服务繁忙"、工具调用的
   Responses 限制）统一翻译成中文可读信息；
4. 工具调用有「自动降级」：模型不支持 tools 时自动改走普通对话，而不是
   把 400 直接甩给用户。
"""

from __future__ import annotations

import threading
from typing import Any, Iterable, Iterator

import httpx
from openai import (
    APIConnectionError,
    APIError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    InternalServerError,
    NotFoundError,
    OpenAI,
    PermissionDeniedError,
    RateLimitError,
)

from core.models import tool_effort


class ChatError(Exception):
    """带错误码的可读异常，界面按 code 决定提示方式。"""

    def __init__(self, code: str, message: str, detail: str = ""):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail

    def __str__(self) -> str:  # pragma: no cover - 仅用于日志
        return f"[{self.code}] {self.message}"


def _body_text(exc: Exception) -> str:
    """尽量从中转站返回体里挖出人话。"""
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        for key in ("message", "error", "msg", "detail"):
            value = body.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, dict):
                inner = value.get("message")
                if isinstance(inner, str) and inner.strip():
                    return inner.strip()
    text = str(exc)
    return text.split("\n", 1)[0][:300] if text else ""


def translate_error(exc: Exception, model: str | None = None, timeout: float | None = None) -> ChatError:
    """把 SDK / 中转站异常翻译成中文提示。"""
    name = model or "该模型"
    low = (str(exc) + " " + _body_text(exc)).lower()

    if "temporarily unavailable" in low or "暂时不可用" in low:
        return ChatError("unavailable", f"{name} 当前临时不可用，请稍后重试或换用其他模型。", str(exc))
    if "service is busy" in low or "繁忙" in low:
        return ChatError("busy", f"{name} 当前服务繁忙，请稍后重试或换用其他模型。", str(exc))
    if "requires responses for tool calls" in low:
        return ChatError("tools_unsupported", f"{name} 不支持工具调用，请换用其他模型。", str(exc))
    if "分组" in low:
        return ChatError("group", f"{name} 在当前账号分组不可用，请换用其他模型或调整分组。", str(exc))

    if isinstance(exc, AuthenticationError):
        return ChatError("auth", "API Key 无效或已失效，请在「设置」中重新填写。", str(exc))
    if isinstance(exc, PermissionDeniedError):
        return ChatError("forbidden", "当前 Key 没有访问权限（403）。", str(exc))
    if isinstance(exc, NotFoundError):
        return ChatError("model_not_found", f"模型 {name} 不存在或当前分组不可用（404）。", str(exc))
    if isinstance(exc, RateLimitError):
        return ChatError("rate_limit", "请求过于频繁或额度不足（429），请稍后再试。", str(exc))
    if isinstance(exc, APITimeoutError):
        cause_text = repr(exc.__cause__) + repr(getattr(exc, "request", ""))
        if "ConnectTimeout" in cause_text or "handshake" in cause_text.lower():
            return ChatError(
                "connect_timeout",
                "连接中转站超时（TLS 握手失败），请检查网络或代理后重试。",
                str(exc),
            )
        seconds = int(timeout) if timeout else 120
        return ChatError(
            "timeout",
            f"等待响应超时（{seconds}s）。{name} 可能临时不可用，可换用其他模型或稍后重试。",
            str(exc),
        )
    if isinstance(exc, APIConnectionError):
        return ChatError("network", "无法连接中转站，请检查网络、代理或 API 地址设置。", str(exc))
    if isinstance(exc, BadRequestError):
        detail = _body_text(exc)
        return ChatError("bad_request", f"请求被拒绝（400）：{detail}", str(exc))
    if isinstance(exc, InternalServerError):
        detail = _body_text(exc)
        return ChatError("server", f"服务端错误（500）：{detail}", str(exc))
    if isinstance(exc, APIStatusError):
        detail = _body_text(exc)
        return ChatError("http", f"服务返回错误（{exc.status_code}）：{detail}", str(exc))
    if isinstance(exc, APIError):
        detail = _body_text(exc)
        return ChatError("api", f"接口返回异常：{detail}", str(exc))
    return ChatError("unknown", f"请求失败：{_body_text(exc) or exc}", str(exc))


class ChatEngine:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        extra_headers: dict | None = None,
        timeout: float = 120.0,
        connect_timeout: float = 15.0,
    ):
        if not api_key or not api_key.strip():
            raise ChatError("no_key", "尚未配置聊天 API Key，请先在「设置」中填写。")
        if not base_url or not base_url.strip():
            raise ChatError("no_base_url", "尚未配置聊天 API 地址，请先在「设置」中填写。")

        self.base_url = base_url.strip().rstrip("/")
        self.timeout = float(timeout)
        self._client = OpenAI(
            api_key=api_key.strip(),
            base_url=self.base_url,
            default_headers={str(k): str(v) for k, v in (extra_headers or {}).items()},
            timeout=httpx.Timeout(self.timeout, connect=float(connect_timeout)),
            # 这个中转站偶发 TLS 握手失败，允许 SDK 对连接类错误重试两次；
            # 401/403/404 这类确定性错误不会重试。
            max_retries=2,
        )

    # -------------------------------------------------------------- 纯文本流
    def stream_reply(
        self,
        messages: Iterable[dict],
        model: str,
        stop_event: threading.Event | None = None,
    ) -> Iterator[str]:
        """逐块产出文本增量（不带工具）。"""
        for kind, payload in self.stream_chat(messages, model, stop_event=stop_event):
            if kind == "text":
                yield payload

    # ---------------------------------------------------------- 带工具的事件流
    def stream_chat(
        self,
        messages: Iterable[dict],
        model: str,
        tools: list[dict] | None = None,
        reasoning_effort: str | None = None,
        stop_event: threading.Event | None = None,
    ) -> Iterator[tuple[str, Any]]:
        """产出事件：("text", 片段) / ("tool_calls", [dict]) / ("notice", 文案)。

        降级链（全部基于实测到的 400 文案）：
        - 模型不支持 tools -> 去掉 tools 重试一次，并给一条 notice
        - 不接受该 effort  -> 去掉 effort 重试
        - 缺 effort 才报错 -> 补 ``none`` 重试
        只在"还没吐出任何内容"时重试，绝不会重复输出。
        """
        state: dict[str, Any] = {
            "tools": tools or None,
            "effort": reasoning_effort if reasoning_effort is not None else tool_effort(model),
        }

        for _attempt in range(4):
            try:
                stream = self._open_stream(messages, model, state["tools"], state["effort"])
            except Exception as exc:  # noqa: BLE001 - 先看能否降级，再翻译
                action, notice = self._degrade(exc, state)
                if action is None:
                    raise translate_error(exc, model, self.timeout) from exc
                state.update(action)
                if notice:
                    yield ("notice", notice)
                continue

            yield from self._consume(stream, model, stop_event)
            return

        raise ChatError("tools_retry_exhausted", "工具调用重试次数用尽，请换用其他模型。")

    def _open_stream(self, messages, model, tools, effort):
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": list(messages),
            "stream": True,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        if effort:
            kwargs["reasoning_effort"] = effort
        return self._client.chat.completions.create(**kwargs)

    @staticmethod
    def _degrade(exc: Exception, state: dict) -> tuple[dict | None, str | None]:
        if not isinstance(exc, BadRequestError):
            return None, None
        text = (str(exc) + " " + _body_text(exc)).lower()

        if state["tools"] and "requires responses for tool calls" in text:
            return (
                {"tools": None, "effort": None},
                "该模型不支持工具调用，本次按普通对话处理。",
            )
        if state["tools"] and state["effort"] and "does not support reasoning effort" in text:
            return ({"effort": None}, None)
        if state["tools"] and not state["effort"] and "reasoning" in text:
            return ({"effort": "none"}, None)
        return None, None

    def _consume(self, stream, model: str, stop_event) -> Iterator[tuple[str, Any]]:
        tool_buffer: dict[int, dict] = {}
        try:
            for chunk in stream:
                if stop_event is not None and stop_event.is_set():
                    break
                choices = getattr(chunk, "choices", None)
                if not choices:
                    continue
                delta = getattr(choices[0], "delta", None)
                piece = getattr(delta, "content", None)
                if piece:
                    yield ("text", piece)
                for call in getattr(delta, "tool_calls", None) or []:
                    slot = tool_buffer.setdefault(
                        getattr(call, "index", 0) or 0,
                        {"id": "", "name": "", "arguments": ""},
                    )
                    if getattr(call, "id", None):
                        slot["id"] = call.id
                    function = getattr(call, "function", None)
                    if function is not None:
                        if getattr(function, "name", None):
                            slot["name"] += function.name
                        if getattr(function, "arguments", None):
                            slot["arguments"] += function.arguments
        except Exception as exc:  # noqa: BLE001 - 流中异常同样翻译
            raise translate_error(exc, model, self.timeout) from exc
        finally:
            close = getattr(stream, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass

        calls = [tool_buffer[key] for key in sorted(tool_buffer)]
        calls = [call for call in calls if call.get("name")]
        if calls:
            yield ("tool_calls", calls)

    # ------------------------------------------------------------ 连通性自检
    def test_connection(self, model: str) -> tuple[bool, str]:
        """真发一次请求并取到首个分块，验证地址/Key/请求头/模型是否都通。"""
        import time

        generator = self.stream_reply([{"role": "user", "content": "ping"}], model=model)
        started = time.perf_counter()
        try:
            next(generator)
            elapsed = time.perf_counter() - started
            return True, f"连接成功 · 首个分块 {elapsed:.1f}s · {model}"
        except StopIteration:
            elapsed = time.perf_counter() - started
            return True, f"连接成功，但 {model} 未返回内容（{elapsed:.1f}s）"
        except ChatError as exc:
            return False, exc.message
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)
        finally:
            generator.close()

    # ------------------------------------------------------------------ 释放
    def close(self) -> None:
        """释放底层连接池，避免长期运行越积越多。"""
        close = getattr(self._client, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass
