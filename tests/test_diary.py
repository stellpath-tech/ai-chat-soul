import json
import os
import tempfile
import threading
import time
import unittest
from contextlib import closing
from datetime import datetime, timezone
from unittest.mock import call, patch

from prometheus_client import generate_latest

import channel.web.database as db
from channel.web.diary import service
from channel.web.diary import storage
from channel.web.diary import styles
from channel.web.diary import worker
from channel.web import web_channel


class DiaryDatabaseTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_db_path = db.DB_PATH
        db.DB_PATH = os.path.join(self.temp_dir.name, "soul.db")
        db.init_db()
        with closing(db.get_db()) as conn:
            conn.execute("""
                INSERT INTO user
                (phone_number, invite_code, user_group, auth_token, account_status,
                 tz_iana, tz_offset_min, created_at, updated_at)
                VALUES ('+8613800000000', 'test', 0, 'token', 'active',
                        'Asia/Shanghai', 480, '2026-07-10 00:00:00', '2026-07-10 00:00:00')
            """)
            self.user_id = conn.execute("SELECT id FROM user").fetchone()["id"]
            conn.execute("""
                INSERT INTO user_chat_message
                (user_id, session_id, role, content, message_type, created_at)
                VALUES (?, 'session', 'user', '今天去散步了', 'text', '2026-07-09 12:00:00')
            """, (self.user_id,))
            conn.commit()

    def tearDown(self):
        db.DB_PATH = self.old_db_path
        self.temp_dir.cleanup()

    def test_job_claim_complete_and_detail(self):
        job = db.create_or_reset_diary_job(self.user_id, "2026-07-09")
        claimed = db.claim_diary_job(self.user_id, "2026-07-09")
        self.assertEqual(job["id"], claimed["id"])
        messages = db.list_chat_messages_in_window(
            self.user_id, "2026-07-09 00:00:00", "2026-07-10 00:00:00",
        )
        self.assertEqual(["今天去散步了"], [message["content"] for message in messages])
        db.complete_diary_job(
            claimed["id"], "散步", "我把今天的风轻轻记下来了", "今天去散步了",
            ["https://example.test/diary.png"], "", "normal", [str(messages[0]["id"])], "hash",
        )
        timestamp_ms = int(datetime(2026, 7, 9, 12, tzinfo=timezone.utc).timestamp() * 1000)
        detail = db.get_user_diary_detail(self.user_id, timestamp_ms)
        self.assertEqual("DONE", detail["state"])
        self.assertEqual("散步", detail["title"])
        self.assertEqual(["https://example.test/diary.png"], detail["imageUrls"])

    def test_failed_job_retries_then_skips(self):
        job = db.create_or_reset_diary_job(self.user_id, "2026-07-08")
        self.assertEqual("GENERATING", db.fail_diary_job(job["id"], "first", max_retries=2))
        self.assertEqual("SKIPPED", db.fail_diary_job(job["id"], "second", max_retries=2))

    def test_image_style_defaults_updates_and_is_snapshotted_per_diary(self):
        self.assertEqual("warm_healing", db.get_user_diary_image_style(self.user_id))
        self.assertTrue(db.update_user_diary_image_style(self.user_id, "pixel_art"))
        first = db.create_or_reset_diary_job(self.user_id, "2026-07-08")
        self.assertTrue(db.update_user_diary_image_style(self.user_id, "lego_style"))
        second = db.create_or_reset_diary_job(self.user_id, "2026-07-07")
        with closing(db.get_db()) as conn:
            first_style = conn.execute(
                "SELECT diary_image_style FROM user_diary WHERE id = ?", (first["id"],),
            ).fetchone()["diary_image_style"]
            second_style = conn.execute(
                "SELECT diary_image_style FROM user_diary WHERE id = ?", (second["id"],),
            ).fetchone()["diary_image_style"]
        self.assertEqual("pixel_art", first_style)
        self.assertEqual("lego_style", second_style)

    def test_image_style_put_endpoint_validates_and_saves(self):
        web_channel.web.ctx.env = {"HTTP_X_AUTH_TOKEN": "token"}
        handler = web_channel.DiaryImageStyleHandler()
        with patch.object(web_channel.web, "header"), patch.object(
            web_channel.web, "data", return_value=b'{"style":"chinese_ink"}',
        ):
            self.assertEqual({"success": True}, json.loads(handler.PUT()))
        self.assertEqual("chinese_ink", db.get_user_diary_image_style(self.user_id))

        with patch.object(web_channel.web, "header"), patch.object(
            web_channel.web, "data", return_value=b'{"style":"unknown"}',
        ):
            self.assertEqual({"success": False}, json.loads(handler.PUT()))
        self.assertEqual("chinese_ink", db.get_user_diary_image_style(self.user_id))

    def test_full_text_generation_writes_diary_and_card(self):
        db.update_user_bear_nickname(self.user_id, "团团")
        db.create_or_reset_diary_job(self.user_id, "2026-07-09")
        key_moment_output = "1：用户把今天散步的小事分享给了满仓"
        diary_output = (
            "今天听见你说去散步了，我就把那阵轻轻的风收进谷仓的小抽屉里。"
            "路边晃动的叶子也被我悄悄记住，和暖暖的光放在了一起。\n\n"
            "等你下次回来时，我们再一起看看，我会把软软的位置一直留在这里。"
            "普通的小事被认真讲给我听以后，也会变成值得珍藏的小满足。"
        )
        settings = {
            "diary_quiet_message_threshold": 0,
            "diary_v29_min_chars": 20,
            "diary_v29_max_chars": 700,
            "diary_image_enabled": False,
            "diary_max_retries": 3,
        }
        configured = lambda name, default=None: settings.get(name, default)
        user = db.list_active_users_for_diary()[0]
        with patch.object(service, "_configured", side_effect=configured), patch.object(
            service, "_call_chat_model", side_effect=[key_moment_output, diary_output],
        ), patch.object(
            service, "deliver_generated_diary_notification",
        ) as deliver_notification:
            result = service.generate_user_diary(
                user, "2026-07-09", deliver_notification=False,
            )
        deliver_notification.assert_not_called()
        self.assertEqual("DONE", result["state"])
        with closing(db.get_db()) as conn:
            diary = conn.execute("SELECT * FROM user_diary WHERE id = 1").fetchone()
            card = conn.execute("SELECT * FROM user_chat_message WHERE message_type = 'card'").fetchone()
        self.assertEqual("DONE", diary["state"])
        self.assertEqual("normal", diary["mode"])
        self.assertEqual("SKIPPED", diary["push_state"])
        self.assertEqual("团团的日记", diary["title"])
        self.assertEqual("团团的日记", card["diary_title"])
        db.update_user_bear_nickname(self.user_id, "麦麦")
        with closing(db.get_db()) as conn:
            stored_title = conn.execute(
                "SELECT title FROM user_diary WHERE id = 1",
            ).fetchone()["title"]
        self.assertEqual("团团的日记", stored_title)
        self.assertIn("\n\n", diary["content"])
        metrics = generate_latest().decode("utf-8")
        self.assertIn(
            'diary_generation_total{mode="normal",result="success"}', metrics,
        )

    def test_prepare_date_retry_checks_existing_job_state(self):
        missing = db.prepare_diary_job_for_date_retry(
            self.user_id, "2026-07-06", retry_done_without_image=True,
        )
        self.assertEqual("created_missing", missing["action"])

        done_with_image = db.create_or_reset_diary_job(self.user_id, "2026-07-07")
        claimed = db.claim_diary_job(self.user_id, "2026-07-07")
        db.complete_diary_job(
            claimed["id"], "title", "content", "summary", ["/image.png"],
            "", "normal", [], "hash",
        )
        skipped = db.prepare_diary_job_for_date_retry(
            self.user_id, "2026-07-07", retry_done_without_image=True,
        )
        self.assertEqual("skipped_done", skipped["action"])
        self.assertEqual("DONE", skipped["state"])

        done_without_image = db.create_or_reset_diary_job(self.user_id, "2026-07-08")
        claimed = db.claim_diary_job(self.user_id, "2026-07-08")
        db.complete_diary_job(
            claimed["id"], "title", "content", "summary", [],
            "", "normal", [], "hash",
        )
        retried = db.prepare_diary_job_for_date_retry(
            self.user_id, "2026-07-08", retry_done_without_image=True,
        )
        self.assertEqual("retried_missing_image", retried["action"])
        self.assertEqual("GENERATING", retried["state"])

        running = db.create_or_reset_diary_job(self.user_id, "2026-07-09", force=True)
        self.assertEqual(running["id"], db.claim_diary_job(self.user_id, "2026-07-09")["id"])
        skipped_running = db.prepare_diary_job_for_date_retry(
            self.user_id, "2026-07-09", retry_done_without_image=True,
        )
        self.assertEqual("skipped_running", skipped_running["action"])

    def test_image_failure_enters_whole_diary_retry_without_card_or_push(self):
        db.create_or_reset_diary_job(self.user_id, "2026-07-09")
        key_moment_output = "1：用户把今天散步的小事分享给了满仓"
        diary_output = (
            "今天听见你说去散步了，我把那阵风认真收进了谷仓。" * 4
            + "\n\n"
            + "等到夜里再想起来，这仍是一件值得保存的小满足。" * 4
        )
        settings = {
            "diary_quiet_message_threshold": 0,
            "diary_v29_min_chars": 20,
            "diary_v29_max_chars": 700,
            "diary_image_enabled": True,
            "diary_max_retries": 3,
        }
        configured = lambda name, default=None: settings.get(name, default)
        user = db.list_active_users_for_diary()[0]
        with patch.object(service, "_configured", side_effect=configured), patch.object(
            service, "_call_chat_model", side_effect=[key_moment_output, diary_output],
        ), patch.object(
            service, "_call_image_model", side_effect=RuntimeError("image failed"),
        ), patch.object(
            service, "deliver_generated_diary_notification",
        ) as deliver_notification:
            result = service.generate_user_diary(user, "2026-07-09")

        self.assertEqual("GENERATING", result["state"])
        deliver_notification.assert_not_called()
        with closing(db.get_db()) as conn:
            diary = conn.execute("SELECT * FROM user_diary WHERE id = 1").fetchone()
            card_count = conn.execute(
                "SELECT COUNT(*) FROM user_chat_message WHERE message_type = 'card'",
            ).fetchone()[0]
        self.assertEqual("GENERATING", diary["state"])
        self.assertEqual(1, diary["retry_count"])
        self.assertIsNotNone(diary["next_retry_at"])
        self.assertIsNone(diary["generated_at"])
        self.assertEqual(0, card_count)


class DiaryServiceTest(unittest.TestCase):
    def test_timezone_window_converts_to_app_timezone(self):
        start, end = service.diary_window(
            {"tz_iana": "America/Los_Angeles", "tz_offset_min": -420}, "2026-07-09",
        )
        self.assertEqual("2026-07-09 15:00:00", start)
        self.assertEqual("2026-07-10 15:00:00", end)

    def test_validation_preserves_v29_paragraphs(self):
        content = ("我把今天温柔地放进谷仓的小抽屉里。" * 8
                   + "\n\n"
                   + "夜里再翻出来时，还能摸到一点暖暖的光。" * 8)
        normalized = service._normalize_diary(content)
        self.assertEqual(content, normalized)
        self.assertEqual([], service._validate_diary(normalized, 80, 700))

    def test_v29_key_moments_build_one_composite_image(self):
        moments = service._parse_key_moments("1：一起散步\n2：分享了晚饭\n补充说明")
        self.assertEqual(["一起散步", "分享了晚饭"], moments)
        prompts = service._build_image_prompts(moments)
        self.assertEqual(1, len(prompts))
        self.assertIn("共 2 条", prompts[0]["positive_prompt"])
        self.assertIn("1：一起散步", prompts[0]["positive_prompt"])

    def test_each_product_style_loads_its_test_platform_prompt(self):
        expected_sources = {
            "warm_healing": "测试2v29",
            "wool_felt": "羊毛毡v3",
            "pixel_art": "像素风v7",
            "clay_art": "超轻粘土v2",
            "lego_style": "乐高风v2",
            "chinese_ink": "清新水墨v1",
        }
        self.assertEqual(set(expected_sources), set(styles.DIARY_IMAGE_STYLES))
        for style, source in expected_sources.items():
            bundle = styles.get_diary_image_prompt(style)
            prompts = service._build_image_prompts(["一起散步"], style)
            self.assertEqual(source, bundle["source"])
            self.assertEqual(style, prompts[0]["style"])
            self.assertTrue(prompts[0]["positive_prompt"].startswith(bundle["positive_prompt"]))
            self.assertEqual(bundle["negative_prompt"], prompts[0]["negative_prompt"])

    def test_unknown_style_falls_back_to_warm_healing(self):
        self.assertEqual(
            "warm_healing", styles.get_diary_image_prompt("not-a-style")["style"],
        )

    def test_local_image_storage_returns_public_url(self):
        fake_config = {
            "diary_image_storage": "local",
            "diary_public_base_url": "https://cdn.example.test",
        }
        with tempfile.TemporaryDirectory() as home, patch.object(
            storage, "conf", return_value=fake_config,
        ), patch.object(storage.os.path, "expanduser", return_value=os.path.join(home, "cow", "data")):
            url = storage.store_diary_image(7, "2026-07-09", "abc", b"png", "image/png")
            self.assertTrue(url.startswith("https://cdn.example.test/diary-images/diary/"))
            self.assertNotIn("2026-07-09", url)

    def test_image_request_keeps_existing_retries_and_logs_each_attempt(self):
        settings = {
            "diary_image_api_key": "test-key",
            "diary_image_api_base": "https://api.openai.com/v1",
            "diary_image_model": "gpt-image-2",
            "diary_reference_image_enabled": False,
        }
        configured = lambda name, default=None: settings.get(name, default)
        error = service.requests.exceptions.SSLError("unexpected eof")
        prompt = {"positive_prompt": "温暖场景", "negative_prompt": "不要文字"}
        with patch.object(service, "_configured", side_effect=configured), patch.object(
            service.requests, "post", side_effect=error,
        ) as post, patch.object(service.time, "sleep") as sleep, patch.object(
            service.logger, "warning",
        ) as warning:
            with self.assertRaises(service.requests.exceptions.SSLError):
                service._call_image_model(prompt, image_trace_id="trace-test")

        self.assertEqual(4, post.call_count)
        self.assertEqual([call(2), call(5), call(10)], sleep.call_args_list)
        self.assertEqual(4, warning.call_count)
        self.assertTrue(all(item.args[1] == "trace-test" for item in warning.call_args_list))


class DiaryWorkerTest(unittest.TestCase):
    def test_scheduled_generation_runs_at_most_five_in_parallel(self):
        users = [
            {"id": user_id, "tz_iana": "Asia/Shanghai", "tz_offset_min": 480}
            for user_id in range(1, 11)
        ]
        settings = {
            "diary_generation_hour": 23,
            "diary_generation_day_offset": 0,
            "diary_generation_workers": 5,
        }
        active = 0
        peak = 0
        lock = threading.Lock()

        def generate(user, diary_date):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.03)
            with lock:
                active -= 1
            return {"state": "DONE"}

        with patch.object(worker, "conf", return_value=settings), patch.object(
            worker.db, "list_active_users_for_diary", return_value=users,
        ), patch.object(
            worker.db, "create_or_reset_diary_job", return_value={"state": "GENERATING"},
        ), patch.object(worker, "generate_user_diary", side_effect=generate):
            processed = worker.run_scheduled_diaries_once(
                datetime(2026, 7, 16, 15, 0, tzinfo=timezone.utc),
            )

        self.assertEqual(10, len(processed))
        self.assertEqual(5, peak)
        self.assertEqual({"2026-07-16"}, {item["date"] for item in processed})

    def test_date_retry_checks_all_users_and_runs_only_candidates(self):
        users = [
            {"id": 1, "tz_iana": "Asia/Shanghai", "tz_offset_min": 480},
            {"id": 2, "tz_iana": "Asia/Shanghai", "tz_offset_min": 480},
            {"id": 3, "tz_iana": "Asia/Shanghai", "tz_offset_min": 480},
        ]
        jobs = [
            {"state": "DONE", "action": "skipped_done"},
            {"state": "GENERATING", "action": "retried_missing_image"},
            {"state": "GENERATING", "action": "created_missing"},
        ]
        settings = {"diary_generation_workers": 5, "diary_image_enabled": True}
        with patch.object(worker, "conf", return_value=settings), patch.object(
            worker.db, "list_active_users_for_diary", return_value=users,
        ), patch.object(
            worker.db, "prepare_diary_job_for_date_retry", side_effect=jobs,
        ) as prepare, patch.object(
            worker, "generate_user_diary", return_value={"state": "DONE"},
        ) as generate:
            result = worker.retry_diaries_for_date("2026-07-16")

        self.assertEqual(3, prepare.call_count)
        self.assertEqual(2, generate.call_count)
        self.assertTrue(all(item.args[2] is False for item in generate.call_args_list))
        self.assertEqual(3, result["checked"])
        self.assertEqual(2, result["scheduled"])
        self.assertEqual(2, result["processed"])
        self.assertEqual(1, result["actions"]["skipped_done"])

    def test_admin_date_retry_endpoint_triggers_async_batch(self):
        web_channel.web.ctx.env = {
            "HTTP_X_ADMIN_PASSCODE": web_channel.COMPLAINT_ADMIN_PASSCODE,
        }
        handler = web_channel.DiaryDateRetryHandler()
        with patch.object(web_channel.web, "header"), patch.object(
            web_channel.web, "data", return_value=b'{"targetDate":"2026-07-16"}',
        ), patch.object(
            worker, "trigger_diary_date_retry",
            return_value={"targetDate": "2026-07-16", "started": True},
        ) as trigger:
            response = json.loads(handler.POST())

        self.assertTrue(response["success"])
        self.assertEqual("2026-07-16", response["data"]["targetDate"])
        trigger.assert_called_once_with("2026-07-16")


if __name__ == "__main__":
    unittest.main()
