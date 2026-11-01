import tempfile
import time
from pathlib import Path

import pc_control_client
import pc_control_service
import pc_control_store


def notify(_chat_id, message):
    notifications.append(message)


def drive_once(job):
    if job["status"] == "queued":
        pc_control_service._advance_queued_job(job, notify)
    else:
        pc_control_service._advance_remote_job(job, notify)


with tempfile.TemporaryDirectory() as temp_dir:
    pc_control_store.DB_PATH = Path(temp_dir) / "assistant.db"
    pc_control_store.init_schema()

    online, health = pc_control_service.refresh_health()
    if not online:
        raise SystemExit(f"companion offline: {health}")
    if pc_control_client.PROTOCOL_VERSION != int(
        health.get("protocol_version") or 0
    ):
        raise SystemExit("protocol mismatch")

    job = pc_control_store.create_job(
        "owner",
        "chat",
        None,
        "windows-main",
        "default",
        "single",
        "Reply with exactly BRIDGE_OK. Do not use tools.",
        "bridge-event-1",
    )
    notifications = []

    for _ in range(180):
        current = pc_control_store.get_job(job["id"])
        if current["status"] in pc_control_store.TERMINAL_JOB_STATES:
            break
        drive_once(current)
        time.sleep(1)

    current = pc_control_store.get_job(job["id"])
    print(
        "status={status} result={result} thread={thread}".format(
            status=current["status"],
            result=current["result_summary"],
            thread=current["codex_thread_id"],
        )
    )
    if current["status"] != "succeeded":
        raise SystemExit("bridge job did not succeed")
    if current["result_summary"] != "BRIDGE_OK":
        raise SystemExit("bridge result mismatch")
    if not current["codex_thread_id"]:
        raise SystemExit("missing Codex thread id")
