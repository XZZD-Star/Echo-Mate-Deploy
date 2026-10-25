#!/usr/bin/env python3
"""修恢复口语：指标类用“恢复正常”。"""

from pathlib import Path

MONITOR = Path("/root/echomate-deploy/monitor/monitor.py")

OLD = '''    if recovery:
        return f"{label}好了。"

    # 指标类标题本身已含“不足/过高”，不再拼“异常”。
    if label.endswith(("不足", "过高", "过低", "已满")):
        return f"注意，{label}。"

    return f"注意，VPS 的{label}异常。"
'''

NEW = '''    metric = label.endswith(("不足", "过高", "过低", "已满"))

    if recovery:
        return f"{label}，恢复正常。" if metric else f"{label}好了。"

    # 指标类标题本身已含“不足/过高”，不再拼“异常”。
    if metric:
        return f"注意，{label}。"

    return f"注意，VPS 的{label}异常。"
'''

text = MONITOR.read_text(encoding="utf-8")

if NEW in text:
    print("already applied")
else:
    if text.count(OLD) != 1:
        raise SystemExit(f"匹配失败：命中 {text.count(OLD)} 次")
    MONITOR.write_text(text.replace(OLD, NEW), encoding="utf-8")
    print("patched")
