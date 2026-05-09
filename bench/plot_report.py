#!/Users/drincanngao/.pyenv/shims/python
"""
阶梯加压压测 — 图表 + 报告生成器

读取 k6 CSV 时间序列 + stages_meta.json，生成：
  1. overview.png    — 性能概览（VUs + 响应时间 + TTFB + 流时长）
  2. errors.png      — 错误率趋势
  3. throughput.png   — 吞吐量趋势
  4. stages.png       — 各阶段对比柱状图
  5. report.md        — Markdown 报告
"""

import argparse
import json
import os
from datetime import datetime

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker
import numpy as np
import pandas as pd

# ─── 全局样式 ──────────────────────────────────────────────────
plt.rcParams.update({
    "figure.facecolor":  "#fafafa",
    "axes.facecolor":    "#ffffff",
    "axes.grid":         True,
    "grid.alpha":        0.3,
    "grid.linestyle":    "--",
    "font.size":         10,
    "axes.titlesize":    12,
    "axes.labelsize":    10,
    "legend.fontsize":   9,
    "figure.dpi":        150,
})

# 尝试设置中文字体
for font_name in ["PingFang SC", "Heiti SC", "Noto Sans CJK SC", "SimHei", "WenQuanYi Micro Hei"]:
    try:
        plt.rcParams["font.sans-serif"] = [font_name] + plt.rcParams.get("font.sans-serif", [])
        break
    except Exception:
        pass
plt.rcParams["axes.unicode_minus"] = False

# 配色
COLORS = {
    "vus":       "#6366f1",  # indigo
    "avg":       "#3b82f6",  # blue
    "p90":       "#f59e0b",  # amber
    "p95":       "#ef4444",  # red
    "err_msg":   "#ef4444",
    "err_stream":"#f97316",  # orange
    "throughput":"#10b981",  # emerald
    "bar1":      "#3b82f6",
    "bar2":      "#f59e0b",
    "bar3":      "#ef4444",
}

VLINE_COLOR = "#dc2626"   # red-600
VLINE_ALPHA = 0.7


def parse_args():
    p = argparse.ArgumentParser(description="压测图表 + 报告生成")
    p.add_argument("--csv",    required=True, help="k6 CSV 文件路径")
    p.add_argument("--meta",   required=True, help="stages_meta.json 路径")
    p.add_argument("--output", required=True, help="输出目录")
    p.add_argument("--window", type=int, default=10, help="聚合窗口秒数 (默认 10)")
    return p.parse_args()


def load_csv(path):
    """加载 k6 CSV，返回 DataFrame"""
    df = pd.read_csv(path)
    df.columns = df.columns.str.strip()
    df["timestamp"] = pd.to_numeric(df["timestamp"], errors="coerce")
    df = df.dropna(subset=["timestamp"])
    # 归一化时间为相对秒数
    t0 = df["timestamp"].min()
    df["t"] = df["timestamp"] - t0
    return df, t0


def load_meta(path):
    with open(path) as f:
        return json.load(f)


def aggregate_metric(df, metric_name, window_s):
    """按时间窗口聚合某个指标"""
    sub = df[df["metric_name"] == metric_name].copy()
    if sub.empty:
        return pd.DataFrame(columns=["t_bin", "avg", "p90", "p95", "count"])

    sub["metric_value"] = pd.to_numeric(sub["metric_value"], errors="coerce")
    sub = sub.dropna(subset=["metric_value"])
    sub["t_bin"] = (sub["t"] // window_s) * window_s + window_s / 2

    agg = sub.groupby("t_bin")["metric_value"].agg(
        avg="mean",
        p90=lambda x: np.percentile(x, 90),
        p95=lambda x: np.percentile(x, 95),
        count="count",
    ).reset_index()
    return agg


def get_vus_series(df, window_s):
    """提取 VU 数时间序列"""
    sub = df[df["metric_name"] == "vus"].copy()
    if sub.empty:
        return pd.DataFrame(columns=["t_bin", "vus"])
    sub["metric_value"] = pd.to_numeric(sub["metric_value"], errors="coerce")
    sub["t_bin"] = (sub["t"] // window_s) * window_s + window_s / 2
    agg = sub.groupby("t_bin")["metric_value"].agg(vus="max").reset_index()
    return agg


def compute_error_rate(df, metric_name, window_s):
    """按窗口计算错误率（Rate 类型指标：值为 0 或 1）"""
    sub = df[df["metric_name"] == metric_name].copy()
    if sub.empty:
        return pd.DataFrame(columns=["t_bin", "error_rate"])
    sub["metric_value"] = pd.to_numeric(sub["metric_value"], errors="coerce")
    sub["t_bin"] = (sub["t"] // window_s) * window_s + window_s / 2
    agg = sub.groupby("t_bin")["metric_value"].agg(
        error_rate="mean",
    ).reset_index()
    agg["error_rate"] = agg["error_rate"] * 100  # 转百分比
    return agg


def compute_throughput(df, window_s):
    """按窗口计算完成聊天数/分钟"""
    sub = df[df["metric_name"] == "completed_chats"].copy()
    if sub.empty:
        return pd.DataFrame(columns=["t_bin", "chats_per_min"])
    sub["metric_value"] = pd.to_numeric(sub["metric_value"], errors="coerce")
    sub["t_bin"] = (sub["t"] // window_s) * window_s + window_s / 2
    agg = sub.groupby("t_bin")["metric_value"].agg(total="sum").reset_index()
    agg["chats_per_min"] = agg["total"] / window_s * 60
    return agg


def draw_vlines(ax, meta, label_y_ratio=0.92):
    """在子图上画加压时间点虚线 + 标注 VU 数"""
    for s in meta["stages"]:
        t = s["time_offset_s"]
        ax.axvline(x=t, color=VLINE_COLOR, alpha=VLINE_ALPHA,
                   linestyle="--", linewidth=1.2)
        ax.text(t + 2, ax.get_ylim()[1] * label_y_ratio,
                f'{s["target_vus"]} VUs',
                fontsize=8, color=VLINE_COLOR, fontweight="bold",
                va="top", ha="left")


def fmt_time_axis(ax):
    """X 轴格式化为 mm:ss"""
    def fmt_func(x, _):
        m, s = divmod(int(x), 60)
        return f"{m}:{s:02d}"
    ax.xaxis.set_major_formatter(ticker.FuncFormatter(fmt_func))


def fmt_ms_val(v):
    """格式化毫秒为可读值"""
    if pd.isna(v) or v is None:
        return "N/A"
    v = float(v)
    if v >= 60000:
        return f"{v/60000:.1f}min"
    elif v >= 1000:
        return f"{v/1000:.2f}s"
    else:
        return f"{v:.0f}ms"


# ─── 图 1: 性能概览 ───────────────────────────────────────────
def plot_overview(vus_df, msg_df, ttfb_df, sse_df, meta, output_dir):
    fig, axes = plt.subplots(4, 1, figsize=(14, 12), sharex=True,
                             gridspec_kw={"height_ratios": [1, 1.5, 1.5, 1.5]})
    fig.suptitle("Performance Overview", fontsize=14, fontweight="bold", y=0.98)

    # A: VUs
    ax = axes[0]
    if not vus_df.empty:
        ax.fill_between(vus_df["t_bin"], 0, vus_df["vus"],
                        color=COLORS["vus"], alpha=0.15, step="mid")
        ax.step(vus_df["t_bin"], vus_df["vus"], where="mid",
                color=COLORS["vus"], linewidth=1.5, label="Active VUs")
    ax.set_ylabel("VUs")
    ax.set_title("Concurrent Users")
    ax.legend(loc="upper left")
    fmt_time_axis(ax)

    # B: POST /message 响应时间
    ax = axes[1]
    if not msg_df.empty:
        ax.plot(msg_df["t_bin"], msg_df["avg"], color=COLORS["avg"],
                linewidth=1.2, label="avg")
        ax.plot(msg_df["t_bin"], msg_df["p90"], color=COLORS["p90"],
                linewidth=1.2, label="P90")
        ax.fill_between(msg_df["t_bin"], msg_df["avg"], msg_df["p90"],
                        alpha=0.1, color=COLORS["p90"])
    ax.set_ylabel("ms")
    ax.set_title("POST /message Response Time")
    ax.legend(loc="upper left")
    draw_vlines(ax, meta)
    fmt_time_axis(ax)

    # C: SSE TTFB
    ax = axes[2]
    if not ttfb_df.empty:
        ax.plot(ttfb_df["t_bin"], ttfb_df["avg"], color=COLORS["avg"],
                linewidth=1.2, label="avg")
        ax.plot(ttfb_df["t_bin"], ttfb_df["p90"], color=COLORS["p90"],
                linewidth=1.2, label="P90")
        ax.fill_between(ttfb_df["t_bin"], ttfb_df["avg"], ttfb_df["p90"],
                        alpha=0.1, color=COLORS["p90"])
    ax.set_ylabel("ms")
    ax.set_title("SSE Time To First Byte (TTFB)")
    ax.legend(loc="upper left")
    draw_vlines(ax, meta)
    fmt_time_axis(ax)

    # D: SSE 流总时长
    ax = axes[3]
    if not sse_df.empty:
        ax.plot(sse_df["t_bin"], sse_df["avg"], color=COLORS["avg"],
                linewidth=1.2, label="avg")
        ax.plot(sse_df["t_bin"], sse_df["p90"], color=COLORS["p90"],
                linewidth=1.2, label="P90")
        ax.fill_between(sse_df["t_bin"], sse_df["avg"], sse_df["p90"],
                        alpha=0.1, color=COLORS["p90"])
    ax.set_ylabel("ms")
    ax.set_title("SSE Stream Total Duration")
    ax.set_xlabel("Elapsed Time")
    ax.legend(loc="upper left")
    draw_vlines(ax, meta)
    fmt_time_axis(ax)

    fig.tight_layout(rect=[0, 0, 1, 0.96])
    path = os.path.join(output_dir, "overview.png")
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)
    return path


# ─── 图 2: 错误率趋势 ─────────────────────────────────────────
def plot_errors(msg_err_df, stream_err_df, vus_df, meta, output_dir):
    fig, ax1 = plt.subplots(figsize=(14, 4))
    fig.suptitle("Error Rate Trend", fontsize=14, fontweight="bold")

    if not msg_err_df.empty:
        ax1.plot(msg_err_df["t_bin"], msg_err_df["error_rate"],
                 color=COLORS["err_msg"], linewidth=1.5, label="Message Errors %")
    if not stream_err_df.empty:
        ax1.plot(stream_err_df["t_bin"], stream_err_df["error_rate"],
                 color=COLORS["err_stream"], linewidth=1.5, label="Stream Errors %",
                 linestyle="--")

    ax1.set_ylabel("Error Rate (%)")
    ax1.set_xlabel("Elapsed Time")
    ax1.legend(loc="upper left")

    # 右轴画 VUs
    ax2 = ax1.twinx()
    if not vus_df.empty:
        ax2.step(vus_df["t_bin"], vus_df["vus"], where="mid",
                 color=COLORS["vus"], alpha=0.3, linewidth=1, linestyle="-.")
        ax2.set_ylabel("VUs", color=COLORS["vus"])

    draw_vlines(ax1, meta)
    fmt_time_axis(ax1)

    fig.tight_layout()
    path = os.path.join(output_dir, "errors.png")
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)
    return path


# ─── 图 3: 吞吐量趋势 ─────────────────────────────────────────
def plot_throughput(tp_df, vus_df, meta, output_dir):
    fig, ax1 = plt.subplots(figsize=(14, 4))
    fig.suptitle("Throughput vs Concurrency", fontsize=14, fontweight="bold")

    if not tp_df.empty:
        ax1.bar(tp_df["t_bin"], tp_df["chats_per_min"],
                width=tp_df["t_bin"].diff().median() * 0.8 if len(tp_df) > 1 else 5,
                color=COLORS["throughput"], alpha=0.6, label="Completed chats/min")

    ax1.set_ylabel("Chats / min")
    ax1.set_xlabel("Elapsed Time")
    ax1.legend(loc="upper left")

    ax2 = ax1.twinx()
    if not vus_df.empty:
        ax2.step(vus_df["t_bin"], vus_df["vus"], where="mid",
                 color=COLORS["vus"], linewidth=1.5, label="Active VUs")
        ax2.set_ylabel("VUs", color=COLORS["vus"])
        ax2.legend(loc="upper right")

    draw_vlines(ax1, meta)
    fmt_time_axis(ax1)

    fig.tight_layout()
    path = os.path.join(output_dir, "throughput.png")
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)
    return path


# ─── 图 4: 各阶段对比柱状图 ───────────────────────────────────
def plot_stage_comparison(stage_stats, output_dir):
    if not stage_stats:
        return None

    labels    = [s["label"] for s in stage_stats]
    ttfb_avg  = [s.get("ttfb_avg", 0) for s in stage_stats]
    sse_avg   = [s.get("sse_avg", 0) for s in stage_stats]
    err_rates = [s.get("error_rate", 0) for s in stage_stats]

    x = np.arange(len(labels))
    width = 0.25

    fig, ax1 = plt.subplots(figsize=(12, 5))
    fig.suptitle("Per-Stage Comparison", fontsize=14, fontweight="bold")

    bars1 = ax1.bar(x - width, [v / 1000 for v in ttfb_avg], width,
                    label="Avg TTFB (s)", color=COLORS["bar1"], alpha=0.8)
    bars2 = ax1.bar(x,         [v / 1000 for v in sse_avg],  width,
                    label="Avg Stream Duration (s)", color=COLORS["bar2"], alpha=0.8)

    ax1.set_ylabel("Seconds")
    ax1.set_xticks(x)
    ax1.set_xticklabels(labels)

    # 在柱子上标数值
    for bar_group in [bars1, bars2]:
        for bar in bar_group:
            h = bar.get_height()
            if h > 0:
                ax1.annotate(f"{h:.1f}s",
                             xy=(bar.get_x() + bar.get_width() / 2, h),
                             xytext=(0, 3), textcoords="offset points",
                             ha="center", va="bottom", fontsize=7)

    # 错误率用右轴
    ax2 = ax1.twinx()
    bars3 = ax2.bar(x + width, err_rates, width,
                    label="Error Rate (%)", color=COLORS["bar3"], alpha=0.5)
    ax2.set_ylabel("Error Rate (%)", color=COLORS["bar3"])

    for bar in bars3:
        h = bar.get_height()
        if h > 0:
            ax2.annotate(f"{h:.1f}%",
                         xy=(bar.get_x() + bar.get_width() / 2, h),
                         xytext=(0, 3), textcoords="offset points",
                         ha="center", va="bottom", fontsize=7, color=COLORS["bar3"])

    # 合并图例
    lines1, labels1 = ax1.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax1.legend(lines1 + lines2, labels1 + labels2, loc="upper left")

    fig.tight_layout()
    path = os.path.join(output_dir, "stages.png")
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)
    return path


# ─── 各阶段统计 + 劣化检测 ────────────────────────────────────
def compute_stage_stats(df, meta):
    """为每个台阶计算聚合指标"""
    stages = meta["stages"]
    total_dur = meta.get("total_duration_s", df["t"].max())
    results = []

    for idx, stage in enumerate(stages):
        t_start = stage["time_offset_s"]
        # 窗口延伸到下一个台阶开始，覆盖延迟完成的请求
        if idx + 1 < len(stages):
            t_end = stages[idx + 1]["time_offset_s"]
        else:
            t_end = total_dur
        mask = (df["t"] >= t_start) & (df["t"] < t_end)
        chunk = df[mask]

        # TTFB
        ttfb = chunk[chunk["metric_name"] == "stream_ttfb"]["metric_value"].astype(float)
        # SSE 总时长
        sse = chunk[chunk["metric_name"] == "stream_total_duration"]["metric_value"].astype(float)
        # POST 响应
        msg = chunk[chunk["metric_name"] == "message_send_duration"]["metric_value"].astype(float)
        # 错误率
        msg_err = chunk[chunk["metric_name"] == "message_errors"]["metric_value"].astype(float)
        stream_err = chunk[chunk["metric_name"] == "stream_errors"]["metric_value"].astype(float)
        # 完成数
        completed = chunk[chunk["metric_name"] == "completed_chats"]["metric_value"].astype(float)

        stat = {
            "label":      stage["label"],
            "target_vus": stage["target_vus"],
            "ttfb_avg":   ttfb.mean() if len(ttfb) > 0 else 0,
            "ttfb_p90":   np.percentile(ttfb, 90) if len(ttfb) > 0 else 0,
            "ttfb_p95":   np.percentile(ttfb, 95) if len(ttfb) > 0 else 0,
            "sse_avg":    sse.mean() if len(sse) > 0 else 0,
            "sse_p90":    np.percentile(sse, 90) if len(sse) > 0 else 0,
            "msg_avg":    msg.mean() if len(msg) > 0 else 0,
            "msg_p90":    np.percentile(msg, 90) if len(msg) > 0 else 0,
            "error_rate": msg_err.mean() * 100 if len(msg_err) > 0 else 0,
            "stream_error_rate": stream_err.mean() * 100 if len(stream_err) > 0 else 0,
            "completed":  completed.sum() if len(completed) > 0 else 0,
            "samples":    len(ttfb),
        }
        results.append(stat)

    return results


def detect_degradation(stage_stats):
    """检测劣化拐点"""
    findings = []
    for i in range(1, len(stage_stats)):
        prev = stage_stats[i - 1]
        curr = stage_stats[i]

        # TTFB 增长 > 50%
        if prev["ttfb_avg"] > 0 and curr["ttfb_avg"] > 0:
            growth = (curr["ttfb_avg"] - prev["ttfb_avg"]) / prev["ttfb_avg"]
            if growth > 0.5:
                findings.append(
                    f"**{curr['label']}**: TTFB avg 从 {fmt_ms_val(prev['ttfb_avg'])} "
                    f"升至 {fmt_ms_val(curr['ttfb_avg'])} (增长 {growth*100:.0f}%)"
                )

        # 错误率从 <5% 跳到 >10%
        if prev["error_rate"] < 5 and curr["error_rate"] > 10:
            findings.append(
                f"**{curr['label']}**: 错误率从 {prev['error_rate']:.1f}% "
                f"跳升至 {curr['error_rate']:.1f}%"
            )

        # SSE 总时长增长 > 80%
        if prev["sse_avg"] > 0 and curr["sse_avg"] > 0:
            growth = (curr["sse_avg"] - prev["sse_avg"]) / prev["sse_avg"]
            if growth > 0.8:
                findings.append(
                    f"**{curr['label']}**: 流总时长 avg 从 {fmt_ms_val(prev['sse_avg'])} "
                    f"升至 {fmt_ms_val(curr['sse_avg'])} (增长 {growth*100:.0f}%)"
                )

    return findings


# ─── Markdown 报告 ─────────────────────────────────────────────
def generate_report(meta, stage_stats, degradations, output_dir):
    lines = []
    lines.append("# 阶梯加压测试报告")
    lines.append("")
    lines.append(f"**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("")

    # 测试配置
    lines.append("## 测试配置")
    lines.append("")
    lines.append("| 参数 | 值 |")
    lines.append("|------|-----|")
    lines.append(f"| 目标地址 | {meta['url']} |")
    lines.append(f"| 起始并发 | {meta['start']} |")
    lines.append(f"| 结束并发 | {meta['end']} |")
    lines.append(f"| 台阶数 | {len(meta['stages'])} |")
    lines.append(f"| 每台阶持续 | {meta['step_seconds']}s |")
    lines.append(f"| 加压模式 | {meta['mode']} |")
    lines.append(f"| VU 序列 | {' -> '.join(str(s['target_vus']) for s in meta['stages'])} |")
    lines.append("")

    # 各阶段对比表
    lines.append("## 各阶段性能对比")
    lines.append("")
    lines.append("| 阶段 | VUs | TTFB avg | TTFB P90 | 流时长 avg | 流时长 P90 | POST avg | 错误率 | 完成数 |")
    lines.append("|------|-----|----------|----------|------------|------------|----------|--------|--------|")

    for s in stage_stats:
        lines.append(
            f"| {s['label']} "
            f"| {s['target_vus']} "
            f"| {fmt_ms_val(s['ttfb_avg'])} "
            f"| {fmt_ms_val(s['ttfb_p90'])} "
            f"| {fmt_ms_val(s['sse_avg'])} "
            f"| {fmt_ms_val(s['sse_p90'])} "
            f"| {fmt_ms_val(s['msg_avg'])} "
            f"| {s['error_rate']:.1f}% "
            f"| {int(s['completed'])} |"
        )
    lines.append("")

    # 劣化分析
    lines.append("## 劣化分析")
    lines.append("")
    if degradations:
        lines.append("检测到以下性能劣化拐点：")
        lines.append("")
        for d in degradations:
            lines.append(f"- {d}")
        lines.append("")
    else:
        lines.append("未检测到明显劣化拐点。")
        lines.append("")

    # 图表
    lines.append("## 图表")
    lines.append("")
    lines.append("### 性能概览")
    lines.append("![Performance Overview](overview.png)")
    lines.append("")
    lines.append("### 错误率趋势")
    lines.append("![Error Rate](errors.png)")
    lines.append("")
    lines.append("### 吞吐量趋势")
    lines.append("![Throughput](throughput.png)")
    lines.append("")
    lines.append("### 各阶段对比")
    lines.append("![Stage Comparison](stages.png)")
    lines.append("")

    lines.append("---")
    lines.append("*由 bench/plot_report.py 自动生成*")

    report_path = os.path.join(output_dir, "report.md")
    with open(report_path, "w") as f:
        f.write("\n".join(lines))

    return report_path


# ─── 主流程 ───────────────────────────────────────────────────
def main():
    args = parse_args()

    print(f"  加载 CSV: {args.csv}")
    df, _ = load_csv(args.csv)
    print(f"  数据点: {len(df)}")

    meta = load_meta(args.meta)
    print(f"  台阶数: {len(meta['stages'])}")

    os.makedirs(args.output, exist_ok=True)
    window = args.window

    # 聚合指标
    print("  聚合指标...")
    vus_df       = get_vus_series(df, window)
    msg_df       = aggregate_metric(df, "message_send_duration", window)
    ttfb_df      = aggregate_metric(df, "stream_ttfb", window)
    sse_df       = aggregate_metric(df, "stream_total_duration", window)
    msg_err_df   = compute_error_rate(df, "message_errors", window)
    stream_err_df= compute_error_rate(df, "stream_errors", window)
    tp_df        = compute_throughput(df, window)

    # 各阶段统计
    print("  计算各阶段统计...")
    stage_stats  = compute_stage_stats(df, meta)

    # 劣化检测
    degradations = detect_degradation(stage_stats)

    # 画图
    print("  生成图表...")
    plot_overview(vus_df, msg_df, ttfb_df, sse_df, meta, args.output)
    print("    -> overview.png")

    plot_errors(msg_err_df, stream_err_df, vus_df, meta, args.output)
    print("    -> errors.png")

    plot_throughput(tp_df, vus_df, meta, args.output)
    print("    -> throughput.png")

    if stage_stats:
        plot_stage_comparison(stage_stats, args.output)
        print("    -> stages.png")

    # 报告
    print("  生成报告...")
    report_path = generate_report(meta, stage_stats, degradations, args.output)
    print(f"    -> {report_path}")

    if degradations:
        print(f"\n  ⚠ 检测到 {len(degradations)} 个劣化拐点")
    else:
        print("\n  ✓ 未检测到明显劣化")


if __name__ == "__main__":
    main()
