"""数据目录管理：把「程序」和「用户数据」分开。

三种存储模式：

- ``portable``：数据放在程序目录下的 ``data/``，拷走整个文件夹就是完整备份（默认，
  也是现在一直在用的方式）；
- ``appdata``：数据放在 ``%APPDATA%\\AI Studio\\data``，覆盖安装/升级不影响历史；
- ``custom``：用户自己挑一个目录。

模式本身存在**固定位置**的 ``mode.json``（``%APPDATA%\\AI Studio\\``）——不能存在
data 目录里，否则一旦切换目录就找不到"模式"这个信息了。

配置（config.json / secrets.dat）跟着数据根走；secrets.dat 是 DPAPI 加密的，
换 Windows 账户解不开，属于预期行为。
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import time
import zipfile
from pathlib import Path

APP_FOLDER = "AI Studio"

MODE_PORTABLE = "portable"
MODE_APPDATA = "appdata"
MODE_CUSTOM = "custom"
MODES = (MODE_PORTABLE, MODE_APPDATA, MODE_CUSTOM)

MODE_LABELS = {
    MODE_PORTABLE: "便携模式（数据在程序目录）",
    MODE_APPDATA: "标准模式（数据在 AppData）",
    MODE_CUSTOM: "自定义目录",
}

BACKUP_SUBDIR = "AIStudioBackup"
BACKUP_VERSION = 1


class DataError(Exception):
    """带可读中文说明的数据层错误。"""


class DataManager:
    def __init__(self, app_dir: str | Path | None = None,
                 anchor_dir: str | Path | None = None):
        # 程序目录：AI_Studio/
        self.app_dir = Path(app_dir) if app_dir else Path(__file__).resolve().parents[1]
        # 锚点：固定放 mode.json，永远不变
        default_anchor = Path(os.environ.get("APPDATA") or Path.home()) / APP_FOLDER
        self.anchor_dir = Path(anchor_dir) if anchor_dir else default_anchor

        self.mode, self.custom_root = self._load_mode()
        self.root = self._resolve_root()

    # ------------------------------------------------------------------ 模式
    def _resolve_root(self) -> Path:
        if self.mode == MODE_APPDATA:
            return self.anchor_dir / "data"
        if self.mode == MODE_CUSTOM and self.custom_root:
            return Path(self.custom_root)
        return self.app_dir / "data"

    def _load_mode(self) -> tuple[str, str]:
        path = self.anchor_dir / "mode.json"
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                mode = data.get("mode")
                if mode in MODES:
                    return mode, str(data.get("root") or "")
            except (OSError, ValueError):
                pass
        return MODE_PORTABLE, ""

    def save_mode(self) -> None:
        self.anchor_dir.mkdir(parents=True, exist_ok=True)
        payload = {"mode": self.mode, "root": self.custom_root}
        (self.anchor_dir / "mode.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    @property
    def mode_label(self) -> str:
        return MODE_LABELS.get(self.mode, self.mode)

    # ------------------------------------------------------------------ 路径
    @property
    def db_path(self) -> Path:
        return self.root / "conversations.db"

    @property
    def images_dir(self) -> Path:
        return self.root / "images"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    @property
    def config_dir(self) -> Path:
        return self.root

    @property
    def config_file(self) -> Path:
        return self.config_dir / "config.json"

    @property
    def secrets_file(self) -> Path:
        return self.config_dir / "secrets.dat"

    def ensure_ready(self) -> None:
        """建目录 + 试写，尽早暴露"目录不可写"这种问题。"""
        for folder in (self.root, self.images_dir, self.logs_dir):
            try:
                folder.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise DataError(f"无法创建数据目录 {folder}：{exc}") from exc
        probe = self.root / ".write_test"
        try:
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            raise DataError(f"数据目录不可写：{self.root}（{exc}）") from exc

    def describe(self) -> dict:
        return {
            "mode": self.mode,
            "mode_label": self.mode_label,
            "root": str(self.root),
            "db": str(self.db_path),
            "images": str(self.images_dir),
            "db_exists": self.db_path.exists(),
            "image_count": _count_files(self.images_dir),
            "size_mb": round(_tree_size(self.root) / 1024 / 1024, 1),
        }

    # ------------------------------------------------------------------ 切换
    def plan_switch(self, mode: str, custom_root: str | Path | None = None,
                    force: bool = False) -> Path:
        """算出切换后的目标根目录，并做基本校验（不落盘）。"""
        if mode not in MODES:
            raise DataError(f"未知的存储模式：{mode}")
        if mode == MODE_CUSTOM:
            if not custom_root:
                raise DataError("自定义模式需要先选择目录。")
            target = Path(custom_root)
        elif mode == MODE_APPDATA:
            target = self.anchor_dir / "data"
        else:
            target = self.app_dir / "data"

        if target.resolve() == self.root.resolve():
            raise DataError("目标目录和当前目录相同，无需切换。")
        if not force and _looks_like_data_dir(target):
            raise DataError(f"目标目录已有数据，请先清空或换一个目录：{target}")
        return target

    def switch(self, mode: str, custom_root: str | Path | None = None,
               force: bool = False) -> dict:
        """切换存储位置：复制 → 校验 → 切换 → 旧目录原样保留。"""
        target = self.plan_switch(mode, custom_root, force=force)
        report = self._copy_and_verify(target)

        self.mode = mode
        self.custom_root = str(target) if mode == MODE_CUSTOM else ""
        self.root = target
        self.save_mode()
        self.ensure_ready()

        report.update({
            "mode": mode,
            "target": str(target),
            "previous": str(report.get("source")),
            "kept_source": True,
        })
        return report

    def _copy_and_verify(self, target: Path) -> dict:
        source = self.root
        target.mkdir(parents=True, exist_ok=True)

        copied = {"db": False, "images": 0, "config": False, "secrets": False}
        if self.db_path.exists():
            shutil.copy2(self.db_path, target / "conversations.db")
            copied["db"] = True
        if self.images_dir.exists():
            (target / "images").mkdir(parents=True, exist_ok=True)
            for item in self.images_dir.iterdir():
                if item.is_file():
                    shutil.copy2(item, target / "images" / item.name)
                    copied["images"] += 1
        for name in ("config.json", "secrets.dat"):
            src = source / name
            if src.exists():
                shutil.copy2(src, target / name)
                copied["config" if name == "config.json" else "secrets"] = True

        src_counts = _db_counts(self.db_path)
        dst_counts = _db_counts(target / "conversations.db")
        if copied["db"] and src_counts != dst_counts:
            raise DataError(
                f"数据校验失败：源 {src_counts} / 目标 {dst_counts}，已中止切换。"
            )

        expected = _count_files(self.images_dir)
        actual = _count_files(target / "images")
        if expected != actual:
            raise DataError(f"图片校验失败：源 {expected} 张 / 目标 {actual} 张，已中止切换。")

        return {"source": str(source), "messages": dst_counts[0], "images_total": actual,
                "copied": copied}

    # ------------------------------------------------------------------ 备份
    def backup(self, zip_path: str | Path | None = None, include_secrets: bool = False) -> dict:
        if zip_path is None:
            stamp = time.strftime("%Y%m%d_%H%M%S")
            zip_path = self.root / f"AIStudio_backup_{stamp}.zip"
        zip_path = Path(zip_path)
        zip_path.parent.mkdir(parents=True, exist_ok=True)

        counts = _db_counts(self.db_path)
        manifest = {
            "version": BACKUP_VERSION,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "mode": self.mode,
            "root": str(self.root),
            "messages": counts[0],
            "images_rows": counts[1],
            "image_files": _count_files(self.images_dir),
            "include_secrets": bool(include_secrets),
        }

        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
            if self.db_path.exists():
                archive.write(self.db_path, f"{BACKUP_SUBDIR}/conversations.db")
            if self.images_dir.exists():
                for item in sorted(self.images_dir.iterdir()):
                    if item.is_file():
                        archive.write(item, f"{BACKUP_SUBDIR}/images/{item.name}")
            if self.config_file.exists():
                archive.write(self.config_file, f"{BACKUP_SUBDIR}/config/config.json")
            if include_secrets and self.secrets_file.exists():
                archive.write(self.secrets_file, f"{BACKUP_SUBDIR}/config/secrets.dat")
            archive.writestr(
                f"{BACKUP_SUBDIR}/backup.json",
                json.dumps(manifest, ensure_ascii=False, indent=2),
            )

        manifest.update({
            "zip": str(zip_path),
            "size_mb": round(zip_path.stat().st_size / 1024 / 1024, 1),
        })
        return manifest

    def restore(self, zip_path: str | Path) -> dict:
        """从备份包恢复；当前数据先改名保留，校验通过才算成功。"""
        zip_path = Path(zip_path)
        if not zip_path.exists():
            raise DataError(f"找不到备份文件：{zip_path}")

        import tempfile

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            temp = Path(temp_dir)
            try:
                with zipfile.ZipFile(zip_path) as archive:
                    names = archive.namelist()
                    if not any(name.endswith(f"{BACKUP_SUBDIR}/conversations.db") for name in names):
                        raise DataError("这个压缩包不是 AI Studio 的备份（缺少数据库）。")
                    archive.extractall(temp)
            except zipfile.BadZipFile as exc:
                raise DataError(f"备份文件已损坏：{exc}") from exc

            source = temp / BACKUP_SUBDIR
            incoming_db = source / "conversations.db"
            if not incoming_db.exists():
                raise DataError("备份包里没有 conversations.db。")

            expected = _db_counts(incoming_db)
            if expected == (0, 0):
                raise DataError("备份包里的数据库是空的，已中止恢复。")

            stamp = time.strftime("%Y%m%d_%H%M%S")
            self.ensure_ready()
            # 用"复制 .bak + 覆盖"而不是"移动"，避免文件被占用时整体失败
            if self.db_path.exists():
                shutil.copy2(self.db_path, self.db_path.with_name(f"conversations.db.bak-{stamp}"))
            shutil.copy2(incoming_db, self.db_path)

            if (source / "images").exists():
                for item in (source / "images").iterdir():
                    if item.is_file():
                        shutil.copy2(item, self.images_dir / item.name)

            restored_config = source / "config" / "config.json"
            if restored_config.exists():
                backup_config = self.config_file.with_name(f"config.json.bak-{stamp}")
                if self.config_file.exists():
                    shutil.copy2(self.config_file, backup_config)
                shutil.copy2(restored_config, self.config_file)
            restored_secrets = source / "config" / "secrets.dat"
            if restored_secrets.exists():
                shutil.copy2(restored_secrets, self.secrets_file)

            actual = _db_counts(self.db_path)
            if actual != expected:
                raise DataError(f"恢复校验失败：备份 {expected} / 当前 {actual}")

        return {
            "zip": str(zip_path),
            "messages": actual[0],
            "images_rows": actual[1],
            "image_files": _count_files(self.images_dir),
            "backup_kept_at": str(self.db_path.with_name(f"conversations.db.bak-{stamp}")),
        }

    def adopt_legacy_config(self) -> bool:
        """老版本把配置放在 AppData 根目录；搬到新的数据根，避免用户丢配置。"""
        legacy = self.anchor_dir
        if self.config_dir.resolve() == legacy.resolve():
            return False
        moved = False
        for name in ("config.json", "secrets.dat"):
            src = legacy / name
            dst = self.config_dir / name
            if src.exists() and not dst.exists():
                self.config_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                moved = True
        return moved

    def remove_image(self, path: str | Path) -> bool:
        """删除图片文件。只允许删 images 目录里的文件，防误删其它东西。"""
        try:
            target = Path(path).resolve()
            images = self.images_dir.resolve()
        except OSError:
            return False
        if images not in target.parents or not target.is_file():
            return False
        try:
            target.unlink()
            return True
        except OSError:
            return False


# ---------------------------------------------------------------------- 工具
def _count_files(folder: Path) -> int:
    if not folder.exists():
        return 0
    return sum(1 for item in folder.iterdir() if item.is_file())


def _tree_size(folder: Path) -> int:
    if not folder.exists():
        return 0
    return sum(item.stat().st_size for item in folder.rglob("*") if item.is_file())


def _db_counts(db_path: Path) -> tuple[int, int]:
    """(消息数, 图片记录数)；文件不存在返回 (0, 0)。"""
    if not db_path.exists():
        return 0, 0
    try:
        with sqlite3.connect(str(db_path)) as conn:
            messages = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            try:
                images = conn.execute("SELECT COUNT(*) FROM images").fetchone()[0]
            except sqlite3.Error:
                images = 0
        return int(messages), int(images)
    except sqlite3.Error:
        return 0, 0


def _looks_like_data_dir(folder: Path) -> bool:
    if not folder.exists():
        return False
    if (folder / "conversations.db").exists():
        return True
    images = folder / "images"
    return images.exists() and _count_files(images) > 0


_default_manager: DataManager | None = None


def default_manager() -> DataManager:
    global _default_manager
    if _default_manager is None:
        _default_manager = DataManager()
    return _default_manager
