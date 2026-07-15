# 生产日记部署

日记生成运行在 `ai-chat-soul` Web 进程的独立后台线程中。每天到达用户本地时间
`diary_generation_hour` 后，worker 为该用户生成前一天的日记。任务通过 `user_diary`
表认领，因此进程重启后可以继续重试，不依赖内存中的任务状态。

## 第一阶段：只生成文字

先在生产配置或环境变量中设置：

```json
{
  "diary_worker_enabled": true,
  "diary_generation_hour": 1,
  "diary_worker_poll_seconds": 300,
  "diary_quiet_message_threshold": 3,
  "diary_max_chars": 120,
  "diary_text_model": "你的文字模型",
  "diary_text_api_key": "模型 API Key",
  "diary_text_api_base": "https://兼容端点/v1",
  "diary_image_enabled": false
}
```

相同配置也可以用大写环境变量传入，例如 `DIARY_WORKER_ENABLED=true`。
未设置日记专用的文字 Key/Base 时会回退到 `open_ai_api_key` 和
`open_ai_api_base`。

重启服务后，日志应出现：

```text
[Diary] worker started
```

可以使用已登录用户的 token 手动做一次安静日验证：

```bash
curl -X POST http://127.0.0.1:9899/api/diary \
  -H 'Content-Type: application/json' \
  -H 'X-Auth-Token: USER_TOKEN' \
  -d '{"targetDate":"2026-07-09","mode":"quiet","force":true}'
```

接口返回 `202` 后，通过 `GET /api/diary?ts=...` 轮询状态。成功状态为 `DONE`；
单次失败会按 5、10 分钟退避重试，达到 `diary_max_retries` 后为 `SKIPPED`。

## 第二阶段：开启生图

本机文件存储适用于单实例验证：

```json
{
  "diary_image_enabled": true,
  "diary_image_model": "gpt-image-2",
  "diary_image_api_key": "IMAGE_API_KEY",
  "diary_image_api_base": "https://api.openai.com/v1",
  "diary_image_count": 1,
  "diary_image_storage": "local",
  "diary_public_base_url": "https://你的 API 域名"
}
```

本地图片写入 `~/cow/data/diary_images`，由 `/diary-images/...` 提供只读访问。
部署容器时必须持久化 `~/cow/data`。

正式环境推荐 OSS：

```json
{
  "diary_image_storage": "oss",
  "diary_oss_access_key_id": "...",
  "diary_oss_access_key_secret": "...",
  "diary_oss_bucket": "bucket-name",
  "diary_oss_endpoint": "oss-cn-region.aliyuncs.com",
  "diary_oss_public_base_url": "https://cdn.example.com"
}
```

OSS RAM 账号只需要目标 bucket 的 `PutObject` 权限。图片失败不会回滚已经生成的文字；
当天日记仍会进入 `DONE`，但 `imageUrls` 可能为空。

## TIMPush 通知

服务端通过腾讯云单发推送接口发送日记完成通知。生产配置需要设置：

```json
{
  "tencent_im_sdk_app_id": 1600150143,
  "tencent_im_admin_user_id": "administrator",
  "tencent_im_secret_key": "服务端 Chat 应用密钥",
  "tencent_im_api_base": "https://console.tim.qq.com",
  "tencent_im_push_timeout_seconds": 10,
  "tencent_im_push_max_retries": 3
}
```

`tencent_im_secret_key` 只能放在服务端配置或环境变量中。客户端使用的 Push Key、
Android `timpush-configs.json` 和厂商证书不需要部署到本服务。

Flutter 调用 TIMPush `getRegistrationID()` 后，将返回值作为 `pushToken` 注册：

```bash
curl -X POST http://127.0.0.1:9899/api/push/register \
  -H 'Content-Type: application/json' \
  -H 'X-Auth-Token: USER_TOKEN' \
  -d '{"platform":"android","pushToken":"TIMPush RegistrationID"}'
```

退出账号时使用同一个 `pushToken` 解绑：

```bash
curl -X POST http://127.0.0.1:9899/api/push/unregister \
  -H 'Content-Type: application/json' \
  -H 'X-Auth-Token: USER_TOKEN' \
  -d '{"pushToken":"TIMPush RegistrationID"}'
```

客户端联调时，可以向当前登录用户绑定的设备发送自定义测试通知：

```bash
curl -X POST http://127.0.0.1:9899/api/push/test \
  -H 'Content-Type: application/json' \
  -H 'X-Auth-Token: USER_TOKEN' \
  -d '{"title":"联调标题","content":"联调正文"}'
```

该接口不读取或修改日记、聊天记录和正式推送状态，成功时返回腾讯云
`taskId`。`title` 长度为 1 至 32 个字符，`content` 长度为 1 至 50 个字符；
当前用户没有注册中的推送设备时返回 `409`。

日记完成后会立即尝试推送。腾讯接口失败时，日记保持 `DONE`，推送按照
`tencent_im_push_max_retries` 独立重试；通知点击参数通过 `Ext` 透传
`type=diary`、`diaryDate` 和 `ts`。

## 发布检查

1. 备份 `~/cow/data/soul.db`。
2. 发布代码并重启 Web 服务；启动时会自动补充表字段。
3. 保持 `diary_worker_enabled=false`，先通过 POST 手动生成一名测试用户。
4. 确认详情接口、聊天历史日记卡片、图片 URL 都正常。
5. 使用真机 RegistrationID 调用注册接口，并在腾讯云接入测试中确认设备可达。
6. 开启 worker，观察至少一个跨日任务、推送 TaskId 及失败重试日志。
