#!/usr/bin/env python3
"""预览监控告警、恢复、开机报告、SSH 连接/断开的新文案。"""

import sys

sys.path.insert(0, "/root/echomate-deploy/monitor")

import monitor
import ssh_session_watch


def show(title, text):
    print("=" * 52)
    print(f"【{title}】")
    print("-" * 52)
    print(text)
    print()


show("告警文案（语音服务）", monitor.spoken_alert_text(
    "服务状态：echomate-server.service", "服务不活跃"))
show("恢复文案（语音服务）", monitor.spoken_alert_text(
    "服务状态：echomate-server.service", "服务运行正常", recovery=True))
show("告警文案（飞书）", monitor.spoken_alert_text(
    "服务状态：echomate-feishu-bot.service", "服务不活跃"))
show("告警文案（内存）", monitor.spoken_alert_text(
    "可用内存不足", "可用内存：200 MiB"))
show("告警文案（磁盘）", monitor.spoken_alert_text(
    "根分区空间不足", "使用率：90%"))

show("SSH 连接报告", ssh_session_watch.make_connect_message())
show("SSH 断开", "（由 send_disconnect_message 发送，内容如下）")

print("=" * 52)
print("【SSH 断开实际文案】")
print("-" * 52)
from datetime import datetime
now = datetime.now(ssh_session_watch.LOCAL_TZ)
print("🔴 断开 · 后台还在跑")
print()
print(f"{now:%H:%M}")
