# MVP 2.2.5 主动式交互 Push 技术方案

## 1. 需求清单

1. **用户状态**：上报活跃时间、时区、位置和系统通知权限，为各类 Push 提供判断依据。
2. **内容资产**：将 Push 文案和插图入库，并提供管理后台。
3. **推送计划与发送**：统一处理四类主动 Push：
   - 问候：每天为活跃用户生成 1～2 条计划；
   - 天气：每 30 分钟检查预警，每人每天最多一条；
   - 日记：23:30 前生成成功时发送；
   - 召回：用户未登录第 7、15、30 天发送。
4. **客户端交互**：点击 Push 后展示首页弹框，并支持跳转和事件上报。

## 2. 用户活跃状态与通知权限

### 2.1 需求细节

- 已登录用户冷启动、回到前台时，调用本接口上报 IANA 时区、UTC 偏移、当前可用位置和系统通知权限；60 秒内可以合并。
- 系统通知权限变化时，立即调用本接口上报最新权限状态。
- 活跃时间使用服务端接收时间。
- 位置复用天气功能已有定位；没有位置时仍正常上报。
- 通知权限统一通过本接口更新，Push Token 保持原有注册状态。

### 2.2 接口

#### （新增）上报用户活跃状态 POST /api/user/activity

```TypeScript
interface UserActivityHeaders {
  "x-auth-token": string       // 登录令牌
}

interface UserActivityRequest {
  notificationEnabled?: boolean // 系统通知权限；新客户端必传，旧客户端未传时保留原值
  timezone: {
    tz_iana: string             // IANA 时区，例如 Asia/Shanghai
    tz_offset_min: number       // UTC 偏移分钟数，例如 UTC+8 为 480
  }
  location?: {
    lat: number                 // 纬度，范围 -90～90
    lon: number                 // 经度，范围 -180～180
  }
}

interface UserActivityResponse {
  success: boolean              // 是否成功
  message: string               // 结果说明
  data: null
}
```

## 3. Push 文案与插图管理

### 3.1 需求细节

- 文案存数据库，插图存 OSS。
- 一条文案可以绑定多张插图；生成 Push 时随机选一张并固定，重试不再随机。
- 同一用户 14 天内不重复使用同一条成功发送过的文案。
- 文案编辑只影响新计划；停用后不再生成新计划，未发送计划直接取消。
- 插图新增时使用新的版本化 Object Key；移除操作只解除文案关系，旧文件继续供历史 Push 使用。
- CTA 和跳转由 Push 类型固定。
- 后台参考客诉后台，复用 `x-admin-passcode` 鉴权。
- 当前素材有 160 条文案、111 张图；110 条文案有图，其中 `W-GALE-03` 有两张图，50 条文案暂无图。
- 111 张现有插图已上传至私有 Bucket `ommo-app-assets-dev/push-cards/`；卡片和管理接口按需返回有效期 24 小时的 OSS 签名 URL。
- 每个问候 30 分钟窗口补到 14 条：早间补 50 条、午间补 50 条、晚间补 77 条，共补充 177 条。
- 每个天气分类补到 14 条，共补充 120 条。

### 3.2 接口

管理页面：

```text
GET /push-contents
```

所有管理接口均携带 `x-admin-passcode`。

#### （新增）查询文案 GET /api/admin/push-contents

```TypeScript
interface PushContentListHeaders {
  "x-admin-passcode": string  // 管理员口令
}

interface PushContentListQuery {
  pushType?: "greeting" | "weather" | "diary" | "recall" // Push 类型
  deliveryScene?: string       // 投放场景，例如 GREETING_0700
  enabled?: boolean            // 是否启用
  keyword?: string             // 搜索编号、标题或正文
  limit?: number               // 每页数量，默认 30
  offset?: number              // 分页偏移，默认 0
}

interface PushContentListResponse {
  success: boolean
  message: string
  data: {
    items: Array<{
      id: number
      contentNo: string
      pushType: "greeting" | "weather" | "diary" | "recall"
      deliveryScene: string
      title: string
      body: string
      enabled: boolean
      images: Array<{
        imageId: number
        imageUrl: string
      }>
      createdAt: string
      updatedAt: string
    }>
    total: number
  } | null
}
```

#### （新增）新增文案 POST /api/admin/push-contents

```TypeScript
interface PushContentCreateHeaders {
  "x-admin-passcode": string
}

interface PushContentCreateRequest {
  contentNo: string
  pushType: "greeting" | "weather" | "diary" | "recall"
  deliveryScene: string
  title: string
  body: string
  enabled: boolean
}

interface PushContentCreateResponse {
  success: boolean
  message: string
  data: {
    id: number                 // 新增文案 ID
  } | null
}
```

#### （新增）编辑文案 PUT /api/admin/push-contents/{id}

```TypeScript
interface PushContentUpdateHeaders {
  "x-admin-passcode": string
}

interface PushContentUpdateRouteParam {
  id: number                   // 文案 ID
}

interface PushContentUpdateRequest {
  contentNo: string
  pushType: "greeting" | "weather" | "diary" | "recall"
  deliveryScene: string
  title: string
  body: string
  enabled: boolean
}

interface PushContentUpdateResponse {
  success: boolean
  message: string
  data: null
}
```

#### （新增）停用文案 DELETE /api/admin/push-contents/{id}

```TypeScript
interface PushContentDisableHeaders {
  "x-admin-passcode": string
}

interface PushContentDisableRouteParam {
  id: number                   // 文案 ID
}

interface PushContentDisableResponse {
  success: boolean
  message: string
  data: null
}
```

#### （新增）上传插图 POST /api/admin/push-contents/{id}/images

请求使用 `multipart/form-data`。

```TypeScript
interface PushContentImageUploadHeaders {
  "x-admin-passcode": string
}

interface PushContentImageUploadRouteParam {
  id: number                   // 文案 ID
}

interface PushContentImageUploadRequest {
  file: File                   // PNG、JPG、JPEG 或 WEBP
}

interface PushContentImageUploadResponse {
  success: boolean
  message: string
  data: {
    imageId: number
    imageUrl: string
  } | null
}
```

#### （新增）移除插图 DELETE /api/admin/push-contents/{id}/images/{imageId}

```TypeScript
interface PushContentImageRemoveHeaders {
  "x-admin-passcode": string
}

interface PushContentImageRemoveRouteParam {
  id: number                   // 文案 ID
  imageId: number              // 插图 ID
}

interface PushContentImageRemoveResponse {
  success: boolean
  message: string
  data: null
}
```

## 4. 推送计划与发送

### 4.1 每日问候

- 只为最近 72 小时内活跃的用户生成计划。
- 每天生成 1 条或 2 条，各占 50%；早、午、晚不重复选择。
- 在所选大时段中随机选择一个 30 分钟窗口，再从对应文案池中选择一条 14 天内未使用的文案，并在窗口内随机发送时间。
- 同一用户同一天只生成一次，生成后文案、插图和时间不变。
- 没有可用文案时取消该条，不补发。
- 发送时检查活跃、通知权限、文案状态和对话状态；不满足时取消。
- 对话状态从收到用户消息开始，到完整回复结束后 5 秒为止。
- 问候发送前先写入一条 `assistant/text` 聊天消息；重试复用原消息。
- 天气 Push 成功后，取消随后 30 分钟内最近的一条问候。

发送窗口：

```text
早：07:00、07:30、08:00、08:30、09:00
午：11:00、11:30、12:00、12:30、13:00
晚：18:00、18:30、19:00、19:30、20:00、20:30、21:00
```

### 4.2 天气预警

- 天气 Push 以和风天气官方预警为触发依据。
- 仅处理最近 24 小时内活跃、有位置、Token 可用且通知权限开启的用户。
- 用户本地时间 07:00～23:00，每 30 分钟检查一次。
- 经纬度保留两位小数，同一位置每轮只请求一次。
- 只处理新增或更新、中等及以上严重程度、需要立即或尽快处理、且能映射到已有天气分类的预警。
- 多条预警只处理最严重的一条。
- 同一预警不重复发送；每人每天最多一条天气 Push。
- 从对应分类中选择 14 天内未使用的文案。
- 将 `effectiveTime、onsetTime、expireTime、headline、description、criteria、responseTypes、instruction` 保存到本次 Push 的卡片快照，通过卡片接口完整返回；客户端自行决定哪些字段进入视图。

#### 接口：和风天气实时预警 GET {QWEATHER_API_HOST}/weatheralert/v1/current/{latitude}/{longitude}

该接口仅由后端调用。

```TypeScript
interface QWeatherAlertHeaders {
  "X-QW-Api-Key": string       // 和风天气 API Key
}

interface QWeatherAlertRouteParam {
  latitude: number
  longitude: number
}

interface QWeatherAlertResponse {
  metadata: {
    zeroResult: boolean         // 是否无预警
  }
  alerts: Array<{
    id: string                 // 预警 ID
    issuedTime: string         // 发布时间
    messageType: {
      code: "alert" | "update" | "cancel"
    }
    eventType: {
      name: string             // 预警类型名称
      code: string             // 预警类型编码
    }
    urgency: "immediate" | "expected" | "future" | "past" | "unknown" | null
    severity: "extreme" | "severe" | "moderate" | "minor" | "unknown" | null
    effectiveTime: string | null
    onsetTime: string | null
    expireTime: string | null
    headline: string | null
    description: string | null
    criteria: string | null
    responseTypes: string[]
    instruction: string | null
  }>
}
```

### 4.3 日记 Push

现有能力：

- 生产环境已在用户本地时间 23:00 生成当日日记。
- 同一篇日记最多成功推送一次，失败后按现有机制重试。

本期改动：

- 日记在 23:30 前生成成功，并且用户最近 72 小时内活跃、通知权限开启且尚未查看时，立即发送 Push。
- 23:30 后生成成功时只保留日记，不发送 Push。
- 日记 Push 发出后至次日 07:00，拦截其他主动 Push。

### 4.4 7/15/30 天召回

- 按用户本地自然日计算未活跃天数。
- 未活跃第 7、15、30 天的本地时间 20:00 各发送一次；30 天后停止召回。
- 发送时检查 Token 和通知权限。
- 用户重新打开 App 后重新计算周期。
- 不提前创建未来任务；到点查询符合条件的用户并发送。
- 从对应召回文案池中选择 14 天内未使用的文案。

## 5. Push 展示、跳转和事件上报

### 5.1 需求细节

- Push 点击后根据 Payload 中的 `pushId` 查询卡片，再进入首页弹框，展示标题、正文和插图。
- 不展示收藏、保存、分享和记忆碎片入口。
- 问候进入聊天页后刷新历史，并定位已有的问候消息。
- Payload 解析或卡片查询失败时进入首页；插图加载失败时显示占位图。

| 类型 | 按钮 | 跳转 |
|---|---|---|
| 问候 | 和满仓聊聊 | 对应聊天消息 |
| 天气 | 知道啦 | 关闭弹框 |
| 天气 | 查看天气 | 天气页 |
| 日记 | 看看今天的日记 | 当日日记页 |
| 召回 | 回来坐坐 | 首页 |

### 5.2 Push Payload

```TypeScript
type ProactivePushPayload =
  | {
      pushId: string
      type: "greeting"
    }
  | {
      pushId: string
      type: "weather"
    }
  | {
      pushId: string
      type: "diary"
      diaryDate?: string        // 兼容旧版客户端
      ts?: number               // 兼容旧版客户端
    }
  | {
      pushId: string
      type: "recall"
    }
```

### 5.3 接口

#### （新增）获取 Push 卡片 GET /api/push/{pushId}/card

卡片内容在生成 Push 任务时形成快照并持久化；接口读取该快照，不依赖内存缓存。

```TypeScript
interface PushCardRouteParam {
  pushId: string               // Push 唯一编号
}

interface PushCardHeaders {
  "x-auth-token": string       // 登录令牌
}

interface PushCardResponse {
  success: boolean
  message: string
  data: {
    pushId: string
    type: "greeting" | "weather" | "diary" | "recall"
    title: string
    body: string
    imageUrl: string
    imageVersion: number
    cta: {
      label: string
      action: "open_chat" | "open_weather" | "open_diary" | "open_home"
      params: Record<string, string | number>
    }
    greeting: {
      contentNo: string
      chatMessageId: number
    } | null
    weather: {
      effectiveTime: string | null
      onsetTime: string | null
      expireTime: string | null
      headline: string | null
      description: string | null
      criteria: string | null
      responseTypes: string[]
      instruction: string | null
    } | null
    diary: {
      diaryDate: string
      ts: number
    } | null
    recall: {
      inactiveDays: 7 | 15 | 30
    } | null
  } | null
}
```

只有当前 `type` 对应的分类型字段有值，其余三个字段返回 `null`。接口校验 `pushId` 所属用户；卡片不存在或不属于当前用户时返回 404。

#### 上报 Push 事件 POST /api/client/event

后端接口已经存在，本期只增加客户端事件约定。

```TypeScript
interface PushClientEventHeaders {
  "x-auth-token": string
}

interface PushClientEventRequest {
  events: Array<{
    type: "push"
    subtype: "push_clicked" | "card_exposed" | "card_cta_clicked"
    pushId: string
    pushType: "greeting" | "weather" | "diary" | "recall"
    ts: number                  // 客户端毫秒时间戳
  }>
}

interface PushClientEventResponse {
  success: boolean
  accepted: number
  message?: string
}
```
