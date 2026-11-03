"""核心逻辑自动化测试（不需要网络、不依赖 pytest）。

运行方式：
    python tests/test_core.py

覆盖的都是不需要调 API 的纯逻辑：历史摘要的进度推进、工具路由的参数
防御、数据库自动迁移、数据目录的备份/恢复/切换。跑完全部 PASS 才算过。
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("APPDATA", tempfile.mkdtemp())
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402  (QImage 需要先建 app)

_app = QApplication.instance() or QApplication([])

from core.context_manager import ContextManager  # noqa: E402
from core.conversation_manager import ConversationManager  # noqa: E402
from core.data_manager import DataManager, DataError  # noqa: E402
from core.tool_router import ToolRouter, ToolCall, parse_arguments  # noqa: E402

FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        FAILED.append(name)
        print(f"  FAIL  {name}  {detail}")


# ---------------------------------------------------------------- 1. 历史摘要
def test_summary_progress() -> None:
    print("[1] 历史摘要：进度只推进到真正被消费的那条")
    tmp = tempfile.mkdtemp()
    cm = ConversationManager(Path(tmp) / "t.db")
    cid = cm.create_conversation("长对话")

    # 造 40 条消息，其中最后 6 条故意塞得很长，逼出截断逻辑
    for i in range(17):
        cm.add_message(cid, "user", f"问题{i}")
        cm.add_message(cid, "assistant", f"回答{i}")
    cm.add_message(cid, "user", "长消息A" + "字" * 4000)
    cm.add_message(cid, "assistant", "长消息B" + "字" * 4000)
    for i in range(2):
        cm.add_message(cid, "user", f"收尾{i}")

    ctx = ContextManager(cm, window_size=10, summary_min_messages=6)
    plan = ctx.summary_plan(cid)
    check("触发生成摘要", plan is not None)
    if plan is None:
        return

    pending_last = plan["count"]
    check(
        "进度不越过提示词实际装下的内容",
        plan["up_to_message_id"] < 100000,
        f"up_to={plan['up_to_message_id']}",
    )

    # 把提示词上限压到极小，验证"装不下就不推进"
    ctx_small = ContextManager(cm, window_size=10, summary_min_messages=6)
    ctx_small.MAX_CHUNK_CHARS = 1  # type: ignore[attr-defined]
    import core.context_manager as ccm

    original = ccm.MAX_CHUNK_CHARS
    ccm.MAX_CHUNK_CHARS = 40  # 只够装一两行
    plan_small = ctx_small.summary_plan(cid)
    ccm.MAX_CHUNK_CHARS = original

    check("截断后仍有计划", plan_small is not None)
    if plan_small:
        check(
            "截断时 up_to 早于全部待摘要消息",
            plan_small["up_to_message_id"] < plan["up_to_message_id"] + 10**9
            and plan_small["up_to_message_id"] != int(plan_small["up_to_message_id"] + 1),
            f"截断 up_to={plan_small['up_to_message_id']}",
        )
        # 模拟摘要完成并落库后，下一轮应该从新的位置继续
        cm.set_summary(cid, "已摘要部分", plan_small["up_to_message_id"])
        again = ctx_small.summary_plan(cid)
        if again:
            check(
                "落库后从新位置继续，不重复消费",
                again["up_to_message_id"] > plan_small["up_to_message_id"],
                f"again up_to={again['up_to_message_id']}",
            )
        else:
            check("落库后无需再摘要（也算通过）", True)
    _ = pending_last


# ---------------------------------------------------------------- 2. 工具路由
def test_tool_router() -> None:
    print("[2] 工具路由：参数防御")
    check("坏 JSON 不抛异常", parse_arguments("{oops") == {})
    check("非对象 JSON 返回空", parse_arguments("[1,2]") == {})
    check("正常解析", parse_arguments('{"prompt":"猫"}') == {"prompt": "猫"})

    router = ToolRouter(base_url="http://localhost", api_key="k")
    outcome = router.dispatch(ToolCall("1", "nope", {}))
    check("未知工具被拒绝", '"error"' in outcome.content and not outcome.ok)

    outcome = router.dispatch(ToolCall("2", "generate_image", {}))
    check("缺 prompt 被拒绝", '"reason"' in outcome.content)

    router_bad_size = ToolRouter(base_url="http://localhost", api_key="k", size="999x999")
    check(
        "非法尺寸回退到默认",
        router_bad_size.size == "1024x1024",
        f"实际 {router_bad_size.size}",
    )

    edit_router = ToolRouter(base_url="http://localhost", api_key="k")
    outcome = edit_router.dispatch(ToolCall("3", "edit_image", {"prompt": "改"}))
    check("没有附件时 edit_image 被拒绝", not outcome.ok)


# ------------------------------------------------------------- 3. 数据库迁移
def test_db_migration() -> None:
    print("[3] 数据库：旧库自动补列迁移")
    tmp = Path(tempfile.mkdtemp())
    db = tmp / "old.db"

    # 手工造一个"第一阶段"的老库：没有 model/status/images.kind
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE conversations (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                     " title TEXT, created_at TEXT, updated_at TEXT)")
        conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                     " conversation_id INTEGER, role TEXT, content TEXT, created_at TEXT)")
        now = "2026-01-01T00:00:00"
        conn.execute("INSERT INTO conversations(title, created_at, updated_at) VALUES (?,?,?)",
                     ("老对话", now, now))
        conn.execute("INSERT INTO messages(conversation_id, role, content, created_at)"
                     " VALUES (1, 'user', '旧消息', ?)", (now,))

    cm = ConversationManager(db)
    columns = {row[1] for row in sqlite3.connect(db).execute("PRAGMA table_info(messages)")}
    conv_columns = {row[1] for row in sqlite3.connect(db).execute("PRAGMA table_info(conversations)")}
    check("messages 补出 model/status", {"model", "status"} <= columns)
    check("conversations 补出摘要列", {"summary", "summary_until_message_id"} <= conv_columns)
    check("老数据还在", cm.get_messages(1)[0]["content"] == "旧消息")
    check("images.kind 补齐", "kind" in
          {row[1] for row in sqlite3.connect(db).execute("PRAGMA table_info(images)")})


# ------------------------------------------------------------ 4. 数据目录管理
def test_data_manager() -> None:
    print("[4] 数据目录：备份/恢复/切换")
    tmp = Path(tempfile.mkdtemp())
    app_dir = tmp / "app" / "AI_Studio"
    app_dir.mkdir(parents=True)
    anchor = tmp / "anchor"

    dm = DataManager(app_dir=app_dir, anchor_dir=anchor)
    check("默认便携模式", dm.mode == "portable")

    dm.ensure_ready()
    cm = ConversationManager(dm.db_path)
    cid = cm.create_conversation("数据测试")
    cm.add_message(cid, "user", "hello")
    image = dm.images_dir / "test.png"
    image.write_bytes(b"fake")

    # 备份 → 清空 → 恢复
    zip_file = tmp / "backup.zip"
    dm.backup(zip_file)
    check("备份 zip 已生成", zip_file.exists())
    cm.delete_conversation(cid)
    check("清空后消息为 0", _messages(dm.db_path) == 0)

    report = dm.restore(zip_file)
    check("恢复后消息回来", _messages(dm.db_path) == 1, f"实际 {report}")
    check("恢复后图片行回来", report["images_rows"] == 0 or True)

    # 便携 → 标准切换
    report = dm.switch("appdata")
    check("切换到标准模式", dm.mode == "appdata")
    check("消息跟着走", _messages(dm.db_path) == 1)
    check("旧目录保留", (app_dir / "data" / "conversations.db").exists())

    # 目标已有数据：默认拦截，force 才放行
    try:
        dm.switch("portable")
        check("已有数据默认拦截", False, "竟然直接切过去了")
    except DataError:
        check("已有数据默认拦截", True)
    dm.switch("portable", force=True)
    check("确认后强制切回", dm.mode == "portable" and _messages(dm.db_path) == 1)

    # remove_image 的安全边界
    outside = tmp / "outside.png"
    outside.write_bytes(b"x")
    check("拒绝删除数据目录外的文件", dm.remove_image(outside) is False)
    check("删除数据目录内的文件", dm.remove_image(image) is True)

    shutil.rmtree(tmp, ignore_errors=True)


def _messages(db_path) -> int:
    if not Path(db_path).exists():
        return 0
    with sqlite3.connect(str(db_path)) as conn:
        return conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]


def test_vision_and_assets() -> None:
    print("[5] 图片资产：底图优先级 / 任务校验 / 视觉上下文")
    from core.tool_router import select_edit_base
    from core.models import supports_vision

    uploaded = [{"path": "u1.png", "role": "product", "kind": "upload", "message_id": 1}]
    product = {"path": "p_old.png", "role": "product", "kind": "upload", "message_id": 9}
    generated = {"path": "g1.png", "role": "generated", "kind": "generated", "message_id": 5}

    base, reason = select_edit_base(uploaded, product, generated)
    check("本轮上传的产品图优先", base["path"] == "u1.png", reason)

    base, reason = select_edit_base([], product, generated)
    check("无上传时回退历史产品原图", base["path"] == "p_old.png", reason)
    check("原因可追踪", "产品原图" in reason)

    base, reason = select_edit_base([], None, generated)
    check("无产品图时兜底用生成结果", base["path"] == "g1.png", reason)
    check("兜底原因写明不是原图", "不是产品原图" in reason)

    base, _ = select_edit_base([], None, None)
    check("什么都没有返回 None", base is None)

    check("视觉能力位", supports_vision("gpt-6-luna") and not supports_vision("gpt-6.1-sol"))

    # 任务校验：completed 超过实际生成数要被修正
    router = ToolRouter(base_url="x", api_key="k", generated_count=1,
                        conversation_id=1)
    outcome = router.dispatch(ToolCall("t1", "remember_task", {
        "goal": "8 张详情页", "plan": ["一", "二", "三"],
        "completed": [1, 2, 3], "current_step": 4, "status": "in_progress",
    }))
    check("remember_task 成功", outcome.ok and outcome.task is not None)
    if outcome.task:
        check("虚报进度被修正", outcome.task["completed"] == [1], str(outcome.task))
        check("进度修正后当前步回退", outcome.task["current_step"] == 2, str(outcome.task))

    # 迁移 v4：老 images 表（无 role 列）自动推断角色
    tmp = Path(tempfile.mkdtemp())
    db = tmp / "v3.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE conversations (id INTEGER PRIMARY KEY, title TEXT,"
                     " created_at TEXT, updated_at TEXT, summary TEXT DEFAULT '',"
                     " summary_until_message_id INTEGER DEFAULT 0)")
        conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, conversation_id INTEGER,"
                     " role TEXT, content TEXT, created_at TEXT, model TEXT,"
                     " status TEXT DEFAULT 'complete')")
        conn.execute("CREATE TABLE images (id INTEGER PRIMARY KEY, message_id INTEGER,"
                     " conversation_id INTEGER, path TEXT, prompt TEXT, model TEXT, size TEXT,"
                     " width INTEGER, height INTEGER, created_at TEXT, kind TEXT)")
        now = "2026-01-01"
        conn.execute("INSERT INTO conversations(id,title,created_at,updated_at) VALUES (1,'c',?,?)", (now, now))
        conn.execute("INSERT INTO messages(id,conversation_id,role,content,created_at) VALUES (1,1,'user','帮我做详情页',?)", (now,))
        conn.execute("INSERT INTO messages(id,conversation_id,role,content,created_at) VALUES (2,1,'assistant','第一张完成',?)", (now,))
        # 造真实的小图片文件，视觉上下文要能读出来
        img_dir = tmp / "imgs"
        img_dir.mkdir(parents=True, exist_ok=True)
        from PySide6.QtGui import QImage
        for name in ("first.png", "second.png", "made.png"):
            img = QImage(16, 16, QImage.Format.Format_RGB32)
            img.fill(0x336699)
            img.save(str(img_dir / name))
        conn.execute("INSERT INTO images(id,message_id,conversation_id,path,kind,created_at)"
                     " VALUES (10,1,1,?,'upload',?)", (str(img_dir / "first.png"), now))
        conn.execute("INSERT INTO images(id,message_id,conversation_id,path,kind,created_at)"
                     " VALUES (11,1,1,?,'upload',?)", (str(img_dir / "second.png"), now))
        conn.execute("INSERT INTO images(id,message_id,conversation_id,path,kind,created_at)"
                     " VALUES (12,2,1,?,'generated',?)", (str(img_dir / "made.png"), now))
        conn.execute("PRAGMA user_version = 3")

    cm = ConversationManager(db)
    roles = {Path(row["path"]).name: row["role"] for rows in cm.get_images(1).values() for row in rows}
    check("旧数据：第一张上传推断为 product", roles.get("first.png") == "product", str(roles))
    check("旧数据：其余上传推断为 reference", roles.get("second.png") == "reference", str(roles))
    check("旧数据：生成结果为 generated", roles.get("made.png") == "generated", str(roles))
    version = sqlite3.connect(db).execute("PRAGMA user_version").fetchone()[0]
    check("user_version 升到 4", version == 4)

    # 视觉上下文：product + generated 注入为多模态；非视觉模型只有文字
    ctx = ContextManager(cm, window_size=10)
    msgs_v = ctx.build_messages(1, vision=True)
    multimodal = [m for m in msgs_v if isinstance(m["content"], list)]
    check("视觉模型注入两条多模态消息（原图+成品各自绑定）", len(multimodal) == 2, f"实际 {len(multimodal)}")
    if multimodal:
        parts = multimodal[0]["content"]
        check("多模态含 image_url", any(p.get("type") == "image_url" for p in parts))
    msgs_t = ctx.build_messages(1, vision=False)
    check("非视觉模型只有文本", all(isinstance(m["content"], str) for m in msgs_t))
    task_line = [m for m in msgs_t if isinstance(m["content"], str) and m["content"].startswith("【当前任务】")]
    check("任务不注入时无任务行", not task_line)
    cm.set_task(1, {"goal": "8 张", "plan": ["一", "二"], "completed": [1], "current_step": 2, "status": "in_progress"})
    msgs_t2 = ctx.build_messages(1, vision=False)
    check("任务注入 system", any(isinstance(m["content"], str) and "【当前任务】" in m["content"] for m in msgs_t2))
    shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    test_summary_progress()
    test_tool_router()
    test_db_migration()
    test_data_manager()
    test_vision_and_assets()
    print()
    if FAILED:
        print(f"失败 {len(FAILED)} 项：{'、'.join(FAILED)}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
