import time

from echomate_core import store as echomate_store

from .messaging import send_text
from .time_utils import format_due_friendly

def reminder_loop():
    print("[待办] 飞书提醒投递线程已启动，检查间隔 15 秒", flush=True)

    while True:
        try:
            delivery = echomate_store.claim_delivery("feishu")

            if delivery:
                todo_id = delivery["todo_id"]
                title = delivery["title"]
                chat_id = delivery["chat_id"]
                delivery_id = delivery["delivery_id"]

                if not chat_id:
                    # A deskbot-created todo has no Feishu chat target.
                    # Keep the shared record, but this channel cannot deliver it.
                    echomate_store.mark_delivery(
                        delivery_id, "skipped", "no Feishu chat_id"
                    )
                    echomate_store.finalize_todo_if_delivered(todo_id)
                else:
                    due_display = format_due_friendly(delivery["due_at_utc"])
                    content = (
                        f"⏰ {title}\n\n"
                        f"{due_display}\n\n"
                        "完成后打开待办清单操作"
                    )
                    try:
                        sent = send_text(chat_id, content)
                    except Exception as exc:
                        print(
                            f"[待办] #{todo_id} 发送异常：{type(exc).__name__}",
                            flush=True,
                        )
                        sent = False

                    if sent:
                        echomate_store.mark_delivery(delivery_id, "sent")
                        echomate_store.finalize_todo_if_delivered(todo_id)
                    else:
                        echomate_store.mark_delivery(
                            delivery_id, "failed", "Feishu send failed"
                        )

            time.sleep(15)

        except Exception as exc:
            print(
                f"[待办] 调度异常：{type(exc).__name__}",
                flush=True,
            )
            time.sleep(5)
