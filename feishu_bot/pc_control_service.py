"""Background dispatch and result delivery for local Codex jobs."""

import threading
import time

import pc_control_client
import pc_control_store
import pc_pages


_health_lock = threading.Lock()
_last_health = (0.0, False, {"error": "not checked"})
HEALTH_CACHE_SECONDS = 8.0


def refresh_health(touch=True):
    global _last_health
    try:
        data = pc_control_client.health()
    except pc_control_client.CompanionError as exc:
        snapshot = (time.monotonic(), False, {"error": str(exc)})
        with _health_lock:
            _last_health = snapshot
        return snapshot[1], snapshot[2]

    if touch:
        try:
            pc_control_store.touch_device(
                data.get("device_id") or "windows-main",
                data.get("display_name") or "Windows PC",
                data.get("app_version") or "",
                data.get("protocol_version") or 1,
            )
        except Exception:
            pass
    snapshot = (time.monotonic(), True, data)
    with _health_lock:
        _last_health = snapshot
    return snapshot[1], snapshot[2]


def health_snapshot():
    with _health_lock:
        stamp, online, data = _last_health
    if time.monotonic() - stamp > HEALTH_CACHE_SECONDS:
        return False, {"error": "health check expired"}
    return online, data


def _payload_for(job):
    session = (
        pc_control_store.get_session(job["session_id"])
        if job["session_id"] is not None
        else None
    )
    return {
        "job_id": job["remote_job_id"],
        "prompt": job["prompt"],
        "mode": job["kind"],
        "profile_id": job["profile_id"],
        "codex_thread_id": (
            session["codex_thread_id"]
            if session is not None and job["kind"] == "fixed"
            else None
        ),
    }


def _finish_job(job, status, result_summary=None, error_code=None,
                thread_id=None, notify=None):
    updated = pc_control_store.update_job_status(
        job["id"],
        status,
        result_summary=result_summary,
        error_code=error_code,
        codex_thread_id=thread_id,
    )
    if updated["status"] != status:
        return
    print(
        f"[本机Codex] 任务 #{job['id']} 状态={status}",
        flush=True,
    )
    if job["session_id"] is not None:
        if status == "succeeded" and thread_id:
            try:
                pc_control_store.set_session_thread(
                    job["session_id"], thread_id
                )
            except Exception as exc:
                pc_control_store.set_session_error(
                    job["session_id"],
                    f"thread_save_failed:{type(exc).__name__}",
                )
        elif status in {"failed", "interrupted", "expired", "uncertain"}:
            pc_control_store.set_session_error(
                job["session_id"], error_code or status
            )
    if notify is not None:
        try:
            notify(job["chat_id"], pc_pages.completion_message(updated))
        except Exception as exc:
            print(
                f"[本机Codex] 结果通知失败：{type(exc).__name__}",
                flush=True,
            )


def _advance_queued_job(job, notify):
    online, health = health_snapshot()
    if not online:
        return

    expected_protocol = pc_control_client.PROTOCOL_VERSION
    if int(health.get("protocol_version") or 0) < expected_protocol:
        _finish_job(
            job,
            "failed",
            error_code="protocol_mismatch",
            notify=notify,
        )
        return
    if health.get("device_id") != job["device_id"]:
        _finish_job(
            job,
            "failed",
            error_code="device_mismatch",
            notify=notify,
        )
        return

    try:
        accepted = pc_control_client.submit_job(_payload_for(job))
    except pc_control_client.CompanionUncertain:
        _finish_job(
            job,
            "uncertain",
            error_code="submit_response_lost",
            notify=notify,
        )
        return
    except pc_control_client.CompanionUnavailable:
        _finish_job(
            job,
            "failed",
            error_code="companion_unreachable",
            notify=notify,
        )
        return
    except pc_control_client.CompanionError as exc:
        _finish_job(
            job,
            "failed",
            error_code=f"submit_failed:{exc}",
            notify=notify,
        )
        return

    remote_status = accepted.get("status")
    status = "running" if remote_status == "running" else "dispatched"
    pc_control_store.update_job_status(job["id"], status)
    print(
        f"[本机Codex] 任务 #{job['id']} 已发送到 Companion",
        flush=True,
    )


def _advance_remote_job(job, notify):
    try:
        remote = pc_control_client.get_job(job["remote_job_id"])
    except pc_control_client.CompanionUnavailable:
        return
    except pc_control_client.CompanionUncertain:
        return
    except pc_control_client.CompanionError:
        return

    if remote is None:
        _finish_job(
            job,
            "uncertain",
            error_code="remote_job_missing",
            notify=notify,
        )
        return

    remote_status = remote.get("status")
    if remote_status in {"accepted", "dispatched"}:
        if job["status"] != "dispatched":
            pc_control_store.update_job_status(
                job["id"], "dispatched"
            )
        return
    if remote_status == "running":
        if job["status"] != "running":
            pc_control_store.update_job_status(job["id"], "running")
        return
    if remote_status == "succeeded":
        _finish_job(
            job,
            "succeeded",
            result_summary=remote.get("result") or "任务已完成。",
            thread_id=remote.get("thread_id"),
            notify=notify,
        )
        return
    if remote_status == "interrupted":
        _finish_job(
            job,
            "interrupted",
            result_summary=remote.get("result") or "任务已中断。",
            error_code="remote_interrupted",
            notify=notify,
        )
        return

    _finish_job(
        job,
        "failed",
        result_summary=remote.get("result"),
        error_code=remote.get("error") or "remote_failed",
        notify=notify,
    )


def _process_cancel_requests(notify):
    for job in pc_control_store.list_cancel_requests(limit=10):
        if job["status"] == "queued":
            _finish_job(
                job,
                "interrupted",
                result_summary="任务在发送到本机前已中断。",
                error_code="user_cancelled",
                notify=notify,
            )
            continue
        try:
            response = pc_control_client.cancel_job(
                job["remote_job_id"]
            )
        except pc_control_client.CompanionUnavailable:
            continue
        except pc_control_client.CompanionUncertain:
            _finish_job(
                job,
                "uncertain",
                result_summary="中断请求结果不确定。",
                error_code="cancel_response_lost",
                notify=notify,
            )
            continue
        except pc_control_client.CompanionError:
            continue

        if response is None:
            _finish_job(
                job,
                "uncertain",
                result_summary="本机找不到正在执行的任务。",
                error_code="cancel_remote_missing",
                notify=notify,
            )
            continue
        if response.get("status") == "interrupted":
            _finish_job(
                job,
                "interrupted",
                result_summary=response.get("result")
                or "任务已按你的要求中断。",
                error_code="user_cancelled",
                notify=notify,
            )
        else:
            _advance_remote_job(job, notify)


def worker_loop(notify, poll_seconds=1.0):
    print("[本机Codex] 调度线程已启动", flush=True)
    last_health_check = 0.0
    last_device_touch = 0.0
    while True:
        try:
            now = time.monotonic()
            if now - last_health_check >= 5.0:
                should_touch = now - last_device_touch >= 30.0
                online, _ = refresh_health(touch=should_touch)
                if online and should_touch:
                    last_device_touch = now
                last_health_check = now
            _process_cancel_requests(notify)
            jobs = pc_control_store.list_pollable_jobs(limit=10)
            for job in jobs:
                if job["status"] == "queued":
                    _advance_queued_job(job, notify)
                else:
                    _advance_remote_job(job, notify)
        except Exception as exc:
            print(
                f"[本机Codex] 调度异常：{type(exc).__name__}: {exc}",
                flush=True,
            )
        time.sleep(max(0.2, float(poll_seconds)))


def start_worker(notify):
    thread = threading.Thread(
        target=worker_loop,
        args=(notify,),
        name="pc-control-worker",
        daemon=True,
    )
    thread.start()
    return thread
