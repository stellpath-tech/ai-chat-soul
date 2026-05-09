#!/usr/bin/env bash
# ─── ai-chat-soul 阶梯加压压测 ──────────────────────────────────
# 从 start VU 逐步加压到 end VU，每个台阶持续指定时间，
# 最后生成带时序图表的 Markdown 报告。
#
# 用法:
#   ./bench/stress.sh --start 2 --end 30 --steps 6
#   ./bench/stress.sh --start 2 --end 100 --steps 8 --mode power
#   ./bench/stress.sh --url http://x.x.x.x:9899 --start 5 --end 50 --step-duration 90s
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
RESULTS_DIR="${SCRIPT_DIR}/results"

# ─── 默认参数 ──────────────────────────────────────────────────
URL="http://localhost:9899"
START_VUS="2"
END_VUS="30"
STEPS="6"
STEP_DURATION="60s"
MODE="linear"       # linear | power
SSE_TIMEOUT="120"
RAMP_SECONDS="10"   # 每个台阶爬升时间

# ─── 解析命令行 ────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --url)            URL="$2";            shift 2 ;;
    --start)          START_VUS="$2";      shift 2 ;;
    --end)            END_VUS="$2";        shift 2 ;;
    --steps)          STEPS="$2";          shift 2 ;;
    --step-duration)  STEP_DURATION="$2";  shift 2 ;;
    --mode)           MODE="$2";           shift 2 ;;
    --sse-timeout)    SSE_TIMEOUT="$2";    shift 2 ;;
    -h|--help)
      echo "用法: $0 [选项]"
      echo ""
      echo "选项:"
      echo "  --url URL             目标地址 (默认: http://localhost:9899)"
      echo "  --start N             起始并发数 (默认: 2)"
      echo "  --end N               结束并发数 (默认: 30)"
      echo "  --steps N             台阶数 (默认: 6)"
      echo "  --step-duration TIME  每个台阶持续时间 (默认: 60s)"
      echo "  --mode linear|power   加压模式 (默认: linear)"
      echo "  --sse-timeout N       SSE 超时秒数 (默认: 120)"
      echo "  -h, --help            显示帮助"
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
  echo "错误: k6 未安装。请运行: brew install k6"
  exit 1
fi

if ! command -v /Users/drincanngao/.pyenv/shims/python &>/dev/null; then
  echo "错误: /Users/drincanngao/.pyenv/shims/python 未找到"
  exit 1
fi

# 检查 Python 依赖
/Users/drincanngao/.pyenv/shims/python -c "import matplotlib, pandas" 2>/dev/null || {
  echo "缺少 Python 依赖，正在安装 matplotlib pandas ..."
  pip3 install matplotlib pandas
}

# ─── 解析 step-duration 为秒数 ────────────────────────────────
parse_duration_to_seconds() {
  local d="$1"
  if [[ "${d}" =~ ^([0-9]+)s$ ]]; then
    echo "${BASH_REMATCH[1]}"
  elif [[ "${d}" =~ ^([0-9]+)m$ ]]; then
    echo $(( BASH_REMATCH[1] * 60 ))
  elif [[ "${d}" =~ ^([0-9]+)h$ ]]; then
    echo $(( BASH_REMATCH[1] * 3600 ))
  else
    echo "${d}"  # 假设是纯数字秒
  fi
}

STEP_SECONDS=$(parse_duration_to_seconds "${STEP_DURATION}")

# ─── 计算每个台阶的 VU 数 ────────────────────────────────────
calculate_steps() {
  /Users/drincanngao/.pyenv/shims/python -c "
import json, math, sys

start = int(sys.argv[1])
end   = int(sys.argv[2])
n     = int(sys.argv[3])
mode  = sys.argv[4]

if n <= 1:
    vus_list = [end]
elif mode == 'power':
    # 指数增长: start * r^i = end => r = (end/start)^(1/(n-1))
    if start < 1:
        start = 1
    r = (end / start) ** (1.0 / (n - 1))
    vus_list = [max(1, round(start * r**i)) for i in range(n)]
    vus_list[-1] = end  # 确保最后一个精确
else:
    # 线性增长
    vus_list = [round(start + (end - start) * i / (n - 1)) for i in range(n)]

# 去重（相邻相同的合并）
deduped = [vus_list[0]]
for v in vus_list[1:]:
    if v != deduped[-1]:
        deduped.append(v)

print(json.dumps(deduped))
" "${START_VUS}" "${END_VUS}" "${STEPS}" "${MODE}"
}

VUS_LIST=$(calculate_steps)
ACTUAL_STEPS=$(echo "${VUS_LIST}" | /Users/drincanngao/.pyenv/shims/python -c "import json,sys; print(len(json.load(sys.stdin)))")

# ─── 生成 k6 stages JSON 和 stages_meta.json ─────────────────
generate_stages() {
  /Users/drincanngao/.pyenv/shims/python -c "
import json, sys

vus_list       = json.loads(sys.argv[1])
ramp_seconds   = int(sys.argv[2])
step_seconds   = int(sys.argv[3])
url            = sys.argv[4]
mode           = sys.argv[5]
start          = int(sys.argv[6])
end            = int(sys.argv[7])

k6_stages = []
meta_stages = []
elapsed = 0

for i, vus in enumerate(vus_list):
    # 爬升阶段
    k6_stages.append({'duration': f'{ramp_seconds}s', 'target': vus})
    elapsed += ramp_seconds

    # 持平阶段
    k6_stages.append({'duration': f'{step_seconds}s', 'target': vus})

    meta_stages.append({
        'time_offset_s': elapsed,
        'target_vus': vus,
        'label': f'{vus} VUs',
        'step_index': i,
    })

    elapsed += step_seconds

# 结尾收尾
k6_stages.append({'duration': f'{ramp_seconds}s', 'target': 0})

meta = {
    'stages': meta_stages,
    'url': url,
    'mode': mode,
    'start': start,
    'end': end,
    'ramp_seconds': ramp_seconds,
    'step_seconds': step_seconds,
    'total_duration_s': elapsed + ramp_seconds,
}

# 输出两个 JSON，用 --- 分隔
print(json.dumps(k6_stages))
print('---')
print(json.dumps(meta, indent=2))
" "${VUS_LIST}" "${RAMP_SECONDS}" "${STEP_SECONDS}" "${URL}" "${MODE}" "${START_VUS}" "${END_VUS}"
}

STAGES_OUTPUT=$(generate_stages)
K6_STAGES=$(echo "${STAGES_OUTPUT}" | sed -n '1p')
STAGES_META=$(echo "${STAGES_OUTPUT}" | sed '1d;/^---$/d')

# ─── 准备输出目录 ──────────────────────────────────────────────
mkdir -p "${RESULTS_DIR}"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
CSV_FILE="${RESULTS_DIR}/stress_${TIMESTAMP}.csv"
JSON_FILE="${RESULTS_DIR}/stress_${TIMESTAMP}.json"
META_FILE="${RESULTS_DIR}/stress_${TIMESTAMP}_meta.json"
REPORT_DIR="${RESULTS_DIR}/stress_${TIMESTAMP}"

mkdir -p "${REPORT_DIR}"

# 保存 meta
echo "${STAGES_META}" > "${META_FILE}"

# ─── 打印配置 ──────────────────────────────────────────────────
echo "═══════════════════════════════════════════════════════"
echo "  ai-chat-soul 阶梯加压压测"
echo "═══════════════════════════════════════════════════════"
echo "  目标地址:     ${URL}"
echo "  起始并发:     ${START_VUS}"
echo "  结束并发:     ${END_VUS}"
echo "  台阶数:       ${ACTUAL_STEPS}"
echo "  每台阶持续:   ${STEP_DURATION}"
echo "  加压模式:     ${MODE}"
echo "  SSE超时:      ${SSE_TIMEOUT}s"
echo "  VU 台阶:      ${VUS_LIST}"
echo ""
echo "  CSV 输出:     ${CSV_FILE}"
echo "  报告目录:     ${REPORT_DIR}"
echo "═══════════════════════════════════════════════════════"
echo ""

# ─── 运行 k6 ──────────────────────────────────────────────────
k6 run \
  --out "csv=${CSV_FILE}" \
  -e BASE_URL="${URL}" \
  -e SSE_TIMEOUT="${SSE_TIMEOUT}" \
  -e "STAGES_JSON=${K6_STAGES}" \
  -e SUMMARY_EXPORT="${JSON_FILE}" \
  "${SCRIPT_DIR}/loadtest_stress.js"

echo ""

# ─── 生成图表和报告 ────────────────────────────────────────────
if [[ -f "${CSV_FILE}" ]]; then
  echo "正在生成图表和报告..."
  /Users/drincanngao/.pyenv/shims/python "${SCRIPT_DIR}/plot_report.py" \
    --csv "${CSV_FILE}" \
    --meta "${META_FILE}" \
    --output "${REPORT_DIR}"

  echo ""
  echo "═══════════════════════════════════════════════════════"
  echo "  报告已生成: ${REPORT_DIR}/report.md"
  echo "  图表目录:   ${REPORT_DIR}/"
  echo "═══════════════════════════════════════════════════════"
  echo ""

  # 输出报告文本
  if [[ -f "${REPORT_DIR}/report.md" ]]; then
    cat "${REPORT_DIR}/report.md"
  fi
else
  echo "错误: CSV 文件未生成: ${CSV_FILE}"
  exit 1
fi
