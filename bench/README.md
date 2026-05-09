# ai-chat-soul 压测工具

对 `/message` + `/stream` 端点进行压力测试，模拟多用户并发聊天。

## 依赖

```bash
brew install k6 jq
```

## 快速开始

```bash
# 默认：10 并发，60 秒
./bench/run.sh

# 指定参数
./bench/run.sh --url http://36.103.199.211:9899 --vus 50 --duration 300s
```

## 参数说明

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--url` | `http://localhost:9899` | 目标服务地址 |
| `--vus` | `10` | 并发虚拟用户数（每个用户独立 session） |
| `--duration` | `60s` | 持续时间，支持 `30s` / `5m` / `1h` |
| `--ramp-up` | 关闭 | 启用阶梯加压（30% → 70% → 100% → 持续 → 收尾） |
| `--sse-timeout` | `120` | SSE 连接超时秒数 |

## 测试流程

每个虚拟用户循环执行：

1. `POST /message` — 发送随机生成的消息（每次内容不同，防止缓存）
2. `GET /stream?request_id=xxx` — 消费 SSE 流直到收到 `type=done`
3. 记录指标，等待 0.5-2s 模拟思考时间，进入下一轮

## 输出

运行结束后自动在 `bench/results/` 生成：

- `result_<时间戳>.json` — k6 原始指标数据
- `report_<时间戳>.md` — Markdown 格式报告

## 报告包含

- **POST /message 响应时间**: min / avg / P50 / P90 / P95 / P99 / max
- **SSE 首字节延迟 (TTFB)**: 首个 token 到达时间，反映模型推理启动延迟
- **SSE 流总时长**: 完整回复的端到端耗时
- **SSE 事件统计**: 每个流的事件数分布
- **错误率**: message 和 stream 分别统计
- **质量门判定**: P95 < 5s、错误率 < 10% 等阈值检查

## 示例

```bash
# 轻量验证
./bench/run.sh --vus 1 --duration 10s

# 中等压力
./bench/run.sh --vus 20 --duration 120s

# 高压 + 阶梯加压
./bench/run.sh --vus 100 --duration 300s --ramp-up

# 压远程服务器
./bench/run.sh --url http://36.103.199.211:9899 --vus 50 --duration 5m
```
