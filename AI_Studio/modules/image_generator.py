"""生图模块：从旧的 Tkinter 程序（app.py）抽取出来的纯逻辑。

只依赖 openai / PySide6(QImage)，不再依赖 tkinter。调用方拿到 ``ImageResult``
后自行决定怎么存、怎么显示。

实测（2026-11-02）：
- 模型 id 是 ``gpt-image-2.5``（``GPT Image 2.5 Sunburst`` 这类只是旧程序的
  显示名，不能直接当 id 用）
- 返回的是 ``b64_json`` 而不是 url，单张约 37 秒，所以超时给到 300 秒
- 生图模型在另一个账号分组，必须用独立的生图 Key
"""

from __future__ import annotations

import base64
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    NotFoundError,
    OpenAI,
    PermissionDeniedError,
    RateLimitError,
)

# 与旧程序一致的尺寸选项：(显示名, 接口参数)
SIZE_PRESETS = [
    ("1K 方图 (1024x1024)", "1024x1024"),
    ("1.5K 横图 (1536x1024)", "1536x1024"),
    ("1.5K 竖图 (1024x1536)", "1024x1536"),
    ("2K 方图 (2048x2048)", "2048x2048"),
    ("2K 横图 (2048x1152)", "2048x1152"),
    ("2K 竖图 (1152x2048)", "1152x2048"),
    ("4K 横图 (3840x2160)", "3840x2160"),
    ("4K 竖图 (2160x3840)", "2160x3840"),
]

DEFAULT_SIZE = "1024x1024"
DEFAULT_MODEL = "gpt-image-2.5"
IMAGE_MODEL_OPTIONS = [
    "gpt-image-2.5",
    "gpt-image-2.5-sunburst",
    "gpt-image-2.5-flare",
    "gpt-image-2",
]


class ImageError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class ImageResult:
    data: bytes
    path: Path
    width: int
    height: int
    format: str
    model: str
    prompt: str
    size: str
    revised_prompt: str = ""
    elapsed: float = 0.0
    extra: dict = field(default_factory=dict)


def translate_error(exc: Exception, model: str, timeout: float) -> ImageError:
    text = str(exc)
    if isinstance(exc, AuthenticationError):
        return ImageError("auth", "生图 API Key 无效，请在「设置 → 生图」中检查。")
    if isinstance(exc, PermissionDeniedError):
        return ImageError("forbidden", "生图 Key 没有权限（403）。")
    if isinstance(exc, NotFoundError):
        return ImageError(
            "model_not_found",
            f"生图模型 {model} 在当前分组不可用（404），这个 Key 可能不属于生图分组。",
        )
    if isinstance(exc, RateLimitError):
        return ImageError("rate_limit", "生图请求过于频繁或额度不足（429）。")
    if isinstance(exc, APITimeoutError):
        return ImageError("timeout", f"生图超时（{int(timeout)}s），可换小尺寸重试。")
    if isinstance(exc, APIConnectionError):
        return ImageError("network", "无法连接生图服务，请检查网络或代理。")
    if isinstance(exc, BadRequestError):
        return ImageError("bad_request", f"生图请求被拒绝（400）：{text[:200]}")
    if isinstance(exc, APIStatusError):
        return ImageError("http", f"生图服务返回错误（{exc.status_code}）：{text[:200]}")
    return ImageError("unknown", f"生图失败：{text[:200]}")


class ImageGenerator:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        extra_headers: dict | None = None,
        timeout: float = 300.0,
    ):
        if not api_key or not api_key.strip():
            raise ImageError("no_key", "尚未配置生图 API Key，请在「设置 → 生图」中填写。")
        if not base_url or not base_url.strip():
            raise ImageError("no_base_url", "尚未配置生图 API 地址。")

        self.base_url = base_url.strip().rstrip("/")
        self.timeout = float(timeout)
        self._client = OpenAI(
            api_key=api_key.strip(),
            base_url=self.base_url,
            default_headers={str(k): str(v) for k, v in (extra_headers or {}).items()},
            timeout=httpx.Timeout(self.timeout, connect=20.0),
            max_retries=0,
        )

    # ------------------------------------------------------------------ 生成
    def generate(
        self,
        prompt: str,
        size: str = DEFAULT_SIZE,
        model: str = DEFAULT_MODEL,
        save_dir: str | Path | None = None,
    ) -> ImageResult:
        prompt = (prompt or "").strip()
        if not prompt:
            raise ImageError("empty_prompt", "生图描述是空的。")

        valid_sizes = {value for _, value in SIZE_PRESETS}
        size = size if size in valid_sizes else DEFAULT_SIZE
        model = (model or "").strip() or DEFAULT_MODEL

        started = time.monotonic()
        try:
            response = self._client.images.generate(
                model=model, prompt=prompt, size=size
            )
        except Exception as exc:  # noqa: BLE001 - 统一翻译后抛出
            raise translate_error(exc, model, self.timeout) from exc

        return self._finalize(response, prompt, size, model, save_dir, started)

    # ------------------------------------------------------------------ 改图
    def edit(
        self,
        image_path: str | Path,
        prompt: str,
        size: str = DEFAULT_SIZE,
        model: str = DEFAULT_MODEL,
        save_dir: str | Path | None = None,
    ) -> ImageResult:
        """以本地图片为底图生成新图（images.edit）。实测本网关支持，单张约 70 秒。"""
        prompt = (prompt or "").strip()
        if not prompt:
            raise ImageError("empty_prompt", "改图描述是空的。")

        source = Path(image_path)
        if not source.is_file():
            raise ImageError("no_source", "参考图不存在，可能已被移动或删除。")

        valid_sizes = {value for _, value in SIZE_PRESETS}
        size = size if size in valid_sizes else DEFAULT_SIZE
        model = (model or "").strip() or DEFAULT_MODEL

        started = time.monotonic()
        try:
            with source.open("rb") as handle:
                response = self._client.images.edit(
                    model=model, image=handle, prompt=prompt, size=size
                )
        except Exception as exc:  # noqa: BLE001
            raise translate_error(exc, model, self.timeout) from exc

        return self._finalize(response, prompt, size, model, save_dir, started)

    # ------------------------------------------------------------------ 解析
    def _finalize(self, response, prompt, size, model, save_dir, started) -> ImageResult:
        items = getattr(response, "data", None) or []
        if not items:
            raise ImageError("empty_response", "生图接口没有返回任何图片。")
        item = items[0]

        b64 = getattr(item, "b64_json", None)
        url = getattr(item, "url", None)
        if b64:
            try:
                data = base64.b64decode(b64)
            except Exception as exc:  # noqa: BLE001
                raise ImageError("decode_error", f"图片数据解码失败：{exc}") from exc
        elif url:
            try:
                download = httpx.get(url, timeout=120.0, follow_redirects=True)
                download.raise_for_status()
                data = download.content
            except Exception as exc:  # noqa: BLE001
                raise ImageError("download_error", f"图片下载失败：{exc}") from exc
        else:
            raise ImageError("empty_response", "接口既没有返回 b64_json 也没有 url。")

        width, height, fmt = _inspect_image(data)
        path = _save_image(data, save_dir, fmt)

        return ImageResult(
            data=data,
            path=path,
            width=width,
            height=height,
            format=fmt,
            model=model,
            prompt=prompt,
            size=size,
            revised_prompt=str(getattr(item, "revised_prompt", "") or ""),
            elapsed=time.monotonic() - started,
        )

    def close(self) -> None:
        close = getattr(self._client, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass


def _inspect_image(data: bytes) -> tuple[int, int, str]:
    """借 QImage 读尺寸，不额外引入 Pillow 依赖。"""
    try:
        from PySide6.QtGui import QImage

        image = QImage()
        if image.loadFromData(data) and not image.isNull():
            return image.width(), image.height(), _guess_format(data)
    except Exception:  # noqa: BLE001 - 读不出尺寸也不该阻断保存
        pass
    return 0, 0, _guess_format(data)


def _guess_format(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "PNG"
    if data[:2] == b"\xff\xd8":
        return "JPEG"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "WEBP"
    return "PNG"


def _save_image(data: bytes, save_dir: str | Path | None, fmt: str) -> Path:
    ext = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}.get(fmt, ".png")
    target_dir = (
        Path(save_dir) if save_dir
        else Path(__file__).resolve().parents[1] / "data" / "images"
    )
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    path = target_dir / f"{stamp}_{uuid.uuid4().hex[:8]}{ext}"
    path.write_bytes(data)
    return path
