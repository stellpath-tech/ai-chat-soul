import base64
import hashlib
import hmac
import json
import os
import tempfile
import unittest
import zlib
from contextlib import closing
from unittest.mock import patch

import web

import channel.web.database as db
from channel.web.push.contracts import (
    DiaryReadyPushNotification,
    PushDeviceRequestError,
    PushTestNotification,
    PushTestRequestError,
    UserPushDeviceRegistration,
)
from channel.web.push import service as push_service
import channel.web.web_channel as web_channel


class PushDatabaseTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_db_path = db.DB_PATH
        db.DB_PATH = os.path.join(self.temp_dir.name, "soul.db")
        db.init_db()
        with closing(db.get_db()) as conn:
            for index in (1, 2):
                conn.execute("""
                    INSERT INTO user
                    (phone_number, invite_code, user_group, auth_token, account_status,
                     tz_iana, tz_offset_min, created_at, updated_at)
                    VALUES (?, 'test', 0, ?, 'active', 'Asia/Shanghai', 480,
                            '2026-07-10 00:00:00', '2026-07-10 00:00:00')
                """, ("+861380000000{}".format(index), "token-{}".format(index)))
            conn.commit()
            rows = conn.execute("SELECT id FROM user ORDER BY id").fetchall()
            self.user_one = rows[0]["id"]
            self.user_two = rows[1]["id"]

    def tearDown(self):
        db.DB_PATH = self.old_db_path
        self.temp_dir.cleanup()

    def test_registration_replaces_user_device_and_moves_device_between_users(self):
        db.register_user_push_device(self.user_one, "ios", "registration-one")
        db.register_user_push_device(self.user_one, "android", "registration-two")
        first_device = db.get_user_push_device(self.user_one)
        self.assertEqual("registration-two", first_device["push_token"])
        self.assertEqual("android", first_device["platform"])

        db.register_user_push_device(self.user_two, "android", "registration-two")
        self.assertIsNone(db.get_user_push_device(self.user_one))
        self.assertEqual(
            "registration-two",
            db.get_user_push_device(self.user_two)["push_token"],
        )

    def test_unregister_requires_matching_user_and_registration_id(self):
        db.register_user_push_device(self.user_one, "ios", "registration-one")
        self.assertFalse(
            db.unregister_user_push_device(self.user_one, "registration-other")
        )
        self.assertEqual(1, db.get_user_push_device(self.user_one)["enabled"])
        self.assertTrue(
            db.unregister_user_push_device(self.user_one, "registration-one")
        )
        self.assertEqual(0, db.get_user_push_device(self.user_one)["enabled"])


class PushContractTest(unittest.TestCase):
    def test_registration_contract_keeps_timpush_registration_id_as_push_token(self):
        registration = UserPushDeviceRegistration.from_request_body({
            "platform": "IOS",
            "pushToken": "registration-id",
            "deviceModel": "iPhone",
        })
        self.assertEqual("ios", registration.platform)
        self.assertEqual("registration-id", registration.push_token)
        self.assertEqual("iPhone", registration.device_model)

    def test_registration_contract_rejects_invalid_platform(self):
        with self.assertRaises(PushDeviceRequestError):
            UserPushDeviceRegistration.from_request_body({
                "platform": "windows",
                "pushToken": "registration-id",
            })

    def test_diary_notification_serializes_text_and_click_target(self):
        notification = DiaryReadyPushNotification.from_delivery_record({
            "user_id": 7,
            "diary_date": "2026-07-09",
            "tz_iana": "Asia/Shanghai",
            "tz_offset_min": 480,
        })
        body = notification.to_tencent_request_body(
            "administrator", "registration-id", 123
        )
        push_info = body["OfflinePushInfo"]
        extension = json.loads(push_info["Ext"])
        self.assertEqual("diary", extension["type"])
        self.assertEqual("2026-07-09", extension["diaryDate"])
        self.assertIsInstance(extension["ts"], int)
        self.assertEqual("快来接收今天的日记呀✨", push_info["Title"])
        self.assertEqual("满仓已经把今天的小确幸整理好啦", push_info["Desc"])
        self.assertEqual({
            "Title": "记忆碎片",
            "SubTitle": "快来接收今天的日记呀✨",
        }, push_info["ApnsInfo"])
        self.assertNotIn("AndroidInfo", push_info)
        self.assertNotIn("HarmonyInfo", push_info)
        self.assertEqual(["registration-id"], body["To_Account"])

    def test_push_test_notification_serializes_custom_text_without_diary_fields(self):
        notification = PushTestNotification.from_request_body({
            "title": "联调标题",
            "content": "联调正文",
        })
        body = notification.to_tencent_request_body(
            "administrator", "registration-id", 123
        )
        self.assertEqual("联调标题", body["OfflinePushInfo"]["Title"])
        self.assertEqual("联调正文", body["OfflinePushInfo"]["Desc"])
        self.assertNotIn("Ext", body["OfflinePushInfo"])
        self.assertNotIn("DataId", body)
        self.assertEqual("push-test", body["TaskName"])

    def test_push_test_notification_requires_title_and_content(self):
        with self.assertRaises(PushTestRequestError):
            PushTestNotification.from_request_body({"title": "联调标题"})


class TencentPushClientTest(unittest.TestCase):
    def setUp(self):
        self.config = push_service._TencentPushConfig(
            sdk_app_id=1600150143,
            administrator="administrator",
            secret_key="test-server-secret",
            api_base="https://console.tim.qq.com",
            timeout_seconds=10,
            max_retries=3,
        )
        self.notification = DiaryReadyPushNotification(
            user_id=7,
            diary_date="2026-07-09",
            diary_timestamp_ms=1783569600000,
        )

    def test_user_sig_contains_valid_hmac_signature(self):
        user_sig = push_service._TencentImUserSigSigner.generate(
            1600150143,
            "administrator",
            "test-server-secret",
            expire_seconds=86400,
            now=1784160000,
        )
        encoded = user_sig.replace("*", "+").replace("-", "/").replace("_", "=")
        ticket = json.loads(zlib.decompress(base64.b64decode(encoded)).decode("utf-8"))
        sign_content = (
            "TLS.identifier:administrator\n"
            "TLS.sdkappid:1600150143\n"
            "TLS.time:1784160000\n"
            "TLS.expire:86400\n"
        )
        expected_signature = base64.b64encode(hmac.new(
            b"test-server-secret",
            sign_content.encode("utf-8"),
            hashlib.sha256,
        ).digest()).decode("ascii")
        self.assertEqual(expected_signature, ticket["TLS.sig"])

    def test_client_checks_tencent_business_result(self):
        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {"ErrorCode": 90045, "ErrorInfo": "not enabled"}

        client = push_service._TencentPushClient(
            self.config,
            http_post=lambda *args, **kwargs: FakeResponse(),
        )
        with self.assertRaises(push_service._TencentPushError):
            client.send("registration-id", self.notification)

    def test_client_sends_single_push_without_exposing_secret(self):
        captured = {}

        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {"ErrorCode": 0, "ErrorInfo": "", "TaskId": "task-1"}

        def fake_post(url, **kwargs):
            captured["url"] = url
            captured.update(kwargs)
            return FakeResponse()

        client = push_service._TencentPushClient(
            self.config,
            http_post=fake_post,
        )
        self.assertEqual("task-1", client.send("registration-id", self.notification))
        self.assertEqual(
            "https://console.tim.qq.com/v4/timpush/batch",
            captured["url"],
        )
        self.assertEqual(["registration-id"], captured["json"]["To_Account"])
        self.assertNotIn("test-server-secret", json.dumps(captured))


class PushTestServiceTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_db_path = db.DB_PATH
        db.DB_PATH = os.path.join(self.temp_dir.name, "soul.db")
        db.init_db()
        with closing(db.get_db()) as conn:
            conn.execute("""
                INSERT INTO user
                (phone_number, invite_code, user_group, auth_token, account_status,
                 created_at, updated_at)
                VALUES ('+8613800000000', 'test', 0, 'token', 'active',
                        '2026-07-10 00:00:00', '2026-07-10 00:00:00')
            """)
            conn.commit()
            self.user_id = conn.execute("SELECT id FROM user").fetchone()["id"]

    def tearDown(self):
        db.DB_PATH = self.old_db_path
        self.temp_dir.cleanup()

    def test_push_test_sends_custom_message_without_touching_diary(self):
        db.register_user_push_device(
            self.user_id, "ios", "registration-id"
        )
        config = push_service._TencentPushConfig(
            1600150143, "administrator", "secret", "https://console.tim.qq.com", 10, 3
        )
        with patch.object(
            push_service._TencentPushConfig,
            "from_runtime",
            return_value=config,
        ), patch.object(
            push_service._TencentPushClient,
            "send",
            return_value="task-test",
        ) as send_mock:
            result = push_service.send_authenticated_user_push_test(
                self.user_id,
                {"title": "自定义标题", "content": "自定义正文"},
            )

        self.assertEqual({"taskId": "task-test"}, result)
        push_token, notification = send_mock.call_args.args
        self.assertEqual("registration-id", push_token)
        self.assertEqual("自定义标题", notification.title)
        self.assertEqual("自定义正文", notification.content)
        with closing(db.get_db()) as conn:
            diary_count = conn.execute("SELECT COUNT(*) FROM user_diary").fetchone()[0]
        self.assertEqual(0, diary_count)

    def test_push_test_requires_registered_device(self):
        with self.assertRaises(push_service.PushTestDeviceNotRegisteredError):
            push_service.send_authenticated_user_push_test(
                self.user_id,
                {"title": "自定义标题", "content": "自定义正文"},
            )


class DiaryPushDeliveryTest(unittest.TestCase):
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
                        'Asia/Shanghai', 480, '2026-07-10 00:00:00',
                        '2026-07-10 00:00:00')
            """)
            self.user_id = conn.execute("SELECT id FROM user").fetchone()["id"]
            conn.commit()
        self._complete_diary()

    def tearDown(self):
        db.DB_PATH = self.old_db_path
        self.temp_dir.cleanup()

    def _complete_diary(self):
        job = db.create_or_reset_diary_job(self.user_id, "2026-07-09")
        db.complete_diary_job(
            job["id"], "散步", "正文", "摘要", [], "", "normal", [], "hash"
        )

    def test_delivery_skips_when_user_has_no_registered_device(self):
        result = push_service.deliver_generated_diary_notification(
            self.user_id, "2026-07-09"
        )
        self.assertEqual("SKIPPED", result["state"])
        self.assertEqual(
            "SKIPPED",
            db.get_diary_push_delivery(self.user_id, "2026-07-09")["push_state"],
        )

    def test_delivery_marks_tencent_task_as_sent(self):
        db.register_user_push_device(
            self.user_id, "android", "registration-id"
        )
        config = push_service._TencentPushConfig(
            1600150143, "administrator", "secret", "https://console.tim.qq.com", 10, 3
        )
        with patch.object(
            push_service._TencentPushConfig,
            "from_runtime",
            return_value=config,
        ), patch.object(
            push_service._TencentPushClient,
            "send",
            return_value="task-1",
        ):
            result = push_service.deliver_generated_diary_notification(
                self.user_id, "2026-07-09"
            )
        delivery = db.get_diary_push_delivery(self.user_id, "2026-07-09")
        self.assertEqual("SENT", result["state"])
        self.assertEqual("SENT", delivery["push_state"])
        self.assertEqual("task-1", delivery["push_task_id"])

    def test_delivery_schedules_retry_after_provider_error(self):
        db.register_user_push_device(
            self.user_id, "android", "registration-id"
        )
        config = push_service._TencentPushConfig(
            1600150143, "administrator", "secret", "https://console.tim.qq.com", 10, 3
        )
        with patch.object(
            push_service._TencentPushConfig,
            "from_runtime",
            return_value=config,
        ), patch.object(
            push_service._TencentPushClient,
            "send",
            side_effect=push_service._TencentPushError("temporary"),
        ):
            result = push_service.deliver_generated_diary_notification(
                self.user_id, "2026-07-09"
            )
        delivery = db.get_diary_push_delivery(self.user_id, "2026-07-09")
        self.assertEqual("PENDING", result["state"])
        self.assertEqual(1, delivery["push_retry_count"])
        self.assertIsNotNone(delivery["push_next_retry_at"])


class PushHttpApiTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_db_path = db.DB_PATH
        db.DB_PATH = os.path.join(self.temp_dir.name, "soul.db")
        db.init_db()
        with closing(db.get_db()) as conn:
            conn.execute("""
                INSERT INTO user
                (phone_number, invite_code, user_group, auth_token, account_status,
                 created_at, updated_at)
                VALUES ('+8613800000000', 'test', 0, 'api-token', 'active',
                        '2026-07-10 00:00:00', '2026-07-10 00:00:00')
            """)
            conn.commit()
            self.user_id = conn.execute("SELECT id FROM user").fetchone()["id"]
        self.app = web.application((
            '/api/push/register', 'UserPushDeviceRegisterHandler',
            '/api/push/unregister', 'UserPushDeviceUnregisterHandler',
            '/api/push/test', 'PushTestHandler',
        ), web_channel.__dict__)

    def tearDown(self):
        db.DB_PATH = self.old_db_path
        self.temp_dir.cleanup()

    def test_register_and_unregister_api(self):
        headers = {
            "X-Auth-Token": "api-token",
            "Content-Type": "application/json",
        }
        response = self.app.request(
            "/api/push/register",
            method="POST",
            headers=headers,
            data=json.dumps({
                "platform": "ios",
                "pushToken": "registration-id",
                "appVersion": "2.2.4",
            }),
        )
        self.assertEqual("200 OK", response.status)
        self.assertTrue(json.loads(response.data)["success"])
        self.assertEqual(
            "registration-id",
            db.get_user_push_device(self.user_id)["push_token"],
        )

        response = self.app.request(
            "/api/push/unregister",
            method="POST",
            headers=headers,
            data=json.dumps({"pushToken": "registration-id"}),
        )
        self.assertEqual("200 OK", response.status)
        self.assertTrue(json.loads(response.data)["success"])
        self.assertEqual(0, db.get_user_push_device(self.user_id)["enabled"])

    def test_register_api_requires_authentication(self):
        response = self.app.request(
            "/api/push/register",
            method="POST",
            headers={"Content-Type": "application/json"},
            data=json.dumps({
                "platform": "ios",
                "pushToken": "registration-id",
            }),
        )
        self.assertEqual("401 Unauthorized", response.status)

    def test_push_test_api_sends_custom_message(self):
        request_body = {"title": "联调标题", "content": "联调正文"}
        with patch.object(
            web_channel,
            "send_authenticated_user_push_test",
            return_value={"taskId": "task-test"},
        ) as send_mock:
            response = self.app.request(
                "/api/push/test",
                method="POST",
                headers={
                    "X-Auth-Token": "api-token",
                    "Content-Type": "application/json",
                },
                data=json.dumps(request_body),
            )

        self.assertEqual("200 OK", response.status)
        payload = json.loads(response.data)
        self.assertTrue(payload["success"])
        self.assertEqual({"taskId": "task-test"}, payload["data"])
        send_mock.assert_called_once_with(self.user_id, request_body)

    def test_push_test_api_requires_authentication(self):
        response = self.app.request(
            "/api/push/test",
            method="POST",
            headers={"Content-Type": "application/json"},
            data=json.dumps({"title": "联调标题", "content": "联调正文"}),
        )
        self.assertEqual("401 Unauthorized", response.status)

    def test_push_test_api_rejects_incomplete_message(self):
        response = self.app.request(
            "/api/push/test",
            method="POST",
            headers={
                "X-Auth-Token": "api-token",
                "Content-Type": "application/json",
            },
            data=json.dumps({"title": "联调标题"}),
        )
        self.assertEqual("400 Bad Request", response.status)
        self.assertEqual("content is required", json.loads(response.data)["message"])

    def test_push_test_api_reports_missing_device(self):
        response = self.app.request(
            "/api/push/test",
            method="POST",
            headers={
                "X-Auth-Token": "api-token",
                "Content-Type": "application/json",
            },
            data=json.dumps({"title": "联调标题", "content": "联调正文"}),
        )
        self.assertEqual("409 Conflict", response.status)
        self.assertEqual(
            "push device not registered",
            json.loads(response.data)["message"],
        )


if __name__ == "__main__":
    unittest.main()
