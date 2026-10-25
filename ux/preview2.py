#!/usr/bin/env python3
"""渲染文案，每个命令用独立连接，避免自己持有写锁。"""

import sqlite3
import sys
import types

PATH = "/root/echomate-deploy/feishu_bot/bot.py"
DB = "/var/lib/echomate/assistant.db"
CHAT = "oc_5b3d6c4b7569f760854044c0c5b7ba0d"

src = open(PATH, encoding="utf-8").read()
cut = src.index("# ==================== 飞书消息发送 ====================")

m = types.ModuleType("bot_head")
m.__file__ = PATH
exec(compile(src[:cut], PATH, "exec"), m.__dict__)


def show(title, text):
    print("=" * 52)
    print(f"【{title}】")
    print("-" * 52)
    print(text)
    print()


def cmd(text):
    conn = sqlite3.connect(DB, timeout=15)
    try:
        result = m.handle_command(conn, text, CHAT)
        conn.commit()
        return result
    finally:
        conn.close()


show("菜单", m.menu_text())
show("帮助", m.help_text())
show("VPS 状态", m.vps_status_text())
show("播报设置", m.notify_settings_text())
show("播报记录", m.notify_records_text())

show("待办列表（空）", cmd("我的待办"))
show("新待办提示", cmd("添加待办"))
show("错误：时间已过", cmd("添加待办 2020-01-01 10:00 测试"))
show("错误：格式错", cmd("添加待办 2026/10/25 10:00 测试"))
show("错误：无此待办", cmd("完成待办 999"))

show("记忆列表", cmd("列出记忆"))
show("错误：查无记忆", cmd("查记忆 不存在的词"))
show("错误：删除无此记忆", cmd("删除记忆 999"))

show("静音格式错", cmd("静音 25:00 08:00"))
show("静音起止相同", cmd("静音 10:00 10:00"))
show("静音成功", cmd("静音 23:00 08:00"))
show("关闭静音", cmd("关闭静音"))
show("关闭待办播报", cmd("关闭待办播报"))
show("开启待办播报", cmd("开启待办播报"))

show("创建待办", cmd("添加待办 2026-10-26 09:30 交周报"))
show("待办列表", cmd("我的待办"))

conn = sqlite3.connect(DB, timeout=15)
row = conn.execute(
    "SELECT id FROM todos WHERE title='交周报' ORDER BY id DESC LIMIT 1"
).fetchone()
conn.close()

if row:
    show("完成待办", cmd(f"完成待办 {row[0]}"))
    conn = sqlite3.connect(DB, timeout=15)
    conn.execute("DELETE FROM todos WHERE id = ?", (row[0],))
    conn.commit()
    conn.close()

print("预览完成")
