#!/usr/bin/env python3
"""真实触发一次告警与恢复，验证显示时间。"""

import sys

sys.path.insert(0, "/root/echomate-deploy/monitor")

import monitor

KEY = "tzcheck:test"

print("=== 触发告警 ===")
monitor.evaluate(
    key=KEY,
    title="服务状态：echomate-server.service",
    abnormal=True,
    details="时区验证",
    min_count=1,
    min_duration=0,
)

print("\n=== 触发恢复 ===")
monitor.evaluate(
    key=KEY,
    title="服务状态：echomate-server.service",
    abnormal=False,
    details="时区验证恢复",
    min_count=1,
    min_duration=0,
)

print("\n=== 清理状态 ===")
import sqlite3

conn = sqlite3.connect("/var/lib/echomate/assistant.db", timeout=15)
conn.execute(
    "DELETE FROM monitor_state WHERE alert_key = ?", (KEY,)
)
conn.execute(
    "DELETE FROM notify_outbox WHERE dedupe_key LIKE 'monitor:tzcheck%'"
)
conn.commit()
print("done")
