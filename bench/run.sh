#!/usr/bin/env bash
# ─── ai-chat-soul 压测运行脚本 ──────────────────────────────────
# 用法:
#   ./bench/run.sh                                    # 默认 10 VU, 60s
#   ./bench/run.sh --vus 50 --duration 300s           # 50 并发, 5 分钟
#   ./bench/run.sh --url http://x.x.x.x:9899 --vus 20 --duration 120s
#   ./bench/run.sh --vus 100 --duration 300s --ramp-up
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
RESULTS_DIR="${SCRIPT_DIR}/results"

# ─── 默认参数 ──────────────────────────────────────────────────
URL="http://localhost:9899"
VUS="10"
DURATION="60s"
RAMP_UP="false"
SSE_TIMEOUT="120"

# ─── 解析命令行 ────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --url)        URL="$2";         shift 2 ;;
    --vus)        VUS="$2";         shift 2 ;;
    --duration)   DURATION="$2";    shift 2 ;;
    --ramp-up)    RAMP_UP="true";   shift   ;;
    --sse-timeout) SSE_TIMEOUT="$2"; shift 2 ;;
    -h|--help)
      echo "用法: $0 [选项]"
      echo ""
      echo "选项:"
      echo "  --url URL          目标地址 (默认: http://localhost:9899)"
      echo "  --vus N            并发虚拟用户数 (默认: 10)"
      echo "  --duration TIME    压测持续时间, 如 60s/5m (默认: 60s)"
      echo "  --ramp-up          启用阶梯加压模式"
      echo "  --sse-timeout N    SSE 连接超时秒数 (默认: 120)"
      echo "  -h, --help         显示帮助"
      exit 0
      ;;
    *)
      echo "未知参数: $1 (使用 --help 查看帮助)"
      exit 1
      ;;
  esac
done

# ─── 前置检查 ──────────────────────────────────────────────────
if ! command -v k6 &>/dev/null; then
  echo "❌ k6 未安装。请运行: brew install k6"
  exit 1
fi

if ! command -v jq &>/dev/null; then
  echo "⚠️  jq 未安装，报告生成将受限。建议运行: brew install jq"
fi

# ─── 准备输出目录 ──────────────────────────────────────────────
mkdir -p "${RESULTS_DIR}"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
JSON_FILE="${RESULTS_DIR}/result_${TIMESTAMP}.json"
REPORT_FILE="${RESULTS_DIR}/report_${TIMESTAMP}.md"

# ─── 打印配置 ──────────────────────────────────────────────────
echo "═══════════════════════════════════════════════════════"
echo "  ai-chat-soul 压测"
echo "═══════════════════════════════════════════════════════"
echo "  目标地址:   ${URL}"
echo "  并发用户:   ${VUS}"
echo "  持续时间:   ${DURATION}"
echo "  加压模式:   $([ "$RAMP_UP" = "true" ] && echo "阶梯加压" || echo "固定并发")"
echo "  SSE超时:    ${SSE_TIMEOUT}s"
echo "  结果输出:   ${JSON_FILE}"
echo "═══════════════════════════════════════════════════════"
echo ""

# ─── 运行 k6 ──────────────────────────────────────────────────
k6 run \
  -e BASE_URL="${URL}" \
  -e VUS="${VUS}" \
  -e DURATION="${DURATION}" \
  -e RAMP_UP="${RAMP_UP}" \
  -e SSE_TIMEOUT="${SSE_TIMEOUT}" \
  -e SUMMARY_EXPORT="${JSON_FILE}" \
  "${SCRIPT_DIR}/loadtest.js"

echo ""

# ─── 生成报告 ──────────────────────────────────────────────────
if [[ -f "${JSON_FILE}" ]] && command -v jq &>/dev/null; then
  echo "正在生成压测报告..."
  bash "${SCRIPT_DIR}/report.sh" "${JSON_FILE}" "${REPORT_FILE}" \
    "${URL}" "${VUS}" "${DURATION}" "${RAMP_UP}"
  echo ""
  echo "═══════════════════════════════════════════════════════"
  echo "  报告已生成: ${REPORT_FILE}"
  echo "═══════════════════════════════════════════════════════"
  echo ""
  cat "${REPORT_FILE}"
else
  echo "⚠️  无法生成报告 (JSON 文件不存在或 jq 未安装)"
fi
