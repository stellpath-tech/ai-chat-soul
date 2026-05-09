# ai-chat-soul 压测工具

对 `/message` + `/stream` 端点进行压力测试，模拟多用户并发聊天。

## 依赖

```bash
brew install k6 jq
# Python 依赖（阶梯加压图表需要）
pip install matplotlib pandas
```

## 两种模式

### 1. 固定并发模式 (`run.sh`)

固定 N 个并发用户持续压测，输出 Markdown 表格报告。

```bash
# 默认：10 并发，60 秒
./bench/run.sh

# 指定参数
./bench/run.sh --url http://36.103.199.211:9899 --vus 50 --duration 300s
```

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--url` | `http://localhost:9899` | 目标服务地址 |
| `--vus` | `10` | 并发虚拟用户数（每个用户独立 session） |
| `--duration` | `60s` | 持续时间，支持 `30s` / `5m` / `1h` |
| `--ramp-up` | 关闭 | 启用阶梯加压（30% → 70% → 100% → 持续 → 收尾） |
| `--sse-timeout` | `120` | SSE 连接超时秒数 |

输出：
- `bench/results/result_<时间戳>.json` — k6 原始数据
- `bench/results/report_<时间戳>.md` — Markdown 报告（含分位数表格 + 质量门判定）

### 2. 阶梯加压模式 (`stress.sh`)

从起始并发逐步加压到目标��发，自动生成**时序图表**，找到性能劣化拐点。

```bash
# 从 2 并发线性加到 30，分 6 个台阶，每台阶 60 秒
./bench/stress.sh --start 2 --end 30 --steps 6

# 指数加压到 100（前慢后快）
./bench/stress.sh --start 2 --end 100 --steps 8 --mode power

# 指定远程地址 + 自定义台阶时长
./bench/stress.sh --url http://36.103.199.211:9899 --start 5 --end 50 --step-duration 90s
```

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--url` | `http://localhost:9899` | 目标服务地址 |
| `--start` | `2` | 起始并发数 |
| `--end` | `30` | 结束并发数 |
| `--steps` | `6` | 分几个台阶加压 |
| `--step-duration` | `60s` | 每个台阶持续时间 |
| `--mode` | `linear` | 加压方式：`linear`（等差）/ `power`（指数） |
| `--sse-timeout` | `120` | SSE 连接超时秒数 |

输出（在 `bench/results/stress_<时间戳>/` 目录下）：
- `overview.png` — 性能概览（4 子图：VUs + POST 响应时间 + TTFB + 流时长）
- `errors.png` — 错误率趋势（双 Y 轴：错误率 + VUs）
- `throughput.png` — 吞吐量趋势（双 Y 轴：完成数/min + VUs）
- `stages.png` — 各阶段对比柱状图（TTFB / 流时长 / 错误率）
- `report.md` — Markdown 报告（含各阶段对比表格 + 自动劣化检测）

所有时序图上用**垂直虚线**标出每次加压的时间点和对应 VU 数。

## 测试流程

每个虚拟用户循环执行：

1. `POST /message` — 发送随机生成的消息（每次内容不同，防止缓存）
2. `GET /stream?request_id=xxx` — 消费 SSE 流直到收到 `type=done`
3. 记录指标，等待 0.5-2s 模拟思考时间，进入下一轮

## 报告指标说明

| 指标 | 含义 |
|------|------|
| POST /message 响应时间 | 发送消息到收到 request_id 的耗时（通常几毫秒，因为后端异步处理） |
| SSE TTFB | 连上 SSE 到收到第一个 token 的延迟，反映模型推理启动时间 |
| SSE 流总时长 | 从 SSE 连接到收到完成事件的端到端耗时 |
| 错误率 | POST 失败或 SSE 流中断/超时的比例 |
| P50 / P90 / P95 | 排第 50%/90%/95% 的值。P95=3s 意味着 95% 请求在 3s 内完成 |

## 示例

```bash
# 轻量验证（1 人跑 10 秒）
./bench/run.sh --vus 1 --duration 10s

# 中等压力（20 人跑 2 分钟）
./bench/run.sh --vus 20 --duration 120s

# 阶梯加压找拐点（2→50 人，线性 6 阶，每阶 60s）
./bench/stress.sh --start 2 --end 50 --steps 6

# 指数加压（前慢后快，适合找极限）
./bench/stress.sh --start 2 --end 100 --steps 8 --mode power --step-duration 90s
```
