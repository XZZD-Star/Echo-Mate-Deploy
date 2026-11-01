import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parent))

import pc_control_store
import pc_control_service
import pc_pages
import pc_routing


class PcControlStoreTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "assistant.db"
        pc_control_store.init_schema(self.db)
        self.old_db = pc_control_store.DB_PATH
        pc_control_store.DB_PATH = self.db

    def tearDown(self):
        pc_control_store.DB_PATH = self.old_db
        self.temp.cleanup()

    def test_job_event_is_idempotent(self):
        first = pc_control_store.create_job(
            "owner",
            "chat",
            None,
            "device",
            "default",
            "single",
            "hello",
            "event-1",
        )
        second = pc_control_store.create_job(
            "owner",
            "chat",
            None,
            "device",
            "default",
            "single",
            "hello again",
            "event-1",
        )
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(
            pc_control_store.list_history("owner", "chat")[0]["prompt"],
            "hello",
        )

    def test_session_activation_keeps_two_recoverable_sessions(self):
        ids = []
        for index in range(3):
            session_id = pc_control_store.create_session(
                "owner",
                "chat",
                "device",
                "default",
                f"session-{index}",
            )
            pc_control_store.activate_session(
                "owner", "chat", session_id
            )
            ids.append(session_id)

        recoverable = pc_control_store.list_recoverable_sessions(
            "owner", "chat"
        )
        self.assertEqual(len(recoverable), 2)
        self.assertEqual(
            {row["id"] for row in recoverable},
            set(ids[-2:]),
        )
        self.assertEqual(
            pc_control_store.get_session(ids[0])["lifecycle"],
            "archived",
        )

    def test_active_job_can_be_scoped_to_fixed_session(self):
        session_id = pc_control_store.create_session(
            "owner", "chat", "device", "default", "fixed"
        )
        pc_control_store.create_job(
            "owner",
            "chat",
            session_id,
            "device",
            "default",
            "fixed",
            "fixed prompt",
            "event-fixed",
        )
        pc_control_store.create_job(
            "owner",
            "chat",
            None,
            "device",
            "default",
            "single",
            "single prompt",
            "event-single",
        )
        active = pc_control_store.get_active_job(
            "owner", "chat", session_id=session_id
        )
        self.assertEqual(active["prompt"], "fixed prompt")
        device_active = pc_control_store.get_active_job_for_device(
            "device"
        )
        self.assertEqual(device_active["prompt"], "single prompt")

    def test_terminal_status_updates_turn(self):
        job = pc_control_store.create_job(
            "owner",
            "chat",
            None,
            "device",
            "default",
            "single",
            "prompt",
            "event-status",
        )
        updated = pc_control_store.update_job_status(
            job["id"],
            "succeeded",
            result_summary="done",
        )
        self.assertEqual(updated["status"], "succeeded")
        self.assertIsNotNone(updated["finished_at_utc"])

    def test_cancel_request_is_persisted_without_network_call(self):
        job = pc_control_store.create_job(
            "owner",
            "chat",
            None,
            "device",
            "default",
            "single",
            "prompt",
            "event-cancel",
        )
        self.assertTrue(pc_control_store.request_job_cancel(job["id"]))
        requests = pc_control_store.list_cancel_requests()
        self.assertEqual([row["id"] for row in requests], [job["id"]])

    def test_queued_cancel_is_finalized_by_worker(self):
        job = pc_control_store.create_job(
            "owner",
            "chat",
            None,
            "device",
            "default",
            "single",
            "prompt",
            "event-cancel-worker",
        )
        pc_control_store.request_job_cancel(job["id"])
        notices = []
        pc_control_service._process_cancel_requests(
            lambda chat_id, message: notices.append((chat_id, message))
        )
        current = pc_control_store.get_job(job["id"])
        self.assertEqual(current["status"], "interrupted")
        self.assertEqual(len(notices), 1)

    def test_running_cancel_calls_companion(self):
        job = pc_control_store.create_job(
            "owner",
            "chat",
            None,
            "device",
            "default",
            "single",
            "prompt",
            "event-cancel-running",
        )
        pc_control_store.update_job_status(job["id"], "running")
        pc_control_store.request_job_cancel(job["id"])
        with patch.object(
            pc_control_service.pc_control_client,
            "cancel_job",
            return_value={"status": "interrupted", "result": "stopped"},
        ) as cancel_job:
            pc_control_service._process_cancel_requests(
                lambda _chat_id, _message: None
            )
        cancel_job.assert_called_once()
        self.assertEqual(
            pc_control_store.get_job(job["id"])["status"],
            "interrupted",
        )

    def test_startup_pauses_all_active_sessions(self):
        session_id = pc_control_store.create_session(
            "owner", "chat", "device", "default", "fixed"
        )
        pc_control_store.activate_session("owner", "chat", session_id)
        self.assertEqual(
            pc_control_store.pause_all_active_sessions(),
            1,
        )
        self.assertEqual(
            pc_control_store.get_session(session_id)["lifecycle"],
            "paused",
        )


class PcRoutingTest(unittest.TestCase):
    def test_regular_echo_pages_keep_echo_route(self):
        for page in ("menu", "todo", "todo_new", "memory", "help"):
            self.assertEqual(pc_routing.route_mode(page), "echo")

    def test_pc_control_and_capture_pages_do_not_fall_back_to_echo(self):
        self.assertEqual(pc_routing.route_mode("pc"), "pc_control")
        self.assertEqual(
            pc_routing.route_mode("pc_history"),
            "pc_control",
        )
        self.assertEqual(
            pc_routing.route_mode("pc_single"),
            "pc_capture",
        )
        self.assertEqual(
            pc_routing.route_mode("pc_fixed_new"),
            "pc_capture",
        )
        self.assertEqual(
            pc_routing.route_mode("pc_fixed"),
            "pc_fixed_capture",
        )


class PcUserFacingCopyTest(unittest.TestCase):
    def setUp(self):
        self.job = {
            "id": 7,
            "status": "succeeded",
            "prompt": "查看电脑 CPU 和内存占用",
            "result_summary": "一切正常",
            "error_code": None,
            "created_at_utc": "2026-10-30T03:00:00+00:00",
            "finished_at_utc": "2026-10-30T03:01:00+00:00",
        }

    def test_history_and_detail_hide_internal_job_id(self):
        history = pc_pages.render_pc_history([self.job])
        detail = pc_pages.render_pc_history_detail(self.job)
        status = pc_pages.render_pc_job_status(self.job)
        completion = pc_pages.completion_message(self.job)

        for text in (history, detail, status, completion):
            self.assertNotIn("#", text)
        self.assertIn("1 · 查看电脑 CPU 和内存占用", history)
        self.assertIn("🖥 本机任务", detail)


if __name__ == "__main__":
    unittest.main()
