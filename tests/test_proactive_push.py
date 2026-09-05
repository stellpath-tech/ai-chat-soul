import json
import os
import random
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from unittest.mock import patch

import web

import channel.web.database as db
import channel.web.web_channel as web_channel
from channel.web.push import assets, planning, repository, service, worker
from channel.web.push.contracts import UserActivityReport, UserActivityRequestError
from channel.web.push.conversation import ConversationActivityTracker, conversation_activity


class ProactivePushDatabaseTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_db_path = db.DB_PATH
        db.DB_PATH = os.path.join(self.temp_dir.name, "soul.db")
        db.init_db()
        with closing(db.get_db()) as conn:
            conn.execute("""
                INSERT INTO user
                (phone_number, invite_code, user_group, auth_token, account_status,
                 tz_iana, tz_offset_min, last_active_at, notification_enabled,
                 created_at, updated_at)
                VALUES ('+8613800000011', 'test', 0, 'activity-token', 'active',
                        'Asia/Shanghai', 480, '2026-09-05 08:00:00', 1,
                        '2026-09-05 08:00:00', '2026-09-05 08:00:00')
            """)
            conn.commit()
            self.user_id = conn.execute("SELECT id FROM user").fetchone()["id"]

    def tearDown(self):
        conversation_activity.reset()
        db.DB_PATH = self.old_db_path
        self.temp_dir.cleanup()

    def test_seed_contains_complete_weather_pool_and_uploaded_images(self):
        with closing(db.get_db()) as conn:
            self.assertEqual(457, conn.execute("SELECT COUNT(*) FROM push_content").fetchone()[0])
            self.assertEqual(111, conn.execute("SELECT COUNT(*) FROM push_content_image").fetchone()[0])
            counts = conn.execute("""
                SELECT delivery_scene, COUNT(*) AS count
                FROM push_content WHERE push_type = 'weather'
                GROUP BY delivery_scene
            """).fetchall()
            greeting_counts = conn.execute("""
                SELECT delivery_scene, COUNT(*) AS count
                FROM push_content WHERE push_type = 'greeting'
                GROUP BY delivery_scene
            """).fetchall()
        self.assertEqual(12, len(counts))
        self.assertTrue(all(row["count"] == 14 for row in counts))
        self.assertEqual(17, len(greeting_counts))
        self.assertTrue(all(row["count"] == 14 for row in greeting_counts))

    def test_activity_updates_server_time_permission_and_optional_location(self):
        report = UserActivityReport.from_request_body({
            "notificationEnabled": False,
            "timezone": {"tz_iana": "Asia/Tokyo", "tz_offset_min": 540},
            "location": {"lat": 35.68, "lon": 139.69},
        })
        repository.update_user_activity(
            self.user_id,
            report.timezone_profile,
            report.notification_enabled,
            report.location,
            now=datetime(2026, 9, 5, 9, 30),
        )
        user = repository.get_user(self.user_id)
        self.assertEqual("2026-09-05 09:30:00", user["last_active_at"])
        self.assertEqual("Asia/Tokyo", user["tz_iana"])
        self.assertEqual(0, user["notification_enabled"])
        self.assertAlmostEqual(35.68, user["last_lat"])

    def test_activity_contract_rejects_invalid_location(self):
        with self.assertRaises(UserActivityRequestError):
            UserActivityReport.from_request_body({
                "timezone": {"tz_iana": "Asia/Shanghai", "tz_offset_min": 480},
                "location": {"lat": 100, "lon": 121},
            })

    def test_opening_current_empty_diary_records_view_without_creating_job(self):
        timestamp_ms = int(
            datetime(2026, 9, 5, 4, 0, tzinfo=timezone.utc).timestamp() * 1000
        )
        self.assertTrue(repository.mark_diary_viewed(
            self.user_id, timestamp_ms, now=datetime(2026, 9, 5, 8, 0)
        ))
        with closing(db.get_db()) as conn:
            view = conn.execute("""
                SELECT viewed_at FROM user_diary_view
                WHERE user_id = ? AND diary_date = '2026-09-05'
            """, (self.user_id,)).fetchone()
            diary = conn.execute("""
                SELECT id FROM user_diary
                WHERE user_id = ? AND diary_date = '2026-09-05'
            """, (self.user_id,)).fetchone()
        self.assertEqual("2026-09-05 08:00:00", view["viewed_at"])
        self.assertIsNone(diary)

    def test_push_content_supports_multiple_images(self):
        config = assets._PushAssetOssConfig(
            "access-id",
            "access-secret",
            "ommo-app-assets-dev",
            "https://oss-cn-wulanchabu.aliyuncs.com",
            86400,
        )
        content = assets.add_signed_urls_to_content_list(
            repository.list_contents(keyword="W-GALE-03", limit=10),
            config=config,
        )["items"][0]
        self.assertEqual(2, len(content["images"]))
        self.assertTrue(all(
            image["imageUrl"].startswith(
                "https://ommo-app-assets-dev.oss-cn-wulanchabu.aliyuncs.com/"
                "push-cards/weather/w-gale-03/"
            )
            for image in content["images"]
        ))
        self.assertTrue(all(
            "objectKey" not in image for image in content["images"]
        ))

    def test_task_creation_is_idempotent(self):
        first = repository.create_task(
            self.user_id, "recall", "2026-09-05", "cycle|7", 1,
            datetime(2026, 9, 5, 20, 0),
            {"type": "recall", "title": "标题", "body": "正文", "action": "open_home"},
        )
        second = repository.create_task(
            self.user_id, "recall", "2026-09-05", "cycle|7", 2,
            datetime(2026, 9, 5, 20, 1),
            {"type": "recall", "title": "另一个", "body": "正文", "action": "open_home"},
        )
        self.assertEqual(first["id"], second["id"])
        with closing(db.get_db()) as conn:
            self.assertEqual(1, conn.execute("SELECT COUNT(*) FROM user_push_task").fetchone()[0])

    def test_recently_sent_content_is_not_selected_again(self):
        now = datetime(2026, 9, 5, 23, 10)
        first = repository.select_content(
            self.user_id, "diary", ["DIARY_READY"], now=now
        )
        task = repository.create_task(
            self.user_id, "diary", "2026-09-05", "2026-09-05",
            first["id"], now,
            {"type": "diary", "title": first["title"], "body": first["body"], "action": "open_diary"},
        )
        repository.mark_task_sent(task["id"], "provider", now=now)
        second = repository.select_content(
            self.user_id, "diary", ["DIARY_READY"], now=now
        )
        self.assertNotEqual(first["id"], second["id"])


class ProactivePushPlanningTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_db_path = db.DB_PATH
        db.DB_PATH = os.path.join(self.temp_dir.name, "soul.db")
        db.init_db()
        with closing(db.get_db()) as conn:
            conn.execute("""
                INSERT INTO user
                (phone_number, invite_code, user_group, auth_token, account_status,
                 tz_iana, tz_offset_min, last_active_at, last_lat, last_lon,
                 notification_enabled, created_at, updated_at)
                VALUES ('+8613800000022', 'test', 0, 'token', 'active',
                        'Asia/Shanghai', 480, '2026-09-05 07:50:00', 39.92, 116.41,
                        1, '2026-09-05 07:50:00', '2026-09-05 07:50:00')
            """)
            conn.commit()
            self.user_id = conn.execute("SELECT id FROM user").fetchone()["id"]
        db.register_user_push_device(self.user_id, "ios", "registration-id")

    def tearDown(self):
        conversation_activity.reset()
        db.DB_PATH = self.old_db_path
        self.temp_dir.cleanup()

    def test_greeting_plan_is_generated_once(self):
        now_utc = datetime(2026, 9, 5, 0, 0, tzinfo=timezone.utc)
        first = planning.generate_greeting_plans(
            now_utc=now_utc, random_source=random.Random(1)
        )
        second = planning.generate_greeting_plans(
            now_utc=now_utc, random_source=random.Random(2)
        )
        self.assertEqual(1, len(first))
        self.assertEqual([], second)
        payload = json.loads(first[0]["card_json"])
        self.assertEqual("greeting", payload["type"])
        self.assertTrue(payload["pushId"].startswith("psh_"))

    def test_weather_poll_groups_and_creates_one_daily_task(self):
        class FakeWeatherClient:
            calls = 0

            def current_alerts(self, latitude, longitude):
                self.calls += 1
                return [{
                    "id": "alert-1",
                    "issuedTime": "2026-09-05T08:00:00+08:00",
                    "messageType": {"code": "alert"},
                    "eventType": {"name": "雷电黄色预警", "code": "1001"},
                    "urgency": "immediate",
                    "severity": "severe",
                    "effectiveTime": "2026-09-05T08:00:00+08:00",
                    "onsetTime": None,
                    "expireTime": "2026-09-05T12:00:00+08:00",
                    "headline": "雷电预警",
                    "description": "雷电活动正在影响当地。",
                    "criteria": "符合雷电预警标准。",
                    "responseTypes": ["prepare"],
                    "instruction": "请进入室内。",
                }]

        with closing(db.get_db()) as conn:
            cursor = conn.execute("""
                INSERT INTO user
                (phone_number, invite_code, user_group, auth_token, account_status,
                 tz_iana, tz_offset_min, last_active_at, last_lat, last_lon,
                 notification_enabled, created_at, updated_at)
                VALUES ('+8613800000023', 'test', 0, 'token-2', 'active',
                        'Asia/Shanghai', 480, '2026-09-05 07:50:00', 39.92, 116.41,
                        1, '2026-09-05 07:50:00', '2026-09-05 07:50:00')
            """)
            second_user_id = cursor.lastrowid
            conn.commit()
        db.register_user_push_device(second_user_id, "android", "registration-id-2")

        client = FakeWeatherClient()
        now_utc = datetime(2026, 9, 5, 0, 0, tzinfo=timezone.utc)
        first = planning.poll_weather_alerts(now_utc=now_utc, client=client)
        second = planning.poll_weather_alerts(now_utc=now_utc, client=client)
        self.assertEqual(2, len(first))
        self.assertEqual([], second)
        self.assertEqual(1, client.calls)
        payload = json.loads(first[0]["card_json"])
        self.assertEqual("雷电预警", payload["headline"])
        self.assertEqual(["prepare"], payload["responseTypes"])

    def test_weather_alert_priority(self):
        selected = planning.select_weather_alert([
            {
                "id": "moderate", "issuedTime": "2026-09-05T09:00:00+08:00",
                "messageType": {"code": "alert"},
                "eventType": {"name": "大风"},
                "urgency": "expected", "severity": "moderate",
            },
            {
                "id": "extreme", "issuedTime": "2026-09-05T08:00:00+08:00",
                "messageType": {"code": "update"},
                "eventType": {"name": "雷电"},
                "urgency": "immediate", "severity": "extreme",
            },
        ])
        self.assertEqual("extreme", selected["id"])

    def test_recall_is_created_at_local_day_seven(self):
        with closing(db.get_db()) as conn:
            conn.execute(
                "UPDATE user SET last_active_at = '2026-08-29 12:00:00' WHERE id = ?",
                (self.user_id,),
            )
            conn.commit()
        tasks = planning.schedule_recalls(
            now_utc=datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)
        )
        self.assertEqual(1, len(tasks))
        self.assertEqual("recall", tasks[0]["push_type"])
        self.assertTrue(tasks[0]["business_key"].endswith("|7"))
        self.assertEqual(7, json.loads(tasks[0]["card_json"])["inactiveDays"])


class ProactivePushDeliveryTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_db_path = db.DB_PATH
        db.DB_PATH = os.path.join(self.temp_dir.name, "soul.db")
        db.init_db()
        with closing(db.get_db()) as conn:
            conn.execute("""
                INSERT INTO user
                (phone_number, invite_code, user_group, auth_token, account_status,
                 tz_iana, tz_offset_min, last_active_at, notification_enabled,
                 created_at, updated_at)
                VALUES ('+8613800000033', 'test', 0, 'token', 'active',
                        'Asia/Shanghai', 480, '2026-09-05 08:00:00', 1,
                        '2026-09-05 08:00:00', '2026-09-05 08:00:00')
            """)
            conn.commit()
            self.user_id = conn.execute("SELECT id FROM user").fetchone()["id"]
        db.register_user_push_device(self.user_id, "ios", "registration-id")
        self.push_config = service._TencentPushConfig(
            1, "administrator", "secret", "https://console.tim.qq.com", 10, 3
        )

    def tearDown(self):
        conversation_activity.reset()
        db.DB_PATH = self.old_db_path
        self.temp_dir.cleanup()

    def _greeting_task(self):
        content = repository.select_content(
            self.user_id, "greeting", ["GREETING_0800"],
            now=datetime(2026, 9, 5, 8, 0),
        )
        payload = {
            "type": "greeting", "title": content["title"],
            "body": content["body"], "action": "open_chat",
        }
        if content.get("image_object_key"):
            payload["imageObjectKey"] = content["image_object_key"]
            payload["imageVersion"] = content["image_id"]
        return repository.create_task(
            self.user_id, "greeting", "2026-09-05", "2026-09-05:morning",
            content["id"], datetime(2026, 9, 5, 8, 0), payload,
        )

    def test_greeting_delivery_writes_one_chat_message(self):
        task = self._greeting_task()
        with patch.object(service._TencentPushConfig, "from_runtime", return_value=self.push_config), patch.object(
            service._TencentPushClient, "send", return_value="provider-task"
        ) as send_mock:
            result = service.deliver_proactive_task(
                task["id"], now=datetime(2026, 9, 5, 8, 0)
            )
            self.assertIsNone(service.deliver_proactive_task(
                task["id"], now=datetime(2026, 9, 5, 8, 1)
            ))
        self.assertEqual("SENT", result["state"])
        self.assertEqual(1, send_mock.call_count)
        with closing(db.get_db()) as conn:
            messages = conn.execute("""
                SELECT * FROM user_chat_message
                WHERE user_id = ? AND request_id = ?
            """, (self.user_id, "push:" + task["push_id"])).fetchall()
            saved_task = conn.execute(
                "SELECT card_json FROM user_push_task WHERE id = ?",
                (task["id"],),
            ).fetchone()
        self.assertEqual(1, len(messages))
        self.assertEqual(messages[0]["id"], json.loads(saved_task["card_json"])["messageId"])

    def test_weather_delivery_cancels_next_greeting(self):
        greeting = self._greeting_task()
        weather_content = repository.select_content(
            self.user_id, "weather", ["WEATHER_THUNDER"],
            now=datetime(2026, 9, 5, 8, 0),
        )
        weather = repository.create_task(
            self.user_id, "weather", "2026-09-05", "alert-1",
            weather_content["id"], datetime(2026, 9, 5, 7, 50),
            {
                "type": "weather", "title": weather_content["title"],
                "body": weather_content["body"], "action": "open_weather",
            },
        )
        with patch.object(service._TencentPushConfig, "from_runtime", return_value=self.push_config), patch.object(
            service._TencentPushClient, "send", return_value="weather-task"
        ):
            result = service.deliver_proactive_task(
                weather["id"], now=datetime(2026, 9, 5, 7, 50)
            )
        self.assertEqual("SENT", result["state"])
        with closing(db.get_db()) as conn:
            state = conn.execute(
                "SELECT state FROM user_push_task WHERE id = ?", (greeting["id"],)
            ).fetchone()["state"]
        self.assertEqual("CANCELLED", state)

    def test_greeting_retry_reuses_chat_message(self):
        task = self._greeting_task()
        with patch.object(service._TencentPushConfig, "from_runtime", return_value=self.push_config), patch.object(
            service._TencentPushClient, "send",
            side_effect=[service._TencentPushError("temporary"), "provider-task"],
        ):
            failed = service.deliver_proactive_task(
                task["id"], now=datetime(2026, 9, 5, 8, 0)
            )
            sent = service.deliver_proactive_task(
                task["id"], now=datetime(2026, 9, 5, 8, 1)
            )
        self.assertEqual("PENDING", failed["state"])
        self.assertEqual("SENT", sent["state"])
        with closing(db.get_db()) as conn:
            count = conn.execute("""
                SELECT COUNT(*) FROM user_chat_message
                WHERE user_id = ? AND request_id = ?
            """, (self.user_id, "push:" + task["push_id"])).fetchone()[0]
        self.assertEqual(1, count)

    def test_card_uses_selected_image_snapshot_and_fresh_signed_url(self):
        task = repository.create_task(
            self.user_id,
            "greeting",
            "2026-09-05",
            "2026-09-05:image-card",
            0,
            datetime(2026, 9, 5, 8, 0),
            {
                "type": "greeting",
                "title": "早上好",
                "body": "新的一天开始啦。",
                "action": "open_chat",
                "imageObjectKey": "push-cards/greeting/am-0700-01/v1.png",
                "imageVersion": 1,
            },
        )
        repository.mark_task_sent(task["id"], "provider-task")
        saved = repository.get_task(
            self.user_id, "greeting", "2026-09-05:image-card"
        )
        payload = json.loads(saved["card_json"])
        with patch.object(
            assets,
            "create_image_read_url",
            return_value="https://signed.example/image.png?signature=1",
        ) as sign_url:
            card = service.get_authenticated_user_push_card(
                self.user_id, task["push_id"]
            )
        self.assertEqual(
            "https://signed.example/image.png?signature=1", card["imageUrl"]
        )
        sign_url.assert_called_once_with(payload["imageObjectKey"])
        self.assertIsNotNone(card["greeting"])
        self.assertIsNone(card["weather"])

    def test_greeting_is_cancelled_during_conversation(self):
        task = self._greeting_task()
        conversation_activity.start(self.user_id, "request-1")
        with patch.object(service._TencentPushClient, "send") as send_mock:
            result = service.deliver_proactive_task(
                task["id"], now=datetime(2026, 9, 5, 8, 0)
            )
        self.assertEqual("CANCELLED", result["state"])
        send_mock.assert_not_called()
        with closing(db.get_db()) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM user_chat_message WHERE user_id = ?",
                (self.user_id,),
            ).fetchone()[0]
        self.assertEqual(0, count)

    def test_diary_quiet_window_cancels_other_pushes(self):
        diary = repository.create_task(
            self.user_id, "diary", "2026-09-04", "2026-09-04", 0,
            datetime(2026, 9, 4, 23, 10),
            {"type": "diary", "title": "日记", "body": "正文", "action": "open_diary"},
        )
        repository.mark_task_sent(
            diary["id"], "diary-provider", now=datetime(2026, 9, 4, 23, 10)
        )
        content = repository.select_content(
            self.user_id, "greeting", ["GREETING_2000"],
            now=datetime(2026, 9, 4, 20, 0),
        )
        greeting = repository.create_task(
            self.user_id, "greeting", "2026-09-04", "2026-09-04:evening",
            content["id"], datetime(2026, 9, 4, 20, 0),
            {
                "type": "greeting", "title": content["title"],
                "body": content["body"], "action": "open_chat",
            },
        )
        with patch.object(service._TencentPushClient, "send") as send_mock:
            result = service.deliver_proactive_task(
                greeting["id"], now=datetime(2026, 9, 5, 0, 30)
            )
        self.assertEqual("CANCELLED", result["state"])
        send_mock.assert_not_called()


class ConversationActivityTrackerTest(unittest.TestCase):
    def test_concurrent_requests_and_protection_window(self):
        tracker = ConversationActivityTracker(protection_seconds=5)
        tracker.start(7, "one", now=90)
        tracker.start(7, "two", now=90)
        self.assertTrue(tracker.is_busy(7, now=100))
        tracker.finish(7, "one", now=100)
        self.assertTrue(tracker.is_busy(7, now=100))
        tracker.finish(7, "two", now=100)
        self.assertTrue(tracker.is_busy(7, now=104.9))
        self.assertFalse(tracker.is_busy(7, now=105))

    def test_stale_request_does_not_block_greeting_forever(self):
        tracker = ConversationActivityTracker(
            protection_seconds=5,
            request_timeout_seconds=300,
        )
        tracker.start(7, "stale", now=100)
        self.assertFalse(tracker.is_busy(7, now=400))


class ProactivePushWorkerTest(unittest.TestCase):
    def setUp(self):
        worker._LAST_WEATHER_BUCKET = None

    def test_weather_poll_runs_once_per_half_hour_bucket(self):
        with patch.object(worker, "generate_greeting_plans", return_value=[]), patch.object(
            worker, "poll_weather_alerts", return_value=[]
        ) as weather_poll, patch.object(
            worker, "schedule_recalls", return_value=[]
        ), patch.object(
            worker, "deliver_due_proactive_notifications", return_value=[]
        ):
            worker.run_proactive_push_iteration(
                now_utc=datetime(2026, 9, 5, 0, 7, tzinfo=timezone.utc)
            )
            worker.run_proactive_push_iteration(
                now_utc=datetime(2026, 9, 5, 0, 29, tzinfo=timezone.utc)
            )
            worker.run_proactive_push_iteration(
                now_utc=datetime(2026, 9, 5, 0, 30, tzinfo=timezone.utc)
            )
        self.assertEqual(2, weather_poll.call_count)


class ProactivePushHttpApiTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_db_path = db.DB_PATH
        db.DB_PATH = os.path.join(self.temp_dir.name, "soul.db")
        db.init_db()
        with closing(db.get_db()) as conn:
            cursor = conn.execute("""
                INSERT INTO user
                (phone_number, invite_code, user_group, auth_token, account_status,
                 created_at, updated_at)
                VALUES ('+8613800000044', 'test', 0, 'api-token', 'active',
                        '2026-09-05 08:00:00', '2026-09-05 08:00:00')
            """)
            self.user_id = cursor.lastrowid
            conn.commit()
        self.app = web.application((
            '/api/user/activity', 'UserActivityHandler',
            '/api/admin/push-contents', 'PushContentCollectionHandler',
            r'/api/admin/push-contents/(\d+)', 'PushContentItemHandler',
            r'/api/push/(psh_[0-9a-f]{32})/card', 'PushCardHandler',
        ), web_channel.__dict__)
        self.admin_headers = {
            "X-Admin-Passcode": web_channel.COMPLAINT_ADMIN_PASSCODE,
            "Content-Type": "application/json",
        }

    def tearDown(self):
        db.DB_PATH = self.old_db_path
        self.temp_dir.cleanup()

    def test_activity_api_updates_user(self):
        response = self.app.request(
            "/api/user/activity", method="POST",
            headers={"X-Auth-Token": "api-token", "Content-Type": "application/json"},
            data=json.dumps({
                "notificationEnabled": False,
                "timezone": {"tz_iana": "Asia/Shanghai", "tz_offset_min": 480},
                "location": {"lat": 31.2, "lon": 121.5},
            }),
        )
        self.assertEqual("200 OK", response.status)
        self.assertTrue(json.loads(response.data)["success"])

    def test_admin_content_crud(self):
        request = {
            "contentNo": "TEST-001", "pushType": "greeting",
            "deliveryScene": "GREETING_0700", "title": "标题",
            "body": "正文", "enabled": True,
        }
        created = self.app.request(
            "/api/admin/push-contents", method="POST",
            headers=self.admin_headers, data=json.dumps(request),
        )
        self.assertEqual("200 OK", created.status)
        content_id = json.loads(created.data)["data"]["id"]
        request["title"] = "新标题"
        updated = self.app.request(
            "/api/admin/push-contents/{}".format(content_id), method="PUT",
            headers=self.admin_headers, data=json.dumps(request),
        )
        self.assertEqual("200 OK", updated.status)
        disabled = self.app.request(
            "/api/admin/push-contents/{}".format(content_id), method="DELETE",
            headers=self.admin_headers,
        )
        self.assertEqual("200 OK", disabled.status)
        self.assertEqual(0, repository.get_content(content_id)["enabled"])

    def test_admin_api_requires_passcode(self):
        response = self.app.request("/api/admin/push-contents")
        self.assertEqual("401 Unauthorized", response.status)

    def test_push_card_returns_complete_weather_snapshot(self):
        task = repository.create_task(
            self.user_id,
            "weather",
            "2026-09-05",
            "alert-card",
            0,
            datetime(2026, 9, 5, 8, 0),
            {
                "type": "weather",
                "title": "雷雨快到了",
                "body": "请尽快进入室内。",
                "action": "open_weather",
                "effectiveTime": "2026-09-05T08:00:00+08:00",
                "onsetTime": None,
                "expireTime": "2026-09-05T12:00:00+08:00",
                "headline": "雷电预警",
                "description": "完整预警描述",
                "criteria": "完整预警标准",
                "responseTypes": ["prepare"],
                "instruction": "完整防御指引",
            },
        )
        repository.mark_task_sent(task["id"], "provider-task")
        response = self.app.request(
            "/api/push/{}/card".format(task["push_id"]),
            headers={"X-Auth-Token": "api-token"},
        )
        self.assertEqual("200 OK", response.status)
        card = json.loads(response.data)["data"]
        self.assertEqual("完整防御指引", card["weather"]["instruction"])
        self.assertIsNone(card["greeting"])
        self.assertIsNone(card["diary"])
        self.assertIsNone(card["recall"])

    def test_push_card_rejects_other_user(self):
        task = repository.create_task(
            self.user_id,
            "recall",
            "2026-09-05",
            "recall-card",
            0,
            datetime(2026, 9, 5, 20, 0),
            {
                "type": "recall",
                "title": "回来坐坐",
                "body": "满仓在等你。",
                "action": "open_home",
                "inactiveDays": 7,
            },
        )
        repository.mark_task_sent(task["id"], "provider-task")
        with closing(db.get_db()) as conn:
            conn.execute("""
                INSERT INTO user
                (phone_number, invite_code, user_group, auth_token, account_status,
                 created_at, updated_at)
                VALUES ('+8613800000055', 'test', 0, 'other-token', 'active',
                        '2026-09-05 08:00:00', '2026-09-05 08:00:00')
            """)
            conn.commit()
        response = self.app.request(
            "/api/push/{}/card".format(task["push_id"]),
            headers={"X-Auth-Token": "other-token"},
        )
        self.assertEqual("404 Not Found", response.status)


class PushAssetUploadTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_db_path = db.DB_PATH
        db.DB_PATH = os.path.join(self.temp_dir.name, "soul.db")
        db.init_db()
        self.content_id = repository.create_content(
            "TEST-ASSET-01", "greeting", "GREETING_0700",
            "标题", "正文", True,
        )

    def tearDown(self):
        db.DB_PATH = self.old_db_path
        self.temp_dir.cleanup()

    def test_upload_hides_credentials_and_creates_relation(self):
        config = assets._PushAssetOssConfig(
            "access-id", "access-secret", "bucket",
            "https://oss-cn-wulanchabu.aliyuncs.com", 86400,
        )
        captured = {}

        class Response:
            def raise_for_status(self):
                return None

        def fake_put(url, **kwargs):
            captured["url"] = url
            captured.update(kwargs)
            return Response()

        png = b"\x89PNG\r\n\x1a\n" + b"test-image"
        with patch.object(assets._PushAssetOssConfig, "from_runtime", return_value=config):
            result = assets.upload_image_for_content(
                self.content_id, "image.png", png, http_put=fake_put
            )
        self.assertTrue(result["imageUrl"].startswith(
            "https://bucket.oss-cn-wulanchabu.aliyuncs.com/push-cards/"
        ))
        self.assertIn("OSSAccessKeyId=access-id", result["imageUrl"])
        self.assertIn("Signature=", result["imageUrl"])
        self.assertNotIn("access-secret", json.dumps(captured, default=str))
        self.assertNotIn("x-oss-object-acl", captured["headers"])
        content = repository.list_contents(keyword="TEST-ASSET-01")["items"][0]
        self.assertEqual(1, len(content["images"]))


if __name__ == "__main__":
    unittest.main()
