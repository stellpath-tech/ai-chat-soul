# 指定日期日记批量补偿运维手册

> 当前状态：接口代码位于 PR 分支，合并并部署后方可使用。本文所有操作均针对已部署该接口的环境。

## 一、批量补偿接口 @TBD

### （新增）触发指定日期全部日记任务检查与补偿 POST /api/admin/diary/retry

接口异步检查指定日期的全部活跃用户日记任务，只调度需要补偿的任务，不等待所有任务执行完成。

#### 鉴权

推荐通过请求头传递管理员口令：

```http
X-Admin-Passcode: ${adminPasscode}
```

接口同时兼容 query 参数或请求体中的 `passcode`，运维调用统一使用请求头，避免口令进入 URL 日志。

#### 请求结构

```ts
interface RequestBody {
  + targetDate: string; // 必填，补偿日期，格式 YYYY-MM-DD，例如 2026-08-17
  + passcode?: string;  // 可选，管理员口令；运维调用不使用此字段，改用 X-Admin-Passcode
}
```

#### 响应结构

```ts
interface ApiResponse<T> {
  success: boolean; // 接口是否成功受理
  message: string;  // Accepted、Already running、unauthorized 等结果说明
  data: T | null;   // 成功时为批次受理信息，失败时为 null
}

interface DiaryDateRetryData {
  + targetDate: string;          // 已规范化的补偿日期，格式 YYYY-MM-DD
  + started: boolean;            // true 表示已启动后台批次，false 表示同日期批次已运行
  + reason?: 'already_running';  // started=false 时返回
}
```

#### HTTP 状态

| HTTP 状态 | `success` | 场景 |
|---|---:|---|
| `202 Accepted` | `true` | 后台补偿批次已启动 |
| `200 OK` | `true` | 同一日期已有批次运行，本次没有重复启动 |
| `401 Unauthorized` | `false` | 管理员口令缺失或错误 |
| `200 OK` | `false` | `targetDate` 缺失、格式错误、JSON 非法或服务端异常；遵循项目现有响应风格 |

#### 调用示例

```bash
curl -X POST 'http://127.0.0.1:9899/api/admin/diary/retry' \
  -H 'Content-Type: application/json' \
  -H 'X-Admin-Passcode: ${ADMIN_PASSCODE}' \
  -d '{"targetDate":"2026-08-17"}'
```

启动成功：

```json
{
  "success": true,
  "message": "Accepted",
  "data": {
    "targetDate": "2026-08-17",
    "started": true
  }
}
```

同日期批次已运行：

```json
{
  "success": true,
  "message": "Already running",
  "data": {
    "targetDate": "2026-08-17",
    "started": false,
    "reason": "already_running"
  }
}
```

## 二、任务检查规则 @TBD

接口遍历指定日期的全部活跃用户，根据 `user_diary.state`、任务启动时间和图片结果决定是否补偿。

| 当前情况 | 动作 | 汇总 action |
|---|---|---|
| 没有 `user_diary` 记录 | 创建任务并生成 | `created_missing` |
| `state=SKIPPED` | 重置任务并重新生成 | `retried_incomplete` |
| `state=GENERATING`，尚未开始或正在等待 `next_retry_at` | 取消等待，立即重新生成 | `retried_incomplete` |
| `state=GENERATING`，已运行超过 30 分钟 | 视为超时，重置后重新生成 | `retried_stale` |
| `state=GENERATING`，启动未超过 30 分钟 | 跳过，避免重复并发 | `skipped_running` |
| `state=DONE`，图片功能关闭 | 跳过 | `skipped_done` |
| `state=DONE`，图片功能开启且 `imageUrls` 非空 | 跳过 | `skipped_done` |
| `state=DONE`，图片功能开启但 `imageUrls` 为空 | 重置整篇日记并重新生成 | `retried_missing_image` |
| 用户已停用 | 跳过 | `skipped_inactive` |

注意：

- 这里只检查日记生成状态 `user_diary.state`，不会因为 `push_state=SKIPPED` 而重试已经完成的日记。
- 图片补偿会重新生成整篇日记正文和图片，不是只生成图片。
- 从未成功推送的任务在补偿成功后正常发送通知；旧 `DONE` 无图任务如果已经是
  `push_state=SENT`，重做成功后不会重复推送。
- 同一进程内，同一日期同时只允许一个补偿批次。
- 常驻 worker 每轮先处理当天正常任务，再处理 `next_retry_at` 已到期的历史任务。

## 三、标准操作流程 @TBD

### 1. 操作前确认

1. 确认目标日期，格式必须为 `YYYY-MM-DD`。该日期直接作为所有用户的日记业务日期，不做时区转换。
2. 确认是否开启 `diary_image_enabled`。开启时，历史 `DONE` 但无图片的任务会被整篇重做。
3. 确认通知影响：从未推送成功的任务会在补偿成功后发送通知；已经推送过的旧
   `DONE` 无图任务不会重复发送。
4. 首次使用或补偿范围较大时，先备份 `~/cow/data/soul.db`。
5. 确认当前没有针对同一日期的人工补偿操作。

### 2. 调用接口

执行第一节的 `curl`。只有收到以下结果才表示新批次已启动：

```json
{
  "success": true,
  "message": "Accepted",
  "data": {
    "targetDate": "2026-08-17",
    "started": true
  }
}
```

接口返回后不可通过当前接口取消批次。日期填错时不要重复调用，应立即联系服务负责人评估正在执行的任务。

### 3. 确认批次开始

Grafana Loki 查询：

```logql
{job="ai-chat-soul", log_type="runlog"}
|= "[Diary] date retry batch starting"
|= "date=2026-08-17"
```

开始日志示例：

```text
[Diary] date retry batch starting date=2026-08-17 checked=19 jobs=2 workers=2 actions={'skipped_done': 17, 'retried_missing_image': 1, 'created_missing': 1}
```

字段含义：

| 字段 | 含义 |
|---|---|
| `checked` | 本次检查的活跃用户数 |
| `jobs` | 实际提交执行的任务数 |
| `workers` | 本批次使用的并发线程数 |
| `actions` | 每种检查结果的数量 |

### 4. 等待批次结束

Grafana Loki 查询：

```logql
{job="ai-chat-soul", log_type="runlog"}
|= "[Diary] date retry batch complete"
|= "targetDate': '2026-08-17"
```

结束日志示例：

```text
[Diary] date retry batch complete result={'targetDate': '2026-08-17', 'checked': 19, 'scheduled': 2, 'processed': 2, 'actions': {'skipped_done': 17, 'retried_missing_image': 1, 'created_missing': 1}}
```

字段含义：

| 字段 | 含义 |
|---|---|
| `checked` | 检查的活跃用户数 |
| `scheduled` | 实际提交执行的任务数 |
| `processed` | 已执行并返回结果的任务数；不等同于成功数 |
| `actions` | 每种检查结果的数量 |

判断批次线程是否全部返回：

```text
processed == scheduled
```

该条件只表示所有任务均已返回，仍需继续核对成功和失败日志。

### 5. 核对任务结果

成功日志：

```logql
{job="ai-chat-soul", log_type="runlog"}
|= "[Diary] generated"
|= "date=2026-08-17"
```

失败日志：

```logql
{job="ai-chat-soul", log_type="runlog"}
|= "[Diary] generation failed"
|= "date=2026-08-17"
```

图片请求日志：

```logql
{job="ai-chat-soul", log_type="runlog"}
|= "[Diary] image request"
```

拿到某条日志的 `trace_id` 后，可查询同一张图片的全部尝试：

```logql
{job="ai-chat-soul", log_type="runlog"}
|= "trace_id=${traceId}"
```

## 四、异常处理 @TBD

### 接口返回 unauthorized

- 检查 `X-Admin-Passcode` 是否缺失。
- 确认使用请求头传参，不要把口令拼入日志或聊天消息。

### 接口返回 Invalid targetDate

- 必须使用 `YYYY-MM-DD`。
- 示例：`2026-08-17`。

### 接口返回 Already running

- 不需要重复调用。
- 使用第三节 Loki 查询确认已有批次进度。
- 等待出现 `date retry batch complete` 后再决定是否需要下一次补偿。

### 出现 date retry batch failed

查询完整异常：

```logql
{job="ai-chat-soul", log_type="runlog"}
|= "[Diary] date retry batch failed"
|= "date=2026-08-17"
```

该日志表示批次检查或线程调度本身异常。修复原因后可再次调用相同日期接口。

### processed 等于 scheduled，但存在 generation failed

`processed` 只代表任务返回，不代表成功。日记失败后可能进入：

```text
state=GENERATING + next_retry_at
```

或在达到最大次数后进入：

```text
state=SKIPPED
```

常驻 worker 会在 `next_retry_at` 到期后自动重试，并且优先处理当天正常任务。先根据
`generation failed` 的异常处理根因并观察自动重试；只有任务进入 `SKIPPED`，或需要明确
取消当前退避等待并立即重试时，才再次调用批量补偿接口。

## 五、Prometheus 核对 @TBD

日记结果：

```promql
sum by (result, mode) (increase(diary_generation_total[30m]))
```

日记耗时 P95：

```promql
histogram_quantile(
  0.95,
  sum by (le, mode) (rate(diary_generation_duration_seconds_bucket[30m]))
)
```

图片最终结果：

```promql
sum by (result) (increase(diary_image_generation_total[30m]))
```

## 六、操作边界 @TBD

- 本接口只允许管理员调用。
- 本接口会修改指定日期需要补偿的 `user_diary` 任务记录。
- 本接口会重新生成候选任务的正文和图片，可能产生模型调用费用。
- 从未推送成功的任务会在补偿成功后发送通知；已发送过的旧 `DONE` 无图任务不会重复推送。
- 本接口不是只读检查，也不是预览接口。
- 当前没有批次取消接口或批次状态接口，运行状态以 Loki 日志为准。
- 不要因为 `push_state=SKIPPED` 重试任务；代码只根据日记生成状态和图片结果决定。
