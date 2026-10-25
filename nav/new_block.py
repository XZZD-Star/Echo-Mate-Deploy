# ==================== 页面导航 ====================

PAGES = {
    "menu", "status", "status_detail", "todo", "todo_new",
    "memory", "help", "notify", "quiet", "records",
}

# 0 返回上一级；未列出的页面回到主菜单。
PAGE_PARENT = {
    "status_detail": "status",
    "quiet": "notify",
    "records": "notify",
}

SERVICE_UNITS = [
    ("echomate-server.service", "语音", "echomate-server"),
    ("echomate-feishu-bot.service", "飞书", "echomate-feishu-bot"),
    ("echomate-monitor.service", "监控", "echomate-monitor"),
]

PAGE_TTL_SECONDS = 86400


def page_key(chat_id):
    return f"menu:{chat_id}"


def get_page(chat_id):
    """读取当前页面；超过 24 小时未活动则回到主菜单。"""
    raw = notify_store.get_setting(page_key(chat_id), "")
    if not raw:
        return "menu"

    try:
        data = json.loads(raw)
        page = data.get("page", "menu")
        stamp = datetime.fromisoformat(data["at"])
    except (ValueError, KeyError, TypeError):
        return "menu"

    if page not in PAGES:
        return "menu"

    if (
        datetime.now(timezone.utc) - stamp
    ).total_seconds() > PAGE_TTL_SECONDS:
        return "menu"

    return page


def set_page(chat_id, page):
    if page not in PAGES:
        page = "menu"
    notify_store.set_setting(
        page_key(chat_id),
        json.dumps({"page": page, "at": now_utc()}),
    )


def _service_states():
    """返回 [(中文标签, systemd 状态, unit 简名), ...]。"""
    result = []
    for unit, label, short in SERVICE_UNITS:
        try:
            proc = subprocess.run(
                ["systemctl", "is-active", unit],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            state = (proc.stdout or "").strip() or "unknown"
        except (OSError, subprocess.TimeoutExpired):
            state = "unknown"
        result.append((label, state, short))
    return result


def _uptime_text():
    uptime = timedelta(
        seconds=max(0, int(time.time() - psutil.boot_time()))
    )
    if uptime.days:
        return f"{uptime.days} 天"
    return f"{uptime.seconds // 3600} 小时"


def _boot_text():
    boot = datetime.fromtimestamp(
        psutil.boot_time(), tz=timezone.utc
    ).astimezone(LOCAL_TZ)
    if boot.year == datetime.now(LOCAL_TZ).year:
        return boot.strftime("%m-%d %H:%M")
    return boot.strftime("%Y-%m-%d")


def menu_text():
    return (
        "🐣 Echo\n\n"
        "1 · 📊 状态\n"
        "2 · 📋 待办\n"
        "3 · ➕ 新待办\n"
        "4 · 📚 记忆\n"
        "5 · 💡 帮助\n"
        "6 · 🔊 播报\n\n"
        "直接发数字就行"
    )


def render_status():
    cpu = psutil.cpu_percent(interval=0.3)
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    states = _service_states()
    down = [label for label, state, _ in states if state != "active"]

    if down:
        head = "🟡 有服务掉线"
    elif cpu > 90 or mem.available < 250 * 1024**2:
        head = "🟡 有点忙"
    else:
        head = "📊 一切正常"

    service_line = " · ".join(
        f"{label} {'🟢' if state == 'active' else '🔴'}"
        for label, state, _ in states
    )

    lines = [
        head,
        "",
        f"CPU {cpu:.0f}% · 内存 {mem.available / 1024**2:.0f}M"
        f" · 磁盘 {disk.free / 1024**3:.0f}G",
        f"已跑 {_uptime_text()} · 开机 {_boot_text()}",
        "",
        service_line,
    ]

    if down:
        lines.append("掉线：" + "、".join(down))

    lines.append("")
    lines.append("1 刷新 · 2 详情 · 0 返回")
    return "\n".join(lines)


def render_status_detail():
    cpu = psutil.cpu_percent(interval=0.3)
    mem = psutil.virtual_memory()
    swap = psutil.swap_memory()
    disk = psutil.disk_usage("/")
    boot = datetime.fromtimestamp(
        psutil.boot_time(), tz=timezone.utc
    ).astimezone(LOCAL_TZ)
    states = _service_states()

    lines = [
        "🔍 状态详情",
        "",
        f"🖥 {os.uname().nodename}",
        f"⏱ 开机 {boot:%Y-%m-%d %H:%M:%S}",
        f"📈 已跑 {_uptime_text()}",
        "",
        f"CPU {cpu:.0f}% · {psutil.cpu_count()} 核",
        f"内存 {mem.used / 1024**2:.0f}M / {mem.total / 1024**3:.1f}G",
        f"Swap {swap.used / 1024**2:.0f}M / {swap.total / 1024**3:.1f}G",
        f"磁盘 {disk.used / 1024**3:.0f}G / {disk.total / 1024**3:.0f}G",
        "",
    ]

    for label, state, short in states:
        icon = "🟢" if state == "active" else "🔴"
        lines.append(f"{label} {icon} {state} · {short}")

    lines.extend(["", "1 刷新 · 0 返回"])
    return "\n".join(lines)


def _todo_rows(conn, limit=20):
    return conn.execute(
        """
        SELECT id, title, due_at_utc
        FROM todos
        WHERE owner_id = ? AND status = 'pending'
        ORDER BY due_at_utc ASC
        LIMIT ?
        """,
        (OWNER_OPEN_ID, limit),
    ).fetchall()


def render_todo(conn):
    rows = _todo_rows(conn)

    if not rows:
        return (
            "📋 今天很轻松\n\n"
            "没有待办，去歇会儿吧\n\n"
            "3 新建 · 0 返回"
        )

    lines = [f"📋 你有 {len(rows)} 件事", ""]
    for todo_id, title, due_utc in rows:
        lines.append(
            f"#{todo_id} · {format_due_friendly(due_utc)} · {title}"
        )

    first_id = rows[0][0]
    lines.extend([
        "",
        f"完成 → 完成待办 {first_id}",
        "3 新建 · 0 返回",
    ])
    return "\n".join(lines)


def render_todo_new():
    return (
        "➕ 建个待办\n\n"
        "照着发：\n"
        "添加待办 2026-10-26 09:30 交周报\n\n"
        "时间用 24 小时制，北京时间\n\n"
        "0 返回"
    )


def _memory_rows(conn, limit):
    return conn.execute(
        """
        SELECT id, content
        FROM memories
        WHERE owner_id = ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (OWNER_OPEN_ID, limit),
    ).fetchall()


def render_memory(conn, show_all=False):
    total = conn.execute(
        "SELECT COUNT(*) FROM memories WHERE owner_id = ?",
        (OWNER_OPEN_ID,),
    ).fetchone()[0]

    if not total:
        return (
            "📚 记忆 · 0 条\n\n"
            "还没有记忆\n\n"
            "存：记住：喜欢简短回答\n\n"
            "0 返回"
        )

    rows = _memory_rows(conn, 30 if show_all else 3)

    lines = [f"📚 记忆 · {total} 条", ""]
    for memory_id, content in rows:
        lines.append(f"#{memory_id} · {content[:60]}")

    if not show_all and total > len(rows):
        lines.append(f"…还有 {total - len(rows)} 条")

    lines.extend([
        "",
        "查：查记忆 回答",
        "存：记住：喜欢简短回答",
    ])

    if show_all:
        lines.extend(["", "0 返回"])
    else:
        lines.extend(["", "1 全部 · 0 返回"])

    return "\n".join(lines)


def render_help():
    return (
        "💡 Echo 会这些\n\n"
        "📋 待办\n"
        "添加待办 2026-10-26 09:30 交周报\n"
        "我的待办 · 完成待办 6\n\n"
        "📚 记忆\n"
        "存：记住：喜欢简短回答\n"
        "查：查记忆 回答\n\n"
        "🔊 播报\n"
        "静音 23:00 08:00 · 关闭静音\n\n"
        "0 返回"
    )


def _quiet_state():
    start = notify_store.get_setting("notify_quiet_start", "")
    end = notify_store.get_setting("notify_quiet_end", "")
    if _normalize_hhmm(start) is None or _normalize_hhmm(end) is None:
        return "未开启", False
    return f"{start}–{end}", True


def render_notify():
    todo_on = notify_store.get_setting("notify_todo_enabled", "1") == "1"
    monitor_on = (
        notify_store.get_setting("notify_monitor_enabled", "1") == "1"
    )
    quiet_text, _ = _quiet_state()
    pending = notify_store.pending_count()

    lines = [
        "🔊 播报设置",
        "",
        f"🌙 静音时段　{quiet_text}",
        f"📋 待办播报　{'开启 ●' if todo_on else '未开启'}",
        f"📡 监控播报　{'开启 ●' if monitor_on else '未开启'}",
        f"📤 待播通知　{pending} 条",
        "",
        "⚙️ 操作",
        "1 静音设置",
        f"2 {'关闭' if todo_on else '开启'}待办播报",
        f"3 {'关闭' if monitor_on else '开启'}监控播报",
        "4 播报记录",
        "",
        "0 返回",
    ]
    return "\n".join(lines)


def render_quiet():
    quiet_text, enabled = _quiet_state()

    if enabled:
        return (
            "🌙 静音设置\n\n"
            f"当前　{quiet_text}\n"
            "该时段桌宠不播报，飞书通知不受影响\n\n"
            "修改：静音 23:00 08:00\n"
            "取消：关闭静音\n\n"
            "0 返回"
        )

    return (
        "🌙 静音设置\n\n"
        "当前　未开启\n"
        "设置后，该时段桌宠不播报，飞书通知不受影响\n\n"
        "设置：静音 23:00 08:00\n"
        "取消：关闭静音\n\n"
        "0 返回"
    )


def render_records():
    rows = notify_store.recent_records(10)

    if not rows:
        return "📢 播报记录\n\n还没有记录\n\n0 返回"

    status_label = {
        "pending": "待播",
        "claimed": "播放中",
        "sent": "已播",
        "failed": "失败",
        "expired": "过期",
    }

    lines = ["📢 播报记录", ""]
    for row in rows:
        status = status_label.get(row["status"], row["status"])
        if row["status"] == "sent":
            mark = f"{status} {'✓' if row['acked_at_utc'] else '·'}"
        elif row["last_error"]:
            mark = f"{status} · {row['last_error'][:20]}"
        else:
            mark = status
        lines.append(f"#{row['id']} · {_kind_label(row['kind'])} · {mark}")

    lines.extend(["", "0 返回"])
    return "\n".join(lines)


def render_page(conn, chat_id, page):
    if page == "menu":
        return menu_text()
    if page == "status":
        return render_status()
    if page == "status_detail":
        return render_status_detail()
    if page == "todo":
        return render_todo(conn)
    if page == "todo_new":
        return render_todo_new()
    if page == "memory":
        return render_memory(conn)
    if page == "help":
        return render_help()
    if page == "notify":
        return render_notify()
    if page == "quiet":
        return render_quiet()
    if page == "records":
        return render_records()
    return menu_text()


def _normalize_hhmm(value):
    """把 9:05 这类写法规范成 09:05，非法返回 None。"""
    try:
        hour, minute = value.split(":", 1)
        hour = int(hour)
        minute = int(minute)
    except (TypeError, ValueError):
        return None

    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None

    return f"{hour:02d}:{minute:02d}"


def _kind_label(kind):
    return {
        "todo": "待办",
        "alert": "告警",
        "recovery": "恢复",
        "manual": "测试",
    }.get(kind, kind)


def _enter(conn, chat_id, page):
    set_page(chat_id, page)
    return render_page(conn, chat_id, page)


def _toggle(kind_key, target):
    current = notify_store.get_setting(kind_key, "1")
    new_value = "0" if current == "1" else "1"
    notify_store.set_setting(kind_key, new_value)
    state_text = "关闭" if new_value == "0" else "开启"
    return f"✅ {target}播报{state_text}\n\n6 看播报 · 0 返回"


def _handle_page_number(conn, chat_id, page, text):
    """页面内的数字指令。返回 None 表示不归本页处理。"""
    if page == "menu":
        target = {
            "1": "status",
            "2": "todo",
            "3": "todo_new",
            "4": "memory",
            "5": "help",
            "6": "notify",
        }.get(text)
        if target:
            return _enter(conn, chat_id, target)

    elif page == "status":
        if text == "1":
            return render_status()
        if text == "2":
            return _enter(conn, chat_id, "status_detail")

    elif page == "status_detail":
        if text == "1":
            return render_status_detail()

    elif page == "todo":
        if text == "3":
            return _enter(conn, chat_id, "todo_new")

    elif page == "memory":
        if text == "1":
            return render_memory(conn, show_all=True)

    elif page == "notify":
        if text == "1":
            return _enter(conn, chat_id, "quiet")
        if text == "2":
            return _toggle("notify_todo_enabled", "待办")
        if text == "3":
            return _toggle("notify_monitor_enabled", "监控")
        if text == "4":
            return _enter(conn, chat_id, "records")

    return None


def handle_command(conn, text, chat_id):
    text = text.strip()
    page = get_page(chat_id)

    # -------- 全局导航 --------

    if text in {"菜单", "功能"}:
        return _enter(conn, chat_id, "menu")

    if text == "0":
        parent = PAGE_PARENT.get(page, "menu")
        return _enter(conn, chat_id, parent)

    # -------- 页面内数字 --------

    if text in {"1", "2", "3", "4", "5", "6"}:
        handled = _handle_page_number(conn, chat_id, page, text)
        if handled is not None:
            return handled

    # -------- 直接进入页面的文字命令 --------

    if text in {
        "检查 VPS", "检查VPS", "VPS 状态", "VPS状态",
        "服务器状态", "状态",
    }:
        return _enter(conn, chat_id, "status")

    if text in {"状态详情", "详情"}:
        return _enter(conn, chat_id, "status_detail")

    if text == "帮助":
        return _enter(conn, chat_id, "help")

    if text in {"播报设置", "语音播报设置"}:
        return _enter(conn, chat_id, "notify")

    if text in {"播报记录", "语音播报记录"}:
        return _enter(conn, chat_id, "records")

    if text in {"我的待办", "待办列表", "查看待办", "待办"}:
        return _enter(conn, chat_id, "todo")

    if text in {"列出记忆", "我的记忆", "记忆列表", "记忆"}:
        return _enter(conn, chat_id, "memory")

    if text in {"添加待办", "新建待办"}:
        return _enter(conn, chat_id, "todo_new")

    # -------- 静音设置 --------

    match = re.match(
        r"^静音\s+(\d{1,2}:\d{2})\s+(\d{1,2}:\d{2})\s*$",
        text,
    )

    if match:
        start_text, end_text = match.groups()
        start = _normalize_hhmm(start_text)
        end = _normalize_hhmm(end_text)

        if start is None or end is None:
            return "😅 时间写成 23:00 08:00 这样\n\n6 看播报 · 0 返回"

        if start == end:
            return "😅 开始和结束不能一样\n\n6 看播报 · 0 返回"

        notify_store.set_setting("notify_quiet_start", start)
        notify_store.set_setting("notify_quiet_end", end)

        return (
            f"🔇 静音 {start}–{end}\n\n"
            "这段桌宠闭嘴，飞书照常\n\n"
            "6 看播报 · 0 返回"
        )

    if text in {"关闭静音", "取消静音"}:
        notify_store.set_setting("notify_quiet_start", "")
        notify_store.set_setting("notify_quiet_end", "")
        return "🔔 静音关掉了\n\n6 看播报 · 0 返回"

    match = re.match(r"^(关闭|开启)(待办|监控)播报$", text)

    if match:
        action, target = match.groups()
        key = (
            "notify_todo_enabled"
            if target == "待办"
            else "notify_monitor_enabled"
        )
        notify_store.set_setting(key, "1" if action == "开启" else "0")
        state_text = "开启" if action == "开启" else "关闭"
        return f"✅ {target}播报{state_text}\n\n6 看播报 · 0 返回"

    # -------- 创建待办 --------

    if text.startswith(("添加待办", "新建待办")) and not re.match(
        r"^添加待办\s+\d{4}-\d{2}-\d{2}\s+"
        r"\d{2}:\d{2}\s+.+$",
        text,
        re.DOTALL,
    ):
        return (
            "😅 格式差一点\n\n"
            "添加待办 2026-10-26 09:30 交周报\n\n"
            "3 重填 · 0 返回"
        )

    match = re.match(
        r"^添加待办\s+(\d{4}-\d{2}-\d{2})\s+"
        r"(\d{2}:\d{2})\s+(.+)$",
        text,
        re.DOTALL,
    )

    if match:
        date_text, time_text, title = match.groups()
        title = title.strip()

        if not title:
            return "😅 待办得写点内容\n\n3 重填 · 0 返回"

        if len(title) > 200:
            return "😅 标题太长，200 字以内\n\n3 重填 · 0 返回"

        try:
            due_local = datetime.strptime(
                f"{date_text} {time_text}", "%Y-%m-%d %H:%M"
            ).replace(tzinfo=LOCAL_TZ)
        except ValueError:
            return "😅 日期写成 2026-10-26 09:30 这样\n\n3 重填 · 0 返回"

        if due_local <= datetime.now(LOCAL_TZ):
            return "😅 这个时间已经过去了\n\n3 重填 · 0 返回"

        due_utc = due_local.astimezone(timezone.utc).isoformat(
            timespec="seconds"
        )

        cursor = conn.execute(
            """
            INSERT INTO todos
                (owner_id, title, due_at_utc, timezone,
                 status, created_at_utc, chat_id)
            VALUES (?, ?, ?, ?, 'pending', ?, ?)
            """,
            (
                OWNER_OPEN_ID,
                title,
                due_utc,
                "Asia/Shanghai",
                now_utc(),
                chat_id,
            ),
        )

        return (
            f"✅ 记下了 #{cursor.lastrowid}\n\n"
            f"{title} · {format_due_friendly(due_utc)}\n"
            "到点我会提醒你\n\n"
            "2 看待办 · 0 返回"
        )

    # -------- 完成或取消待办 --------

    match = re.match(
        r"^(完成待办|取消待办)\s*[:：#]?\s*(\d+)\s*$",
        text,
    )

    if match:
        action, todo_id_text = match.groups()
        todo_id = int(todo_id_text)
        new_status = "completed" if action == "完成待办" else "cancelled"

        cursor = conn.execute(
            """
            UPDATE todos
            SET status = ?
            WHERE id = ? AND owner_id = ? AND status = 'pending'
            """,
            (new_status, todo_id, OWNER_OPEN_ID),
        )

        if not cursor.rowcount:
            return (
                f"😅 没有 #{todo_id} 这个待办\n\n"
                "发 2 看看有哪些\n\n"
                "2 看待办 · 0 返回"
            )

        if action == "取消待办":
            return f"✅ #{todo_id} 取消\n\n2 看待办 · 0 返回"

        remaining = conn.execute(
            """
            SELECT COUNT(*) FROM todos
            WHERE owner_id = ? AND status = 'pending'
            """,
            (OWNER_OPEN_ID,),
        ).fetchone()[0]

        if not remaining:
            return (
                "🎉 全部完成\n\n"
                "今天没有待办\n\n"
                "3 新建 · 0 返回"
            )

        return (
            f"✅ #{todo_id} 完成\n\n"
            f"还有 {remaining} 件事，加油 💪\n\n"
            "2 看待办 · 0 返回"
        )

    # -------- 保存记忆 --------

    match = re.match(
        r"^(?:记住|保存记忆)\s*[:：]\s*(.+)$",
        text,
        re.DOTALL,
    )

    if match:
        content = match.group(1).strip()

        if not content:
            return "😅 要记的内容是空的\n\n4 看记忆 · 0 返回"

        if len(content) > 2000:
            return "😅 太长了，2000 字以内\n\n4 看记忆 · 0 返回"

        cursor = conn.execute(
            """
            INSERT INTO memories (owner_id, content, created_at)
            VALUES (?, ?, ?)
            """,
            (OWNER_OPEN_ID, content, now_utc()),
        )

        return (
            f"✅ 记下了 #{cursor.lastrowid}\n\n"
            f"{content[:150]}\n\n"
            "4 看记忆 · 0 返回"
        )

    # -------- 搜索记忆 --------

    match = re.match(
        r"^(?:查记忆|搜索记忆)\s*[:：]?\s*(.+)$",
        text,
        re.DOTALL,
    )

    if match:
        keyword = match.group(1).strip()

        rows = conn.execute(
            """
            SELECT id, content
            FROM memories
            WHERE owner_id = ? AND content LIKE ?
            ORDER BY id DESC
            LIMIT 10
            """,
            (OWNER_OPEN_ID, f"%{keyword}%"),
        ).fetchall()

        if not rows:
            return (
                f"😅 没找到「{keyword}」\n\n"
                "1 全部 · 0 返回"
            )

        lines = [f"🔎 找到 {len(rows)} 条", ""]
        for memory_id, content in rows:
            lines.append(f"#{memory_id} · {content[:120]}")
        lines.extend(["", "4 看记忆 · 0 返回"])
        return "\n".join(lines)

    # -------- 删除记忆 --------

    match = re.match(r"^删除记忆\s*[:：#]?\s*(\d+)\s*$", text)

    if match:
        memory_id = int(match.group(1))
        cursor = conn.execute(
            "DELETE FROM memories WHERE id = ? AND owner_id = ?",
            (memory_id, OWNER_OPEN_ID),
        )

        if cursor.rowcount:
            return f"✅ #{memory_id} 删掉了\n\n4 看记忆 · 0 返回"

        return f"😅 没有 #{memory_id}\n\n4 看记忆 · 0 返回"

    return None


