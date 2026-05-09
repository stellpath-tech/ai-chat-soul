#!/usr/bin/env bash
# ─── 压测报告生成器 ─────────────────────────────────────────────
# 从 k6 JSON summary 提取指标，生成 Markdown 报告
# 用法: ./report.sh <json_file> <output_file> <url> <vus> <duration> <ramp_up>
set -euo pipefail

JSON_FILE="${1:?缺少 JSON 文件路径}"
OUTPUT="${2:?缺少输出文件路径}"
URL="${3:-http://localhost:9899}"
VUS="${4:-10}"
DURATION="${5:-60s}"
RAMP_UP="${6:-false}"

if [[ ! -f "${JSON_FILE}" ]]; then
  echo "错误: JSON 文件不存在: ${JSON_FILE}"
  exit 1
fi

if ! command -v jq &>/dev/null; then
  echo "错误: 需要 jq, 请运行 brew install jq"
  exit 1
fi

# ─── 辅助函数: 安全提取指标 ────────────────────────────────────
metric() {
  local path="$1"
  local default="${2:-N/A}"
  local val
  val=$(jq -r "${path} // empty" "${JSON_FILE}" 2>/dev/null)
  if [[ -z "${val}" || "${val}" == "null" ]]; then
    echo "${default}"
  else
    echo "${val}"
  fi
}

# 格式化毫秒为可读时间
fmt_ms() {
  local val="$1"
  if [[ "${val}" == "N/A" ]]; then
    echo "N/A"
    return
  fi
  local ms
  ms=$(echo "${val}" | awk '{printf "%.0f", $1}')
  if (( ms >= 60000 )); then
    echo "${ms}" | awk '{printf "%.1fmin", $1/60000}'
  elif (( ms >= 1000 )); then
    echo "${ms}" | awk '{printf "%.2fs", $1/1000}'
  else
    echo "${ms}ms"
  fi
}

# ─── 提取指标 ──────────────────────────────────────────────────

# POST /message 响应时间
msg_avg=$(metric '.metrics.message_send_duration.values.avg')
msg_p50=$(metric '.metrics.message_send_duration.values.med')
msg_p90=$(metric '.metrics.message_send_duration.values["p(90)"]')
msg_p95=$(metric '.metrics.message_send_duration.values["p(95)"]')
msg_p99=$(metric '.metrics.message_send_duration.values["p(99)"]')
msg_max=$(metric '.metrics.message_send_duration.values.max')
msg_min=$(metric '.metrics.message_send_duration.values.min')

# SSE TTFB（首字节延迟）
ttfb_avg=$(metric '.metrics.stream_ttfb.values.avg')
ttfb_p50=$(metric '.metrics.stream_ttfb.values.med')
ttfb_p90=$(metric '.metrics.stream_ttfb.values["p(90)"]')
ttfb_p95=$(metric '.metrics.stream_ttfb.values["p(95)"]')
ttfb_p99=$(metric '.metrics.stream_ttfb.values["p(99)"]')
ttfb_max=$(metric '.metrics.stream_ttfb.values.max')

# SSE 总时长
sse_avg=$(metric '.metrics.stream_total_duration.values.avg')
sse_p50=$(metric '.metrics.stream_total_duration.values.med')
sse_p90=$(metric '.metrics.stream_total_duration.values["p(90)"]')
sse_p95=$(metric '.metrics.stream_total_duration.values["p(95)"]')
sse_p99=$(metric '.metrics.stream_total_duration.values["p(99)"]')
sse_max=$(metric '.metrics.stream_total_duration.values.max')

# SSE 事件数
evt_avg=$(metric '.metrics.stream_event_count.values.avg')
evt_min=$(metric '.metrics.stream_event_count.values.min')
evt_max=$(metric '.metrics.stream_event_count.values.max')

# 错误率
msg_err=$(metric '.metrics.message_errors.values.rate' '0')
stream_err=$(metric '.metrics.stream_errors.values.rate' '0')

# 完成数
completed=$(metric '.metrics.completed_chats.values.count' '0')

# HTTP 总请求
http_reqs=$(metric '.metrics.http_reqs.values.count' '0')
http_rps=$(metric '.metrics.http_reqs.values.rate' '0')

# 总迭代
iterations=$(metric '.metrics.iterations.values.count' '0')
iter_rate=$(metric '.metrics.iterations.values.rate' '0')

# k6 checks
checks_passes=$(metric '.metrics.checks.values.passes' '0')
checks_fails=$(metric '.metrics.checks.values.fails' '0')

# Thresholds
thresholds_passed=true
for key in $(jq -r '.metrics | to_entries[] | select(.value.thresholds != null) | .key' "${JSON_FILE}" 2>/dev/null); do
  for t in $(jq -r ".metrics.\"${key}\".thresholds | to_entries[] | .value.ok" "${JSON_FILE}" 2>/dev/null); do
    if [[ "${t}" == "false" ]]; then
      thresholds_passed=false
      break 2
    fi
  done
done

# ─── 计算错误率百分比 ──────────────────────────────────────────
msg_err_pct=$(echo "${msg_err}" | awk '{printf "%.2f", $1 * 100}')
stream_err_pct=$(echo "${stream_err}" | awk '{printf "%.2f", $1 * 100}')
http_rps_fmt=$(echo "${http_rps}" | awk '{printf "%.2f", $1}')
iter_rate_fmt=$(echo "${iter_rate}" | awk '{printf "%.2f", $1}')

# ─── 生成报告 ──────────────────────────────────────────────────
cat > "${OUTPUT}" <<REPORT
# 压测报告

**生成时间**: $(date '+%Y-%m-%d %H:%M:%S')

## 测试配置

| 参数 | 值 |
|------|-----|
| 目标地址 | ${URL} |
| 并发用户 (VUs) | ${VUS} |
| 持续时间 | ${DURATION} |
| 加压模式 | $([ "$RAMP_UP" = "true" ] && echo "阶梯加压" || echo "固定并发") |

## 总体概要

| 指标 | 值 |
|------|-----|
| 完成的完整聊天数 | ${completed} |
| 总迭代次数 | ${iterations} |
| 迭代速率 | ${iter_rate_fmt}/s |
| HTTP 请求总数 | ${http_reqs} |
| HTTP 请求速率 | ${http_rps_fmt} req/s |
| Checks 通过/失败 | ${checks_passes} / ${checks_fails} |

## POST /message 响应时间

| 指标 | 值 |
|------|-----|
| 最小 | $(fmt_ms "${msg_min}") |
| 平均 | $(fmt_ms "${msg_avg}") |
| P50 | $(fmt_ms "${msg_p50}") |
| P90 | $(fmt_ms "${msg_p90}") |
| P95 | $(fmt_ms "${msg_p95}") |
| P99 | $(fmt_ms "${msg_p99}") |
| 最大 | $(fmt_ms "${msg_max}") |
| 错误率 | ${msg_err_pct}% |

## SSE 首字节延迟 (TTFB)

首次收到模型生成内容的延迟，反映模型推理启动时间。

| 指标 | 值 |
|------|-----|
| 平均 | $(fmt_ms "${ttfb_avg}") |
| P50 | $(fmt_ms "${ttfb_p50}") |
| P90 | $(fmt_ms "${ttfb_p90}") |
| P95 | $(fmt_ms "${ttfb_p95}") |
| P99 | $(fmt_ms "${ttfb_p99}") |
| 最大 | $(fmt_ms "${ttfb_max}") |

## SSE 流总时长

从发起 SSE 连接到收到完成事件的总耗时。

| 指标 | 值 |
|------|-----|
| 平均 | $(fmt_ms "${sse_avg}") |
| P50 | $(fmt_ms "${sse_p50}") |
| P90 | $(fmt_ms "${sse_p90}") |
| P95 | $(fmt_ms "${sse_p95}") |
| P99 | $(fmt_ms "${sse_p99}") |
| 最大 | $(fmt_ms "${sse_max}") |
| 错误率 | ${stream_err_pct}% |

## SSE 事件统计

| 指标 | 值 |
|------|-----|
| 平均事件数/流 | $(echo "${evt_avg}" | awk '{printf "%.1f", $1}' 2>/dev/null || echo "N/A") |
| 最少事件数 | ${evt_min} |
| 最多事件数 | ${evt_max} |

## 质量门判定

| 检查项 | 阈值 | 结果 |
|--------|------|------|
| POST /message P95 | < 5s | $([ "$(echo "${msg_p95}" | awk '{print ($1 < 5000)}')" = "1" ] && echo "PASS ✅" || echo "FAIL ❌") |
| Message 错误率 | < 10% | $([ "$(echo "${msg_err_pct}" | awk '{print ($1 < 10)}')" = "1" ] && echo "PASS ✅" || echo "FAIL ❌") |
| Stream 错误率 | < 10% | $([ "$(echo "${stream_err_pct}" | awk '{print ($1 < 10)}')" = "1" ] && echo "PASS ✅" || echo "FAIL ❌") |
| k6 Thresholds | 全部通过 | $([ "${thresholds_passed}" = "true" ] && echo "PASS ✅" || echo "FAIL ❌") |

---
*由 bench/report.sh 自动生成*
REPORT

echo "报告已写入: ${OUTPUT}"
