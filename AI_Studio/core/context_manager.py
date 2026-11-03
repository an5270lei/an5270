"""上下文管理：最近消息窗口 + 历史摘要。

两种上下文一起用：

1. 最近 N 条消息原样带给模型（保证短期连贯）；
2. 更早的消息压缩成一段摘要塞进 system（保证长对话不忘事）。

摘要增量更新：只用 ``summary_until_message_id`` 之后的新内容去合并旧摘要，
不会每轮都把整段历史重算一遍。
"""

from __future__ import annotations

from pathlib import Path

from core.conversation_manager import ConversationManager
from modules.image_generator import context_data_url

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
    def build_messages(self, conversation_id, vision: bool = False) -> list[dict]:
        rows = self._rows(conversation_id)
        recent = rows[-self.window_size:] if len(rows) > self.window_size else rows
        summary, _ = self.conversations.get_summary(conversation_id)
        image_map = self.conversations.get_images(conversation_id)
        task = self.conversations.get_task(conversation_id)

        system_bits = []
        if self.system_prompt.strip():
            system_bits.append(self.system_prompt.strip())
        if summary.strip():
            system_bits.append(
                "以下是本次对话更早部分的摘要（不是新问题，仅用于保持上下文连贯）：\n"
                + summary.strip()
            )
        image_state = self._image_state_line(image_map, vision=vision)
        if image_state:
            system_bits.append(image_state)
        task_line = self._task_line(task)
        if task_line:
            system_bits.append(task_line)

        messages: list[dict] = []
        if system_bits:
            messages.append({"role": "system", "content": "\n\n".join(system_bits)})

        # 视觉模型：把关键图片重新"给模型看"。预算 2 张：优先产品原图，
        # 其次最近的成品图；图片挂在它们原本所属的消息上（一条消息可带多张），
        # 模型才知道哪张是用户的原图、哪张是之前生成的成品。
        attached: dict[int, list[dict]] = {}
        if vision:
            budget = 2
            for row in reversed(recent):
                products = [
                    item for item in image_map.get(row["id"], [])
                    if item.get("role") == "product"
                ]
                if products and budget > 0:
                    attached[row["id"]] = [products[0]]
                    budget -= 1
                    break
            for row in reversed(recent):
                generated = [
                    item for item in image_map.get(row["id"], [])
                    if item.get("role") == "generated"
                ]
                if generated and budget > 0:
                    bucket = attached.setdefault(row["id"], [])
                    if generated[0] not in bucket:
                        bucket.append(generated[0])
                        budget -= 1
                    break

        for row in recent:
            content = row["content"]
            marker = self._image_marker(row, image_map.get(row["id"], []))
            if marker:
                content = f"{content}\n{marker}"

            assets = attached.get(row["id"]) if vision else None
            if assets:
                roles = [item.get("role") for item in assets]
                if "product" in roles and "generated" in roles:
                    note = "（随消息附图：第一张是用户上传的产品原图，第二张是之前生成的成品图）"
                elif "product" in roles:
                    note = "（随消息附图：这是用户上传的产品原图）"
                else:
                    note = "（随消息附图：这是之前生成的成品图）"
                parts: list[dict] = [{"type": "text", "text": f"{content}\n{note}"}]
                ok_urls = True
                for item in assets:
                    data_url = context_data_url(item["path"])
                    if not data_url:
                        ok_urls = False
                        break
                    parts.append({"type": "image_url", "image_url": {"url": data_url}})
                if ok_urls:
                    messages.append({"role": row["role"], "content": parts})
                    continue

            messages.append({"role": row["role"], "content": content})
        return messages

    @staticmethod
    def _task_line(task: dict | None) -> str:
        if not task:
            return ""
        lines = ["【当前任务】" + str(task.get("goal") or "")]
        plan = task.get("plan") or []
        if plan:
            lines.append("规划：" + "；".join(f"{i + 1}. {item}" for i, item in enumerate(plan)))
        completed = task.get("completed") or []
        if completed:
            lines.append("已完成：" + "、".join(f"第{n}项" for n in completed))
        if task.get("current_step"):
            lines.append(f"当前进行：第 {task['current_step']} 项")
        if str(task.get("status")) == "completed":
            lines.append("任务已全部完成。")
        lines.append("开始新任务或每完成一项后，用 remember_task 更新进度。")
        return "\n".join(lines)

    @staticmethod
    def _image_marker(row, images: list[dict]) -> str:
        """把图片痕迹写进上下文，模型才知道图存在、是谁的。"""
        if not images:
            return ""
        if row["role"] == "user":
            uploads = [item for item in images if item.get("kind") == "upload"] or images
            names = "、".join(Path(item["path"]).name for item in uploads)
            return f"[用户在这条消息里上传了图片：{names}；这是编辑底图，改图时直接调用 edit_image，不要让用户重新发图]"
        names = "、".join(Path(item["path"]).name for item in images)
        return f"[这条回复生成了图片：{names}]"

    @staticmethod
    def _image_state_line(image_map: dict, vision: bool = False) -> str:
        uploads, generated = [], 0
        for rows in image_map.values():
            for item in rows:
                if item.get("kind") == "upload":
                    uploads.append(Path(item["path"]).name)
                else:
                    generated += 1
        if not uploads and not generated:
            return ""
        parts = []
        if uploads:
            parts.append(f"用户上传过产品图（{ '、'.join(uploads) }），它是对话的编辑底图")
        if generated:
            parts.append(f"对话中已经生成了 {generated} 张图片")
        if vision:
            # 视觉模型：图片已经真实附在对应消息上，告诉它"直接看"，
            # 绝不能出现"无法查看图片"之类的旧话术——那会让模型宁可否认也不看图。
            return (
                "【图片上下文】" + "；".join(parts) + "。"
                "产品图已经随对应消息附上，你可以直接看到图片内容："
                "规划详情页时以图中实际产品为准，不要编造图中没有的功能和参数。"
                "用户上传的产品图是商品主体：只要生成结果需要保留这个产品，就必须调用 "
                "edit_image 基于底图生成，不要用 generate_image 凭空生成（那会画成别的东西）；"
                "只有与该产品完全无关的图才用 generate_image。"
            )
        return (
            "【图片上下文】" + "；".join(parts) + "。"
            "用户上传的产品图是商品主体：只要生成结果需要保留这个产品，就必须调用 "
            "edit_image 基于底图生成，不要用 generate_image 凭空生成（那会画成别的东西）；"
            "只有与该产品完全无关的图才用 generate_image。"
            "聊天模型本身无法查看图片内容，但生图接口可以基于底图编辑，"
            "不要以“看不到图片”为由要求用户重新发图。"
        )

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

        # 关键：进度只标记到"真正装进提示词"的那条，装不下的留给下一轮，
        # 否则超长对话会被静默跳过一部分消息。
        chunk, last_id = self._format_chunk(pending)
        if not chunk or last_id is None:
            return None

        return {
            "conversation_id": conversation_id,
            "old_summary": old_summary,
            "chunk": chunk,
            "up_to_message_id": last_id,
            "count": sum(1 for row in pending if int(row["id"]) <= last_id),
        }

    def build_summary_messages(self, plan) -> list[dict]:
        prompt = SUMMARY_PROMPT.format(
            old=(plan.get("old_summary") or "（无）").strip() or "（无）",
            chunk=plan.get("chunk") or "",
        )
        return [{"role": "user", "content": prompt}]

    @staticmethod
    def _format_chunk(rows) -> tuple[str, int | None]:
        """返回 (提示词文本, 实际消费到的最后一条消息 id)。"""
        lines = []
        total = 0
        last_id = None
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
            last_id = int(row["id"])
        return "\n".join(lines), last_id
