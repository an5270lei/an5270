"""工具定义与调度。

目前只有一个工具：``generate_image``。schema 里的 description 直接决定模型
会不会乱调用，所以措辞写得比较硬：只有用户明确要图才调。

Function Calling 实测（2026-11-02，见 README）：部分模型不传
``reasoning_effort="none"`` 会 400，参数由 core/models.py 的能力表驱动。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from modules.image_generator import (
    IMAGE_MODEL_OPTIONS,
    SIZE_PRESETS,
    ImageError,
    ImageGenerator,
    ImageResult,
)

GENERATE_IMAGE = "generate_image"
EDIT_IMAGE = "edit_image"
REMEMBER_TASK = "remember_task"

_VALID_SIZES = [value for _, value in SIZE_PRESETS]

_GENERATE_IMAGE_SCHEMA = {
    "type": "function",
    "function": {
        "name": GENERATE_IMAGE,
        "description": (
            "根据文字描述生成一张图片。"
            "仅当用户明确要求画图、出图、生成图片、做海报/封面/插画时才调用。"
            "普通问答、翻译、写文案、分析需求都不要调用。"
            "画布尺寸由用户在设置里固定，这里不要指定尺寸，"
            "需要横版或竖版时在 prompt 里写明构图方向即可。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": (
                        "用于生图的完整描述：主体、场景、风格、构图、光线、色调。"
                        "把用户的原话扩写成可直接出图的提示词，不要包含'请生成'这类指令词。"
                    ),
                },
            },
            "required": ["prompt"],
        },
    },
}


_EDIT_IMAGE_SCHEMA = {
    "type": "function",
    "function": {
        "name": EDIT_IMAGE,
        "description": (
            "以用户这一轮上传的图片为底图，按文字要求修改它：换风格、换背景、"
            "换天气/时间、加或去掉元素、局部调整。"
            "仅当用户确实上传了图片、并且要求改动这张图时调用；"
            "没有上传图片不要调用。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": (
                        "要改成什么样：明确写出必须保留的部分和要改变的部分，"
                        "以及风格、光线、色调。不要写'请修改这张图'这类指令词。"
                    ),
                },
            },
            "required": ["prompt"],
        },
    },
}


_REMEMBER_TASK_SCHEMA = {
    "type": "function",
    "function": {
        "name": REMEMBER_TASK,
        "description": (
            "记录或更新当前的多图任务进度（例如一套 8 张详情页）。"
            "当你规划好整体布局后调用一次；之后每完成一张并确认成功后再调用更新。"
            "程序会校验进度与实际生成数量，虚假进度会被修正。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "goal": {"type": "string", "description": "任务目标，如：制作一套 8 张的电商详情页"},
                "plan": {
                    "type": "array", "items": {"type": "string"},
                    "description": "整组图片的规划清单，按顺序，如：['第一张：开篇主视觉', '第二张：核心卖点']",
                },
                "completed": {
                    "type": "array", "items": {"type": "integer"},
                    "description": "已经确认完成的序号列表，如 [1]；只有真的生成成功才计入",
                },
                "current_step": {"type": "integer", "description": "当前正在制作的序号"},
                "status": {"type": "string", "enum": ["in_progress", "completed"]},
            },
            "required": ["goal", "plan"],
        },
    },
}


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict
    raw_arguments: str = ""


@dataclass
class ToolOutcome:
    ok: bool
    content: str
    image: ImageResult | None = None
    task: dict | None = None   # remember_task 的结果，由主线程落库


def select_edit_base(uploaded_assets: list[dict],
                     product_asset: dict | None,
                     generated_asset: dict | None) -> tuple[dict | None, str]:
    """编辑底图的 5 级优先级选择。

    ``uploaded_assets`` 是本轮上传且角色已标注的图片（dict，含 path/role），
    ``product_asset`` / ``generated_asset`` 是历史资产里对应角色的最近一张。
    返回 (选中的资产或 None, 选择原因)。选择原因会写进工具结果，方便追踪。
    """
    product_now = [a for a in uploaded_assets if a.get("role") == "product"]
    if product_now:
        return product_now[0], "本轮上传的产品图，作为编辑主体"

    any_upload = [a for a in uploaded_assets if a.get("kind") == "upload"]
    if any_upload:
        return any_upload[0], "本轮上传的参考图（没有更合适的产品图）"

    if product_asset:
        return product_asset, "本轮没有新上传图片，自动使用历史产品原图"

    if generated_asset:
        return generated_asset, "兜底：没有产品原图，使用上一张生成结果（它不是产品原图）"

    return None, ""


def build_tools(with_edit: bool = False, with_task: bool = False) -> list[dict]:
    tools = [_GENERATE_IMAGE_SCHEMA]
    if with_edit:
        tools.append(_EDIT_IMAGE_SCHEMA)
    if with_task:
        tools.append(_REMEMBER_TASK_SCHEMA)
    return tools


def parse_arguments(raw: str) -> dict:
    if not raw or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


class ToolRouter:
    """把模型的工具调用落到真实动作上。只在后台线程里用。"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        extra_headers: dict | None = None,
        model: str | None = None,
        size: str | None = None,
        timeout: float = 300.0,
        save_dir: str | Path | None = None,
        edit_base: str | Path | None = None,
        edit_base_info: dict | None = None,
        reference_images: list | None = None,
        conversation_id: int | None = None,
        generated_count: int = 0,
    ):
        self.base_url = base_url
        self.api_key = api_key
        self.extra_headers = extra_headers or {}
        self.model = (model or "").strip() or IMAGE_MODEL_OPTIONS[0]
        # 构造时就夹紧到合法值，调用方传错也不至于把非法参数发给接口
        self.size = size if size in _VALID_SIZES else _VALID_SIZES[0]
        self.timeout = timeout
        self.save_dir = save_dir
        # 编辑底图与参考图：由调用方按 5 级优先级选好
        self.edit_base = Path(edit_base) if edit_base else None
        self.edit_base_info = edit_base_info or {}
        self.reference_images = [Path(p) for p in (reference_images or []) if p]
        self.conversation_id = conversation_id
        self.generated_count = int(generated_count or 0)

    def dispatch(self, call: ToolCall) -> ToolOutcome:
        if call.name == REMEMBER_TASK:
            return self._remember_task(call)

        if call.name not in (GENERATE_IMAGE, EDIT_IMAGE):
            return ToolOutcome(False, f'{{"status":"error","reason":"未知工具 {call.name}"}}')

        prompt = str(call.arguments.get("prompt") or "").strip()
        if not prompt:
            return ToolOutcome(False, '{"status":"error","reason":"缺少 prompt 参数"}')

        # 尺寸只听设置的：模型无权改画布大小（实测它会把 4K 悄悄写成 1024x1024）。
        size = self.size if self.size in _VALID_SIZES else _VALID_SIZES[0]

        if call.name == EDIT_IMAGE and self.edit_base is None:
            return ToolOutcome(
                False,
                '{"status":"error","reason":"当前没有可用的编辑底图（用户还没上传过产品图）；可以改用 generate_image"}',
            )

        generator = None
        try:
            generator = ImageGenerator(
                base_url=self.base_url,
                api_key=self.api_key,
                extra_headers=self.extra_headers,
                timeout=self.timeout,
            )
            if call.name == EDIT_IMAGE:
                sources = [self.edit_base] + [
                    ref for ref in self.reference_images if ref != self.edit_base
                ][:3]
                result = generator.edit(
                    sources,
                    prompt=prompt,
                    size=size,
                    model=self.model,
                    save_dir=self.save_dir,
                )
            else:
                result = generator.generate(
                    prompt=prompt, size=size, model=self.model, save_dir=self.save_dir
                )
        except ImageError as exc:
            return ToolOutcome(
                False,
                json.dumps(
                    {"status": "error", "reason": exc.message},
                    ensure_ascii=False,
                ),
            )
        except Exception as exc:  # noqa: BLE001
            return ToolOutcome(
                False,
                json.dumps({"status": "error", "reason": str(exc)[:200]}, ensure_ascii=False),
            )
        finally:
            if generator is not None:
                generator.close()

        payload = {
            "status": "ok",
            "file": result.path.name,
            "width": result.width,
            "height": result.height,
            "model": result.model,
            "size": result.size,
        }
        # 让模型和用户都能追踪这次编辑用的是什么底图
        if call.name == EDIT_IMAGE and self.edit_base_info:
            payload["edit_base"] = self.edit_base_info
        return ToolOutcome(
            True, json.dumps(payload, ensure_ascii=False), image=result
        )

    def _remember_task(self, call: ToolCall) -> ToolOutcome:
        args = call.arguments
        goal = str(args.get("goal") or "").strip()
        plan = args.get("plan")
        if not goal or not isinstance(plan, list) or not plan:
            return ToolOutcome(
                False,
                '{"status":"error","reason":"remember_task 需要 goal 和非空的 plan 列表"}',
            )
        plan = [str(item) for item in plan][:20]

        completed = [int(n) for n in (args.get("completed") or []) if str(n).strip().isdigit()]
        completed = sorted({n for n in completed if 1 <= n <= len(plan)})

        # 程序校验：完成数量不能超过实际生成的图片数，防止模型虚报。
        # 一旦修正了进度，当前步骤也要跟着回退，不能停在没做过的步骤上。
        generated = self.generated_count
        corrected = False
        if len(completed) > generated:
            completed = completed[:generated]
            corrected = True
        note = f"（注意：进度已按实际生成的 {generated} 张图片修正）" if corrected else ""

        if corrected:
            current_step = min(max(completed) + 1, len(plan)) if completed else 1
        else:
            current_step = args.get("current_step")
            try:
                current_step = int(current_step)
            except (TypeError, ValueError):
                current_step = (max(completed) + 1) if completed else 1
            current_step = min(max(current_step, 1), len(plan))

        status = str(args.get("status") or "in_progress")
        if status not in ("in_progress", "completed"):
            status = "in_progress"
        if len(completed) == len(plan):
            status = "completed"

        task = {
            "goal": goal,
            "plan": plan,
            "completed": completed,
            "current_step": current_step,
            "status": status,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        content = json.dumps(
            {"status": "ok", "task": task, "note": note}, ensure_ascii=False
        )
        return ToolOutcome(True, content, task=task)
