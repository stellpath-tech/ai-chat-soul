import os
import json
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from unittest.mock import patch

import channel.web.database as db
from channel.web.diary import service
from channel.web.diary import storage


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

    def test_full_text_generation_writes_diary_and_card(self):
        db.create_or_reset_diary_job(self.user_id, "2026-07-09")
        model_output = {
            "title": "风吹过的小路",
            "summary": "今天记住了一次散步",
            "diary": "今天听见你说去散步了，我就把那阵轻轻的风收进谷仓，放在离你最近的小抽屉里，路边晃动的叶子也被我悄悄记住了，等你下次回来时再一起看看，我会把软软的位置一直留在这里",
            "image_prompts": [{"scene": "满仓陪用户在暖色小路上散步"}],
        }
        settings = {
            "diary_quiet_message_threshold": 0,
            "diary_max_chars": 100,
            "diary_image_enabled": False,
            "diary_max_retries": 3,
        }
        configured = lambda name, default=None: settings.get(name, default)
        user = db.list_active_users_for_diary()[0]
        with patch.object(service, "_configured", side_effect=configured), patch.object(
            service, "_call_chat_model", return_value=json.dumps(model_output, ensure_ascii=False),
        ):
            result = service.generate_user_diary(user, "2026-07-09")
        self.assertEqual("DONE", result["state"])
        with closing(db.get_db()) as conn:
            diary = conn.execute("SELECT * FROM user_diary WHERE id = 1").fetchone()
            card = conn.execute("SELECT * FROM user_chat_message WHERE message_type = 'card'").fetchone()
        self.assertEqual("DONE", diary["state"])
        self.assertEqual("normal", diary["mode"])
        self.assertEqual("风吹过的小路", card["diary_title"])


class DiaryServiceTest(unittest.TestCase):
    def test_timezone_window_converts_to_app_timezone(self):
        start, end = service.diary_window(
            {"tz_iana": "America/Los_Angeles", "tz_offset_min": -420}, "2026-07-09",
        )
        self.assertEqual("2026-07-09 15:00:00", start)
        self.assertEqual("2026-07-10 15:00:00", end)

    def test_validation_does_not_truncate_long_diary(self):
        content = "我把今天温柔地放进谷仓的小抽屉里" * 20
        normalized = service._normalize_diary(content)
        self.assertEqual(content, normalized)
        self.assertEqual([], service._validate_diary({"diary": normalized}, 120))

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


if __name__ == "__main__":
    unittest.main()
