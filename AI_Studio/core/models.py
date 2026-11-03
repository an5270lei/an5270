"""聊天模型清单：整个程序唯一的事实源。

``available`` 与 ``tools`` 均来自 2026-11-02 对中转站的逐模型实测：

- ``available``：能否正常流式出字（分组权限决定，换分组后可能变）
- ``tools``：能否用 Function Calling（决定要不要把生图工具挂给它）
- ``tool_effort``：调工具时必须带的 ``reasoning_effort`` 值。
  实测 gpt-6 系列和 claude-opus-5-5 不带 ``none`` 会：
  gpt-6 系 → 400「requires Responses for tool calls」；
  opus-5-5 → 先输出一段话然后被截断，压根不发 tool_calls。

不可用不代表永久不可用，所以列表全部保留，只在界面标注。
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_MODEL = "gpt-6-luna"
UNAVAILABLE_HINT = "分组不可用"

VENDOR_ORDER = ("Claude", "OpenAI", "Gemini", "Grok")


@dataclass(frozen=True)
class ChatModel:
    id: str
    vendor: str
    available: bool = True
    first_token: float | None = None
    note: str = ""
    tools: bool = False
    tool_effort: str | None = None

    @property
    def display(self) -> str:
        if self.available:
            return self.id
        return f"{self.id} · {UNAVAILABLE_HINT}"


CHAT_MODELS: tuple[ChatModel, ...] = (
    # Claude
    ChatModel("claude-opus-4-8", "Claude", False, None, "404 当前分组暂不支持"),
    ChatModel("claude-sonnet-5", "Claude", True, 6.8, tools=True),
    ChatModel("claude-fable-5", "Claude", False, None, "404 模型请求失败"),
    ChatModel("claude-fable-5.1", "Claude", False, None, "404 模型请求失败"),
    ChatModel("claude-opus-5", "Claude", False, None, "连接后无响应，100s 超时"),
    ChatModel("claude-opus-5-5", "Claude", True, 3.4, tools=True, tool_effort="none"),
    # OpenAI
    ChatModel("gpt-5.5", "OpenAI", False, None, "404 当前分组暂不支持"),
    ChatModel("gpt-5.6-luna", "OpenAI", True, 14.2, tools=True),
    ChatModel("gpt-5.6-sol", "OpenAI", False, None, "连接后无响应，100s 超时"),
    ChatModel("gpt-5.6-terra", "OpenAI", False, None, "连接后无响应，100s 超时"),
    ChatModel("gpt-6-astra", "OpenAI", False, None, "500 暂时不可用"),
    ChatModel("gpt-6-luna", "OpenAI", True, 7.1, tools=True, tool_effort="none"),
    ChatModel("gpt-6-sol", "OpenAI", True, 7.6, "偶发『服务繁忙』", tools=True, tool_effort="none"),
    ChatModel("gpt-6.1-sol", "OpenAI", True, 23.7, "不支持工具调用", tools=False),
    # Gemini
    ChatModel("gemini-3.8-flash", "Gemini", True, 7.5, tools=True),
    # Grok
    ChatModel("grok-4.5", "Grok", False, None, "连接后无响应，100s 超时"),
    ChatModel("grok-4.6", "Grok", False, None, "连接后无响应，100s 超时"),
)

_BY_ID = {model.id: model for model in CHAT_MODELS}


def all_models() -> tuple[ChatModel, ...]:
    return CHAT_MODELS


def get_model(model_id: str | None) -> ChatModel | None:
    if not model_id:
        return None
    return _BY_ID.get(model_id)


def model_ids() -> list[str]:
    return [model.id for model in CHAT_MODELS]


def is_known(model_id: str | None) -> bool:
    return bool(model_id) and model_id in _BY_ID


def supports_tools(model_id: str | None) -> bool:
    model = get_model(model_id)
    return bool(model and model.available and model.tools)


def tool_effort(model_id: str | None) -> str | None:
    model = get_model(model_id)
    return model.tool_effort if model else None


def grouped_models() -> list[tuple[str, list[ChatModel]]]:
    """按厂商分组，顺序固定，便于下拉框展示。"""
    groups: list[tuple[str, list[ChatModel]]] = []
    for vendor in VENDOR_ORDER:
        items = [model for model in CHAT_MODELS if model.vendor == vendor]
        if items:
            groups.append((vendor, items))
    return groups


def label_for(model_id: str | None) -> str:
    """气泡上显示的模型名（不含不可用标注）。"""
    return (model_id or "").strip() or "未知模型"
