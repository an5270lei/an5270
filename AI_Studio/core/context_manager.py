"""上下文管理：最近消息窗口 + 历史摘要。

两种上下文一起用：

1. 最近 N 条消息原样带给模型（保证短期连贯）；
2. 更早的消息压缩成一段摘要塞进 system（保证长对话不忘事）。

摘要增量更新：只用 ``summary_until_message_id`` 之后的新内容去合并旧摘要，
不会每轮都把整段历史重算一遍。
"""

from __future__ import annotations

from core.conversation_manager import ConversationManager

SUMMARY_PROMPT = """你是对话压缩器。把【已有摘要】和【新增对话】合并成一段不超过 300 字的中文摘要。

保留：用户的目标与身份、已确认的事实与数字、已达成的结论和偏好、还没做完的待办。
丢弃：寒暄、重复表述、失败重试的过程、与主题无关的闲聊。
只输出摘要正文，不要加标题、不要解释你在做什么。

【已有摘要】
{old}

【新增对话】
{chunk}
"""

MAX_CHUNK_CHARS = 6000
MAX_ONE_MESSAGE_CHARS = 500


class ContextManager:
    def __init__(
        self,
        conversation_manager: ConversationManager,
        window_size: int = 20,
        system_prompt: str = "",
        summary_enabled: bool = True,
        summary_min_messages: int = 8,
    ):
        self.conversations = conversation_manager
        self.window_size = max(1, int(window_size))
        self.system_prompt = system_prompt or ""
        self.summary_enabled = bool(summary_enabled)
        self.summary_min_messages = max(2, int(summary_min_messages))

    def configure(self, window_size=None, system_prompt=None,
                  summary_enabled=None, summary_min_messages=None) -> None:
        if window_size is not None:
            self.window_size = max(1, int(window_size))
        if system_prompt is not None:
            self.system_prompt = system_prompt
        if summary_enabled is not None:
            self.summary_enabled = bool(summary_enabled)
        if summary_min_messages is not None:
            self.summary_min_messages = max(2, int(summary_min_messages))

    # ------------------------------------------------------------------ 读取
    def _rows(self, conversation_id) -> list[dict]:
        rows = self.conversations.get_messages(conversation_id)
        return [
            row for row in rows
            if row.get("role") in ("user", "assistant") and str(row.get("content") or "").strip()
        ]

    # -------------------------------------------------------------- 生成上下文
    def build_messages(self, conversation_id) -> list[dict]:
        rows = self._rows(conversation_id)
        recent = rows[-self.window_size:] if len(rows) > self.window_size else rows
        summary, _ = self.conversations.get_summary(conversation_id)

        system_bits = []
        if self.system_prompt.strip():
            system_bits.append(self.system_prompt.strip())
        if summary.strip():
            system_bits.append(
                "以下是本次对话更早部分的摘要（不是新问题，仅用于保持上下文连贯）：\n"
                + summary.strip()
            )

        messages: list[dict] = []
        if system_bits:
            messages.append({"role": "system", "content": "\n\n".join(system_bits)})
        for row in recent:
            messages.append({"role": row["role"], "content": row["content"]})
        return messages

    # -------------------------------------------------------------- 摘要任务
    def summary_plan(self, conversation_id):
        """返回需要摘要的内容；不需要摘要时返回 None。"""
        if not self.summary_enabled:
            return None

        rows = self._rows(conversation_id)
        if len(rows) <= self.window_size + self.summary_min_messages - 1:
            return None

        older = rows[:-self.window_size]
        old_summary, until_id = self.conversations.get_summary(conversation_id)
        pending = [row for row in older if int(row["id"]) > until_id]
        if len(pending) < self.summary_min_messages:
            return None

        return {
            "conversation_id": conversation_id,
            "old_summary": old_summary,
            "chunk": self._format_chunk(pending),
            "up_to_message_id": int(pending[-1]["id"]),
            "count": len(pending),
        }

    def build_summary_messages(self, plan) -> list[dict]:
        prompt = SUMMARY_PROMPT.format(
            old=(plan.get("old_summary") or "（无）").strip() or "（无）",
            chunk=plan.get("chunk") or "",
        )
        return [{"role": "user", "content": prompt}]

    @staticmethod
    def _format_chunk(rows) -> str:
        lines = []
        total = 0
        for row in rows:
            who = "用户" if row["role"] == "user" else "助手"
            text = str(row["content"]).strip().replace("\n", " ")
            if len(text) > MAX_ONE_MESSAGE_CHARS:
                text = text[:MAX_ONE_MESSAGE_CHARS] + "…"
            line = f"{who}：{text}"
            if total + len(line) > MAX_CHUNK_CHARS:
                break
            lines.append(line)
            total += len(line)
        return "\n".join(lines)
