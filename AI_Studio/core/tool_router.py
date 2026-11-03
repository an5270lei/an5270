"""工具定义与调度。

目前只有一个工具：``generate_image``。schema 里的 description 直接决定模型
会不会乱调用，所以措辞写得比较硬：只有用户明确要图才调。

Function Calling 实测（2026-11-02，见 README）：部分模型不传
``reasoning_effort="none"`` 会 400，参数由 core/models.py 的能力表驱动。
"""

from __future__ import annotations

import json
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


def build_tools(with_edit: bool = False) -> list[dict]:
    tools = [_GENERATE_IMAGE_SCHEMA]
    if with_edit:
        tools.append(_EDIT_IMAGE_SCHEMA)
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
        attachments: list | None = None,
    ):
        self.base_url = base_url
        self.api_key = api_key
        self.extra_headers = extra_headers or {}
        self.model = (model or "").strip() or IMAGE_MODEL_OPTIONS[0]
        self.size = size or _VALID_SIZES[0]
        self.timeout = timeout
        self.save_dir = save_dir
        # 本轮用户上传的图片，改图时作底图（取第一张）
        self.attachments = [Path(p) for p in (attachments or []) if p]

    def dispatch(self, call: ToolCall) -> ToolOutcome:
        if call.name not in (GENERATE_IMAGE, EDIT_IMAGE):
            return ToolOutcome(False, f'{{"status":"error","reason":"未知工具 {call.name}"}}')

        prompt = str(call.arguments.get("prompt") or "").strip()
        if not prompt:
            return ToolOutcome(False, '{"status":"error","reason":"缺少 prompt 参数"}')

        # 尺寸只听设置的：模型无权改画布大小（实测它会把 4K 悄悄写成 1024x1024）。
        size = self.size if self.size in _VALID_SIZES else _VALID_SIZES[0]

        if call.name == EDIT_IMAGE and not self.attachments:
            return ToolOutcome(
                False,
                '{"status":"error","reason":"用户这一轮没有上传图片，无法改图；可以改用 generate_image"}',
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
                result = generator.edit(
                    self.attachments[0],
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
        return ToolOutcome(
            True, json.dumps(payload, ensure_ascii=False), image=result
        )
