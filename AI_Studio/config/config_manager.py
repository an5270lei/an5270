"""配置管理。

分两个文件存放，互不混用：

- ``config.json``：非敏感配置（地址、请求头、默认模型、超时等），明文可读；
- ``secrets.dat``：只有 API Key，用 Windows DPAPI 加密后落盘，绝不写明文。

默认目录为 ``%APPDATA%\\AI Studio``。
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

from config import dpapi

APP_FOLDER_NAME = "AI Studio"

DEFAULT_CHAT_HEADERS = {"x-openai-actor-authorization": "local-image-extension"}
DEFAULT_IMAGE_HEADERS = {"x-openai-actor-authorization": "local-image-extension"}

DEFAULTS: dict[str, Any] = {
    "ui": {
        "theme": "dark",
    },
    "chat": {
        "base_url": "https://api.x5m5x.com/v1",
        "extra_headers": DEFAULT_CHAT_HEADERS,
        "default_model": "gpt-6-luna",
        "timeout": 120,
        "connect_timeout": 15,
        "context_window": 20,
        "system_prompt": "",
        "auto_image": True,
        "summary_enabled": True,
        "summary_min_messages": 8,
    },
    "image": {
        "base_url": "https://api.x5m5x.com/v1",
        "extra_headers": DEFAULT_IMAGE_HEADERS,
        "model": "gpt-image-2.5",
        "size": "1024x1024",
        "timeout": 300,
    },
}


def _default_config_dir() -> Path:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return Path(base) / APP_FOLDER_NAME


def _deep_merge(base: dict, patch: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in (patch or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


class ConfigManager:
    def __init__(self, config_dir: str | Path | None = None):
        self.config_dir = Path(config_dir) if config_dir else _default_config_dir()
        self.config_file = self.config_dir / "config.json"
        self.secrets_file = self.config_dir / "secrets.dat"
        self._data: dict[str, Any] = copy.deepcopy(DEFAULTS)
        self._secrets: dict[str, str] = {"chat_api_key": "", "image_api_key": ""}
        self.load()

    # ------------------------------------------------------------------ 读写
    def load(self) -> None:
        self._data = copy.deepcopy(DEFAULTS)
        if self.config_file.exists():
            try:
                raw = json.loads(self.config_file.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    self._data = _deep_merge(DEFAULTS, raw)
            except (OSError, ValueError):
                # 配置文件损坏时退回默认值，不影响启动。
                self._data = copy.deepcopy(DEFAULTS)

        self._secrets = {"chat_api_key": "", "image_api_key": ""}
        if self.secrets_file.exists():
            try:
                plain = dpapi.decrypt(self.secrets_file.read_bytes())
                loaded = json.loads(plain.decode("utf-8"))
                if isinstance(loaded, dict):
                    for key in ("chat_api_key", "image_api_key"):
                        self._secrets[key] = str(loaded.get(key) or "")
            except Exception:
                # 换了 Windows 账户或文件损坏：清空密钥，要求重新填写。
                self._secrets = {"chat_api_key": "", "image_api_key": ""}

    def save(self) -> None:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.config_file.with_name("config.json.tmp")
        tmp.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, self.config_file)
        self._save_secrets()

    def _save_secrets(self) -> None:
        payload = json.dumps(self._secrets, ensure_ascii=False).encode("utf-8")
        encrypted = dpapi.encrypt(payload)
        self.config_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.secrets_file.with_name("secrets.dat.tmp")
        tmp.write_bytes(encrypted)
        os.replace(tmp, self.secrets_file)

    # ------------------------------------------------------------- 取/存配置
    def get(self, key_path: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in key_path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, key_path: str, value: Any) -> None:
        parts = key_path.split(".")
        node = self._data
        for part in parts[:-1]:
            if not isinstance(node.get(part), dict):
                node[part] = {}
            node = node[part]
        node[parts[-1]] = value

    # ------------------------------------------------------------------ 密钥
    @property
    def chat_api_key(self) -> str:
        return self._secrets.get("chat_api_key", "")

    @chat_api_key.setter
    def chat_api_key(self, value: str) -> None:
        self._secrets["chat_api_key"] = (value or "").strip()

    @property
    def image_api_key(self) -> str:
        return self._secrets.get("image_api_key", "")

    @image_api_key.setter
    def image_api_key(self, value: str) -> None:
        self._secrets["image_api_key"] = (value or "").strip()

    @property
    def has_chat_key(self) -> bool:
        return bool(self.chat_api_key)

    # ---------------------------------------------------------------- 便捷项
    @property
    def chat_base_url(self) -> str:
        return str(self.get("chat.base_url", "") or "").rstrip("/")

    @property
    def chat_headers(self) -> dict:
        headers = self.get("chat.extra_headers", {}) or {}
        return {str(k): str(v) for k, v in headers.items() if str(k).strip()}

    @property
    def chat_timeout(self) -> float:
        try:
            return float(self.get("chat.timeout", 120) or 120)
        except (TypeError, ValueError):
            return 120.0

    @property
    def chat_connect_timeout(self) -> float:
        try:
            return float(self.get("chat.connect_timeout", 15) or 15)
        except (TypeError, ValueError):
            return 15.0

    @property
    def default_model(self) -> str:
        return str(self.get("chat.default_model", "") or "")

    @property
    def context_window(self) -> int:
        try:
            return max(1, int(self.get("chat.context_window", 20) or 20))
        except (TypeError, ValueError):
            return 20

    @property
    def system_prompt(self) -> str:
        return str(self.get("chat.system_prompt", "") or "")

    @property
    def auto_image(self) -> bool:
        return bool(self.get("chat.auto_image", True))

    @property
    def summary_enabled(self) -> bool:
        return bool(self.get("chat.summary_enabled", True))

    @property
    def summary_min_messages(self) -> int:
        try:
            return max(2, int(self.get("chat.summary_min_messages", 8) or 8))
        except (TypeError, ValueError):
            return 8

    # ---------------------------------------------------------------- 生图项
    @property
    def image_base_url(self) -> str:
        base = str(self.get("image.base_url", "") or "").strip().rstrip("/")
        return base or self.chat_base_url

    @property
    def image_headers(self) -> dict:
        headers = self.get("image.extra_headers", {}) or {}
        return {str(k): str(v) for k, v in headers.items() if str(k).strip()}

    @property
    def image_model(self) -> str:
        return str(self.get("image.model", "") or "gpt-image-2.5")

    @property
    def image_size(self) -> str:
        return str(self.get("image.size", "") or "1024x1024")

    @property
    def image_timeout(self) -> float:
        try:
            return float(self.get("image.timeout", 300) or 300)
        except (TypeError, ValueError):
            return 300.0

    @property
    def has_image_key(self) -> bool:
        return bool(self.image_api_key)
