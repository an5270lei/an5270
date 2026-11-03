"""SQLite 对话管理：会话、消息的创建、读取、更新和删除。

第 2 阶段新增（幂等迁移，不影响旧数据）：

- ``messages.model``：这条 AI 回复实际使用的模型 ID；
- ``messages.status``：``complete`` / ``stopped`` / ``error``，用于区分
  被停止或失败的回复。
"""

import json
import shutil
import sqlite3
import time
from datetime import datetime
from pathlib import Path


class ConversationManager:
    """SQLite 对话管理：会话、消息的创建、读取、更新和删除。"""

    def __init__(self, db_path=None):
        project_root = Path(__file__).resolve().parents[1]
        self.db_path = Path(db_path) if db_path else project_root / "data" / "conversations.db"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize_database()

    def _connect(self):
        conn = sqlite3.connect(str(self.db_path), timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _initialize_database(self):
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS conversations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL DEFAULT '新对话',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id INTEGER NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('system', 'user', 'assistant', 'tool')),
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(conversation_id) REFERENCES conversations(id)
                        ON DELETE CASCADE
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_messages_conversation
                ON messages(conversation_id, id)
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS images (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id INTEGER NOT NULL,
                    conversation_id INTEGER NOT NULL,
                    path TEXT NOT NULL,
                    prompt TEXT,
                    model TEXT,
                    size TEXT,
                    width INTEGER DEFAULT 0,
                    height INTEGER DEFAULT 0,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(message_id) REFERENCES messages(id) ON DELETE CASCADE,
                    FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_images_message
                ON images(message_id, id)
            """)
            self._migrate(conn)

    def _migrate(self, conn):
        """按列补齐方式迁移，重复执行无副作用。

        v3 → v4 涉及图片资产角色，属于破坏性较强的结构变更：
        迁移前先把整个数据库文件备份一份，失败也不会留下半迁移状态
        （所有 ALTER 都在同一个事务里，连接关闭时统一提交/回滚）。
        """
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version < 4 and self.db_path.exists():
            stamp = time.strftime("%Y%m%d_%H%M%S")
            backup = self.db_path.with_name(f"conversations.db.bak-v4-{stamp}")
            if not backup.exists():
                shutil.copy2(self.db_path, backup)

        message_columns = {row["name"] for row in conn.execute("PRAGMA table_info(messages)")}
        if "model" not in message_columns:
            conn.execute("ALTER TABLE messages ADD COLUMN model TEXT")
        if "status" not in message_columns:
            conn.execute(
                "ALTER TABLE messages ADD COLUMN status TEXT NOT NULL DEFAULT 'complete'"
            )

        conversation_columns = {row["name"] for row in conn.execute("PRAGMA table_info(conversations)")}
        if "summary" not in conversation_columns:
            conn.execute("ALTER TABLE conversations ADD COLUMN summary TEXT DEFAULT ''")
        if "summary_until_message_id" not in conversation_columns:
            conn.execute(
                "ALTER TABLE conversations ADD COLUMN summary_until_message_id INTEGER DEFAULT 0"
            )
        if "task_json" not in conversation_columns:
            conn.execute("ALTER TABLE conversations ADD COLUMN task_json TEXT DEFAULT ''")

        image_columns = {row["name"] for row in conn.execute("PRAGMA table_info(images)")}
        if "kind" not in image_columns:
            conn.execute("ALTER TABLE images ADD COLUMN kind TEXT DEFAULT 'generated'")
        if "role" not in image_columns:
            conn.execute("ALTER TABLE images ADD COLUMN role TEXT DEFAULT ''")
        if "role_source" not in image_columns:
            conn.execute("ALTER TABLE images ADD COLUMN role_source TEXT DEFAULT ''")
        if "purpose" not in image_columns:
            conn.execute("ALTER TABLE images ADD COLUMN purpose TEXT DEFAULT ''")

        # 旧数据角色推断：上传的第一张 → 产品主体，其余上传 → 参考图。
        # role_source='inferred' 表明这是系统推测，不是用户明确指定的。
        if True:
            conn.execute("""
                UPDATE images SET role = 'generated', role_source = 'inferred'
                WHERE (kind IS NULL OR kind != 'upload') AND (role IS NULL OR role = '')
            """)
            conn.execute("""
                UPDATE images SET role = 'reference', role_source = 'inferred'
                WHERE kind = 'upload' AND (role IS NULL OR role = '')
                  AND id NOT IN (
                      SELECT MIN(id) FROM images
                      WHERE kind = 'upload'
                      GROUP BY conversation_id
                  )
            """)
            conn.execute("""
                UPDATE images SET role = 'product', role_source = 'inferred'
                WHERE kind = 'upload' AND (role IS NULL OR role = '')
            """)

        conn.execute("PRAGMA user_version = 4")

    @staticmethod
    def _now():
        return datetime.now().astimezone().isoformat(timespec="seconds")

    # ------------------------------------------------------------------ 会话
    def create_conversation(self, title="新对话"):
        now = self._now()
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO conversations(title, created_at, updated_at) VALUES (?, ?, ?)",
                (title, now, now)
            )
            return cursor.lastrowid

    def list_conversations(self):
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT id, title, created_at, updated_at
                FROM conversations
                ORDER BY updated_at DESC, id DESC
            """).fetchall()
            return [dict(row) for row in rows]

    def get_conversation(self, conversation_id):
        with self._connect() as conn:
            row = conn.execute(
                """SELECT id, title, created_at, updated_at, summary,
                          summary_until_message_id
                   FROM conversations WHERE id = ?""",
                (conversation_id,)
            ).fetchone()
            return dict(row) if row else None

    # ------------------------------------------------------------------ 摘要
    def get_summary(self, conversation_id):
        """返回 (摘要文本, 已覆盖到哪条消息 id)。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT summary, summary_until_message_id FROM conversations WHERE id = ?",
                (conversation_id,)
            ).fetchone()
        if not row:
            return "", 0
        return (row["summary"] or ""), int(row["summary_until_message_id"] or 0)

    def set_summary(self, conversation_id, summary, until_message_id):
        with self._connect() as conn:
            conn.execute(
                """UPDATE conversations
                   SET summary = ?, summary_until_message_id = ?
                   WHERE id = ?""",
                (summary or "", int(until_message_id or 0), conversation_id)
            )

    def update_title(self, conversation_id, title):
        title = (title or "").strip() or "新对话"
        with self._connect() as conn:
            conn.execute(
                "UPDATE conversations SET title = ?, updated_at = ? WHERE id = ?",
                (title[:60], self._now(), conversation_id)
            )

    def delete_conversation(self, conversation_id):
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM conversations WHERE id = ?",
                (conversation_id,)
            )
            return cursor.rowcount > 0

    # ------------------------------------------------------------------ 消息
    def add_message(self, conversation_id, role, content, model=None, status="complete"):
        if role not in ("system", "user", "assistant", "tool"):
            raise ValueError(f"不支持的消息角色：{role}")
        now = self._now()
        with self._connect() as conn:
            cursor = conn.execute("""
                INSERT INTO messages(conversation_id, role, content, created_at, model, status)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (conversation_id, role, content, now, model, status))
            conn.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (now, conversation_id)
            )
            return cursor.lastrowid

    def get_messages(self, conversation_id):
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT id, conversation_id, role, content, created_at, model, status
                FROM messages
                WHERE conversation_id = ?
                ORDER BY id ASC
            """, (conversation_id,)).fetchall()
            return [dict(row) for row in rows]

    def count_messages(self, conversation_id):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM messages WHERE conversation_id = ?",
                (conversation_id,)
            ).fetchone()
            return int(row["n"]) if row else 0

    # ------------------------------------------------------------------ 图片
    def add_image(self, message_id, conversation_id, path, prompt="", model="", size="",
                  width=0, height=0, kind="generated", role="generated",
                  role_source="inferred", purpose=""):
        with self._connect() as conn:
            cursor = conn.execute("""
                INSERT INTO images(message_id, conversation_id, path, prompt, model, size,
                                   width, height, created_at, kind, role, role_source, purpose)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (message_id, conversation_id, str(path), prompt, model, size,
                  int(width or 0), int(height or 0), self._now(), kind,
                  role, role_source, purpose))
            return cursor.lastrowid

    def latest_image_by_role(self, conversation_id, role):
        """某个角色最近的一张图（如 product=产品原图）。"""
        with self._connect() as conn:
            row = conn.execute("""
                SELECT id, message_id, path, role, purpose, created_at
                FROM images
                WHERE conversation_id = ? AND role = ?
                ORDER BY id DESC LIMIT 1
            """, (conversation_id, role)).fetchone()
        return dict(row) if row else None

    def count_uploads(self, conversation_id) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM images WHERE conversation_id = ? AND kind = 'upload'",
                (conversation_id,)
            ).fetchone()
        return int(row["n"]) if row else 0

    # ------------------------------------------------------------------ 任务
    def get_task(self, conversation_id) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT task_json FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
        if not row or not row["task_json"]:
            return None
        try:
            data = json.loads(row["task_json"])
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def set_task(self, conversation_id, task: dict) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE conversations SET task_json = ? WHERE id = ?",
                (json.dumps(task, ensure_ascii=False, indent=2), conversation_id),
            )

    def latest_upload_image(self, conversation_id):
        """最近一张用户上传的产品图（改图时的默认底图）。"""
        with self._connect() as conn:
            row = conn.execute("""
                SELECT path FROM images
                WHERE conversation_id = ? AND kind = 'upload'
                ORDER BY id DESC LIMIT 1
            """, (conversation_id,)).fetchone()
        return row["path"] if row else None

    def get_images(self, conversation_id):
        """返回 {message_id: [图片行, ...]}，按插入顺序。"""
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT id, message_id, path, prompt, model, size, width, height,
                       created_at, kind, role, role_source, purpose
                FROM images
                WHERE conversation_id = ?
                ORDER BY id ASC
            """, (conversation_id,)).fetchall()
        grouped: dict[int, list] = {}
        for row in rows:
            grouped.setdefault(row["message_id"], []).append(dict(row))
        return grouped
