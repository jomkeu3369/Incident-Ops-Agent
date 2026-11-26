from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Sequence

import matplotlib

matplotlib.use("Agg")

from matplotlib import font_manager
import matplotlib.pyplot as plt
import pandas as pd
import seaborn as sns

_PREFERRED_KOREAN_FONTS = (
    "Malgun Gothic",
    "AppleGothic",
    "Apple SD Gothic Neo",
    "NanumGothic",
    "Noto Sans CJK KR",
    "Noto Sans KR",
)

_EXPECTED_ORDER = ["SUPPORTED", "NEED_MORE_EVIDENCE", "CONTRADICTED"]
_PREDICTED_ORDER = ["SUPPORTED", "NEED_MORE_EVIDENCE", "CONTRADICTED", "UNDECIDABLE"]
_CATEGORY_LABELS = {
    "db_connection_pool": "DB Connection Pool",
    "memory_oom": "Memory / OOM",
    "external_dependency": "External Dependency",
    "schema_migration": "Schema Migration",
    "telemetry_gap": "Telemetry Gap",
    "adversarial_edge": "Adversarial / Injection",
}


def configure_plot_style() -> str:
    """헤드리스 환경에서 한글 깨짐 없이 일관된 matplotlib/seaborn 스타일을 설정한다."""
    sns.set_theme(style="whitegrid", context="notebook")
    available = {f.name for f in font_manager.fontManager.ttflist}
    chosen = "DejaVu Sans"
    for candidate in _PREFERRED_KOREAN_FONTS:
        if candidate in available:
            chosen = candidate
            break
    plt.rcParams["font.family"] = [chosen, "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False
    plt.rcParams["figure.facecolor"] = "white"
    plt.rcParams["axes.facecolor"] = "#FAFAFC"
    plt.rcParams["axes.edgecolor"] = "#CBD5E1"
    plt.rcParams["grid.color"] = "#E2E8F0"
    plt.rcParams["grid.linestyle"] = "--"
    plt.rcParams["grid.alpha"] = 0.7
    return chosen


def _load_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def plot_confusion_matrices(reports: Sequence[dict[str, Any]], output_path: Path) -> Path:
    """판정기별 혼동 행렬 히트맵을 생성한다."""
    configure_plot_style()
    n = len(reports)
    fig, axes = plt.subplots(1, n, figsize=(6.8 * n, 5.0), squeeze=False)

    for idx, report in enumerate(reports):
        ax = axes[0][idx]
        cm = report["confusion_matrix"]
        matrix_df = pd.DataFrame(
            [[int(cm.get(exp, {}).get(pred, 0)) for pred in _PREDICTED_ORDER] for exp in _EXPECTED_ORDER],
            index=_EXPECTED_ORDER,
            columns=["SUPPORTED", "NEED_MORE\nEVIDENCE", "CONTRADICTED", "UNDECIDABLE"],
        )
        sns.heatmap(
            matrix_df,
            annot=True,
            fmt="d",
            cmap="Blues",
            cbar=idx == n - 1,
            linewidths=0.8,
            linecolor="#CBD5E1",
            annot_kws={"size": 12, "weight": "bold"},
            ax=ax,
        )
        acc_pct = report["accuracy"] * 100.0
        fsr_pct = report["false_supported_rate"] * 100.0
        f1 = report["macro_f1"]
        ax.set_title(
            f"{report['judge_name']} ({report['model']})\n"
            f"Acc {acc_pct:.1f}% | Macro-F1 {f1:.3f} | FSR {fsr_pct:.1f}% "
            f"({report['false_supported_count']}/{report['non_supported_cases']})",
            fontsize=11,
            fontweight="bold",
            pad=12,
        )
        ax.set_xlabel("Predicted Verdict", fontsize=10, labelpad=8)
        ax.set_ylabel("Expected Verdict" if idx == 0 else "", fontsize=10, labelpad=8)
        ax.tick_params(axis="x", labelsize=9)
        ax.tick_params(axis="y", labelsize=9, rotation=0)

    fig.suptitle(
        f"EvidenceJudge Confusion Matrix ({reports[0]['dataset_version']})",
        fontsize=13,
        fontweight="bold",
        y=1.01,
    )
    fig.tight_layout()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, dpi=300, bbox_inches="tight")
    plt.close(fig)
    return output_path


def plot_category_metrics(reports: Sequence[dict[str, Any]], output_path: Path) -> Path:
    """장애 카테고리별 정확도 및 위험 오승인율(FSR) 비교 막대 차트를 생성한다."""
    configure_plot_style()
    rows: list[dict[str, Any]] = []
    for report in reports:
        for cat, metrics in report["per_category"].items():
            rows.append({
                "judge": report["judge_name"],
                "category": _CATEGORY_LABELS.get(cat, cat),
                "accuracy_pct": metrics["accuracy"] * 100.0,
                "fsr_pct": metrics["false_supported_rate"] * 100.0,
                "correct": metrics["correct"],
                "total": metrics["total"],
                "fs_count": metrics["false_supported_count"],
                "non_sup": metrics["non_supported_total"],
            })

    df = pd.DataFrame(rows)
    fig, (ax_acc, ax_fsr) = plt.subplots(1, 2, figsize=(12.5, 4.8), sharey=True)

    palette = sns.color_palette("Set2", n_colors=max(1, df["judge"].nunique()))
    sns.barplot(data=df, y="category", x="accuracy_pct", hue="judge", palette=palette, ax=ax_acc)
    ax_acc.set_title("Category Accuracy (%, ↑ higher is better)", fontsize=11, fontweight="bold")
    ax_acc.set_xlabel("Accuracy (%)")
    ax_acc.set_ylabel("Incident Category")
    ax_acc.set_xlim(0, 118)
    for container in ax_acc.containers:
        ax_acc.bar_label(container, fmt="%.1f%%", padding=4, fontsize=9)

    sns.barplot(data=df, y="category", x="fsr_pct", hue="judge", palette=palette, ax=ax_fsr)
    ax_fsr.set_title("False-Supported Rate — FSR (%, ↓ lower is safer)", fontsize=11, fontweight="bold")
    ax_fsr.set_xlabel("FSR (%)")
    ax_fsr.set_ylabel("")
    ax_fsr.set_xlim(0, max(15.0, float(df["fsr_pct"].max()) + 15.0))
    ax_fsr.axvline(5.0, color="#DC2626", linestyle="--", linewidth=1.2, label="Safety Gate (5.0%)")
    for container in ax_fsr.containers:
        ax_fsr.bar_label(container, fmt="%.1f%%", padding=4, fontsize=9)

    if df["judge"].nunique() == 1:
        if ax_acc.get_legend():
            ax_acc.get_legend().remove()
        ax_fsr.legend(loc="lower right", frameon=True, fontsize=9)
    else:
        ax_acc.legend(title="Judge", loc="lower right", fontsize=9)
        ax_fsr.legend(title="Judge / Gate", loc="lower right", fontsize=9)

    fig.suptitle(
        f"EvidenceJudge Category Accuracy & Safety Gate ({reports[0]['dataset_version']})",
        fontsize=13,
        fontweight="bold",
        y=1.02,
    )
    fig.tight_layout()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, dpi=300, bbox_inches="tight")
    plt.close(fig)
    return output_path


def plot_latency_distribution(reports: Sequence[dict[str, Any]], output_path: Path) -> Path:
    """판정기별 케이스 레이턴시 분포와 p50/p95/p99 백분위수를 시각화한다."""
    configure_plot_style()
    rows: list[dict[str, Any]] = []
    for report in reports:
        for case in report["cases"]:
            rows.append({
                "judge": report["judge_name"],
                "latency_ms": float(case["latency_ms"]),
                "correct": "Correct" if case["correct"] else "Missed",
            })

    df = pd.DataFrame(rows)
    fig, ax = plt.subplots(figsize=(9.5, 4.6))

    judge_order = list(dict.fromkeys(df["judge"].tolist()))
    grouped_latencies = [df.loc[df["judge"] == j, "latency_ms"].to_numpy() for j in judge_order]
    positions = list(range(len(judge_order)))
    ax.boxplot(
        grouped_latencies,
        positions=positions,
        orientation="horizontal",
        widths=0.45,
        patch_artist=True,
        showfliers=False,
        boxprops={"facecolor": "#DBEAFE", "edgecolor": "#475569", "linewidth": 1.3},
        whiskerprops={"color": "#475569", "linewidth": 1.3},
        capprops={"color": "#475569", "linewidth": 1.3},
        medianprops={"color": "#1E293B", "linewidth": 1.5},
    )
    sns.stripplot(
        data=df,
        y="judge",
        x="latency_ms",
        order=judge_order,
        hue="correct",
        palette={"Correct": "#2563EB", "Missed": "#EA580C"},
        alpha=0.82,
        size=6,
        jitter=0.14,
        ax=ax,
    )
    ax.set_yticks(positions)
    ax.set_yticklabels(judge_order)

    summary_lines: list[str] = []
    for report in reports:
        p50 = float(report.get("latency_p50_ms", 0.0))
        p95 = float(report.get("latency_p95_ms", 0.0))
        p99 = float(report.get("latency_p99_ms", p95))
        summary_lines.append(f"{report['judge_name']}: p50={p50:.2f}ms | p95={p95:.2f}ms | p99={p99:.2f}ms")
        if len(reports) == 1:
            ax.axvline(p50, color="#1D4ED8", linestyle="-", linewidth=1.2, alpha=0.8, label=f"p50 ({p50:.2f} ms)")
            ax.axvline(p95, color="#D97706", linestyle="--", linewidth=1.2, alpha=0.85, label=f"p95 ({p95:.2f} ms)")
            ax.axvline(p99, color="#DC2626", linestyle=":", linewidth=1.4, alpha=0.9, label=f"p99 ({p99:.2f} ms)")

    ax.set_title(
        f"EvidenceJudge Latency Distribution — p50 / p95 / p99 ({reports[0]['dataset_version']})",
        fontsize=12,
        fontweight="bold",
        pad=12,
    )
    ax.set_xlabel("Latency (ms)")
    ax.set_ylabel("Judge")
    ax.legend(loc="upper right", frameon=True, fontsize=9)
    ax.text(
        0.01,
        -0.22,
        "   |   ".join(summary_lines),
        transform=ax.transAxes,
        fontsize=9,
        color="#334155",
    )

    fig.tight_layout()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, dpi=300, bbox_inches="tight")
    plt.close(fig)
    return output_path


def plot_judge_comparison(reports: Sequence[dict[str, Any]], output_path: Path) -> Path:
    """복수 판정기 간 종합 지표(Accuracy, Macro-F1, Safe-Block, Injection Resist)를 비교한다."""
    configure_plot_style()
    rows: list[dict[str, Any]] = []
    for report in reports:
        inj = report.get("injection_resistance_rate")
        rows.extend([
            {"judge": report["judge_name"], "metric": "Accuracy", "score_pct": report["accuracy"] * 100.0},
            {"judge": report["judge_name"], "metric": "Macro-F1", "score_pct": report["macro_f1"] * 100.0},
            {"judge": report["judge_name"], "metric": "Safe-Block (1-FSR)", "score_pct": report["safe_block_rate"] * 100.0},
            {"judge": report["judge_name"], "metric": "Injection Resist", "score_pct": (inj * 100.0) if inj is not None else 0.0},
        ])

    df = pd.DataFrame(rows)
    fig, ax = plt.subplots(figsize=(10.0, 4.8))
    sns.barplot(data=df, x="metric", y="score_pct", hue="judge", palette="Set2", ax=ax)
    ax.set_title(f"EvidenceJudge Multi-Model Comparison ({reports[0]['dataset_version']})", fontsize=12, fontweight="bold")
    ax.set_xlabel("")
    ax.set_ylabel("Score (%)")
    ax.set_ylim(0, 114)
    for container in ax.containers:
        ax.bar_label(container, fmt="%.1f%%", padding=3, fontsize=8.5)
    ax.legend(title="Judge", loc="lower right", frameon=True)

    fig.tight_layout()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, dpi=300, bbox_inches="tight")
    plt.close(fig)
    return output_path


def plot_dr_dci_budget_curve(payload: dict[str, Any], output_path: Path) -> Path:
    """DR_DCI_MAX_PULL_STEPS 변화에 따른 Evidence Recall vs. 컨텍스트 문자량/절감률 곡선을 생성한다."""
    configure_plot_style()
    summary = pd.DataFrame(payload["summary_by_budget"])
    summary["recall_pct"] = summary["mean_evidence_recall"] * 100.0
    summary["reduction_pct"] = summary["mean_char_reduction_ratio"] * 100.0

    fig, ax_left = plt.subplots(figsize=(9.5, 4.8))
    ax_right = ax_left.twinx()

    sns.lineplot(
        data=summary,
        x="max_pull_steps",
        y="recall_pct",
        marker="o",
        linewidth=2.4,
        markersize=8,
        color="#2563EB",
        label="Evidence Recall (%)",
        ax=ax_left,
        legend=False,
    )
    sns.lineplot(
        data=summary,
        x="max_pull_steps",
        y="mean_context_chars",
        marker="s",
        linewidth=2.2,
        linestyle="--",
        markersize=7,
        color="#EA580C",
        label="Mean Context Chars",
        ax=ax_right,
        legend=False,
    )

    full_chars = float(summary["mean_total_corpus_chars"].iloc[0])
    ax_right.axhline(
        full_chars,
        color="#64748B",
        linestyle=":",
        linewidth=1.3,
        label=f"Full Corpus Mean ({full_chars:.0f} chars)",
    )

    for _, row in summary.iterrows():
        x = row["max_pull_steps"]
        y_rec = row["recall_pct"]
        y_chr = row["mean_context_chars"]
        red = row["reduction_pct"]
        ax_left.annotate(
            f"{y_rec:.0f}%",
            (x, y_rec),
            textcoords="offset points",
            xytext=(0, 9),
            ha="center",
            fontsize=9,
            fontweight="bold",
            color="#1D4ED8",
        )
        ax_right.annotate(
            f"{y_chr:.0f}ch (-{red:.0f}%)",
            (x, y_chr),
            textcoords="offset points",
            xytext=(0, -16),
            ha="center",
            fontsize=8.5,
            color="#C2410C",
        )

    ax_left.set_xticks(summary["max_pull_steps"].tolist())
    ax_left.set_xlabel("DR_DCI_MAX_PULL_STEPS (Pull Budget)", fontsize=10)
    ax_left.set_ylabel("Mean Evidence Recall (%)", fontsize=10, color="#1D4ED8")
    ax_left.set_ylim(0, 115)
    ax_right.set_ylabel("Context Size (chars)", fontsize=10, color="#C2410C")
    ax_right.set_ylim(0, max(full_chars * 1.18, float(summary["mean_context_chars"].max()) * 1.25))
    ax_right.grid(False)

    h1, l1 = ax_left.get_legend_handles_labels()
    h2, l2 = ax_right.get_legend_handles_labels()
    ax_left.legend(h1 + h2, l1 + l2, loc="lower right", frameon=True, fontsize=9)

    ax_left.set_title(
        f"Keyword DR-DCI Baseline: Pull Budget vs. Evidence Recall & Context Size ({payload['scenario_count']} scenarios)",
        fontsize=11.5,
        fontweight="bold",
        pad=12,
    )
    fig.tight_layout()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, dpi=300, bbox_inches="tight")
    plt.close(fig)
    return output_path


def generate_judge_charts(
    report_json_path: str | Path,
    output_dir: str | Path,
    *,
    prefix: str = "phase5",
) -> list[Path]:
    """EvidenceJudge JSON 리포트에서 표준 시각화 차트 세트를 일괄 생성한다."""
    reports = _load_json(report_json_path)
    if not isinstance(reports, list) or not reports:
        raise ValueError("Judge report JSON must be a non-empty list of report objects.")
    out = Path(output_dir)
    paths = [
        plot_confusion_matrices(reports, out / f"{prefix}-confusion-matrix.png"),
        plot_category_metrics(reports, out / f"{prefix}-category-metrics.png"),
        plot_latency_distribution(reports, out / f"{prefix}-latency-distribution.png"),
    ]
    if len(reports) > 1:
        paths.append(plot_judge_comparison(reports, out / f"{prefix}-judge-comparison.png"))
    return paths


_STRATEGY_LABELS = {
    "full_context": "Full Context",
    "vector_rag": "Vector RAG",
    "keyword_dr_dci": "Keyword DR-DCI",
    "agentic_dr_dci": "Agentic DR-DCI",
}
_STRATEGY_COLORS = {
    "Full Context": "#64748B",
    "Vector RAG": "#F59E0B",
    "Keyword DR-DCI": "#0EA5E9",
    "Agentic DR-DCI": "#2563EB",
}


def plot_four_way_pareto(payload: dict[str, Any], output_path: Path) -> Path:
    """4종 검색 전략의 입력 토큰 대비 Macro-F1 / Evidence Recall 파레토 산점도를 생성한다."""
    configure_plot_style()
    rows = []
    for s in payload["summaries"]:
        label = _STRATEGY_LABELS.get(s["strategy"], s["strategy"])
        rows.append({
            "strategy": label,
            "mean_input_tokens": float(s["mean_input_tokens"]),
            "macro_f1_pct": float(s["macro_f1"]) * 100.0,
            "recall_pct": float(s["mean_evidence_recall"]) * 100.0,
            "multi_hop_pct": float(s["multi_hop_evidence_recall"]) * 100.0,
            "fsr_pct": float(s["false_supported_rate"]) * 100.0,
            "p99_ms": float(s["latency_p99_ms"]),
            "reduction_pct": float(s["token_reduction_vs_full"]) * 100.0,
        })
    df = pd.DataFrame(rows)

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(13.0, 5.0))
    for ax, y_col, y_title in (
        (ax1, "macro_f1_pct", "Macro-F1 (%, ↑)"),
        (ax2, "multi_hop_pct", "Multi-Hop Evidence Recall (%, ↑)"),
    ):
        sns.scatterplot(
            data=df,
            x="mean_input_tokens",
            y=y_col,
            hue="strategy",
            palette=_STRATEGY_COLORS,
            s=260,
            edgecolor="#0F172A",
            linewidth=1.1,
            ax=ax,
            legend=False,
        )
        label_offsets = {
            "Full Context": ((0, -28), "center"),
            "Agentic DR-DCI": ((0, 14), "center"),
            "Vector RAG": ((16, 4), "left"),
            "Keyword DR-DCI": ((16, -24), "left"),
        }
        for _, r in df.iterrows():
            red_lbl = "base" if r["strategy"] == "Full Context" else f"-{r['reduction_pct']:.0f}% tok"
            (dx, dy), ha = label_offsets.get(r["strategy"], ((10, 10), "left"))
            ax.annotate(
                f"{r['strategy']}\n{r[y_col]:.1f}% | {red_lbl} | FSR {r['fsr_pct']:.1f}%",
                (r["mean_input_tokens"], r[y_col]),
                textcoords="offset points",
                xytext=(dx, dy),
                ha=ha,
                fontsize=8.3,
                fontweight="bold",
                color="#1E293B",
                bbox={"boxstyle": "round,pad=0.22", "facecolor": "white", "edgecolor": "none", "alpha": 0.78},
            )
        ax.set_xlabel("Mean Input Tokens per Incident (↓ lower is cheaper)", fontsize=9.5)
        ax.set_ylabel(y_title, fontsize=9.5)
        ax.set_ylim(max(0, float(df[y_col].min()) - 18), 112)
        x_max = float(df["mean_input_tokens"].max())
        ax.set_xlim(-120, x_max * 1.20)
        ax.set_title(f"Token Efficiency vs. {y_title.split(' (')[0]}", fontsize=11, fontweight="bold")

    fig.suptitle(
        f"4-Way Retrieval Strategy Pareto Comparison ({payload['dataset_version']}, n={payload['total_cases']})",
        fontsize=13,
        fontweight="bold",
        y=1.02,
    )
    fig.tight_layout()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, dpi=300, bbox_inches="tight")
    plt.close(fig)
    return output_path


def plot_four_way_groundedness_and_safety(payload: dict[str, Any], output_path: Path) -> Path:
    """4종 검색 전략의 회수율·Groundedness·Macro-F1·안전 지표(Ungrounded Rate, FSR)를 비교한다."""
    configure_plot_style()
    qual_rows = []
    risk_rows = []
    for s in payload["summaries"]:
        label = _STRATEGY_LABELS.get(s["strategy"], s["strategy"])
        qual_rows.extend([
            {"strategy": label, "metric": "Evidence\nRecall", "pct": float(s["mean_evidence_recall"]) * 100.0},
            {"strategy": label, "metric": "Multi-Hop\nRecall", "pct": float(s["multi_hop_evidence_recall"]) * 100.0},
            {"strategy": label, "metric": "Groundedness", "pct": float(s["groundedness_rate"]) * 100.0},
            {"strategy": label, "metric": "Macro-F1", "pct": float(s["macro_f1"]) * 100.0},
        ])
        risk_rows.extend([
            {"strategy": label, "metric": "Ungrounded Rate (↓)", "pct": float(s["ungrounded_rate"]) * 100.0},
            {"strategy": label, "metric": "FSR (↓)", "pct": float(s["false_supported_rate"]) * 100.0},
        ])

    q_df = pd.DataFrame(qual_rows)
    r_df = pd.DataFrame(risk_rows)

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(13.5, 5.0), gridspec_kw={"width_ratios": [1.45, 1.0]})
    sns.barplot(data=q_df, x="metric", y="pct", hue="strategy", palette=_STRATEGY_COLORS, ax=ax1)
    ax1.set_title("Quality & Groundedness Metrics (%, ↑ higher is better)", fontsize=11, fontweight="bold")
    ax1.set_xlabel("")
    ax1.set_ylabel("Score (%)")
    ax1.set_ylim(0, 118)
    for container in ax1.containers:
        ax1.bar_label(container, fmt="%.1f", padding=2, fontsize=7.8)
    ax1.legend(title="Strategy", loc="lower right", frameon=True, fontsize=8.5)

    sns.barplot(data=r_df, x="metric", y="pct", hue="strategy", palette=_STRATEGY_COLORS, ax=ax2)
    ax2.set_title("Safety Risk Metrics (%, ↓ lower is safer)", fontsize=11, fontweight="bold")
    ax2.set_xlabel("")
    ax2.set_ylabel("Rate (%)")
    ax2.set_ylim(0, max(16.0, float(r_df["pct"].max()) + 10.0))
    ax2.axhline(5.0, color="#DC2626", linestyle="--", linewidth=1.2, label="FSR Safety Gate (5.0%)")
    for container in ax2.containers:
        ax2.bar_label(container, fmt="%.1f%%", padding=2, fontsize=8)
    ax2.legend(title="Strategy / Gate", loc="upper right", frameon=True, fontsize=8.5)

    fig.suptitle(
        f"4-Way Retrieval Quality, Groundedness & Safety Comparison ({payload['dataset_version']})",
        fontsize=13,
        fontweight="bold",
        y=1.02,
    )
    fig.tight_layout()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, dpi=300, bbox_inches="tight")
    plt.close(fig)
    return output_path


def plot_four_way_latency_and_steps(payload: dict[str, Any], output_path: Path) -> Path:
    """4종 전략별 레이턴시 분포(p50/p95/p99)와 평균 인출 스텝·토큰 절감률을 시각화한다."""
    configure_plot_style()
    case_rows = [
        {
            "strategy": _STRATEGY_LABELS.get(c["strategy"], c["strategy"]),
            "latency_ms": float(c["latency_ms"]),
        }
        for c in payload["cases"]
    ]
    c_df = pd.DataFrame(case_rows)

    sum_rows = [
        {
            "strategy": _STRATEGY_LABELS.get(s["strategy"], s["strategy"]),
            "mean_pulls": float(s["mean_pull_calls"]),
            "mean_tokens": float(s["mean_input_tokens"]),
            "p50": float(s["latency_p50_ms"]),
            "p95": float(s["latency_p95_ms"]),
            "p99": float(s["latency_p99_ms"]),
        }
        for s in payload["summaries"]
    ]
    s_df = pd.DataFrame(sum_rows)

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(13.2, 4.9))
    strat_order = list(dict.fromkeys(c_df["strategy"].tolist()))
    grouped_strat = [c_df.loc[c_df["strategy"] == st, "latency_ms"].to_numpy() for st in strat_order]
    strat_positions = list(range(len(strat_order)))
    bp = ax1.boxplot(
        grouped_strat,
        positions=strat_positions,
        orientation="horizontal",
        widths=0.5,
        patch_artist=True,
        showfliers=True,
        boxprops={"edgecolor": "#334155", "linewidth": 1.2},
        whiskerprops={"color": "#334155", "linewidth": 1.2},
        capprops={"color": "#334155", "linewidth": 1.2},
        medianprops={"color": "#0F172A", "linewidth": 1.4},
        flierprops={"marker": "o", "markersize": 2.5, "markerfacecolor": "#475569", "markeredgecolor": "#475569"},
    )
    for patch, strat in zip(bp["boxes"], strat_order):
        patch.set_facecolor(_STRATEGY_COLORS.get(strat, "#94A3B8"))
        patch.set_alpha(0.85)
    ax1.set_yticks(strat_positions)
    ax1.set_yticklabels(strat_order)
    ax1.invert_yaxis()
    is_live = payload.get("latency_scope") == "live_llm_api_inclusive"
    ax1.set_title(
        "Live LLM API End-to-End Latency — p50/p95/p99 (incl. OpenAI RTT)"
        if is_live
        else "Offline In-Memory CPU Latency — p50/p95/p99 (Pre-Indexed RAG; excl. LLM/MCP RTT)",
        fontsize=10,
        fontweight="bold",
    )
    ax1.set_xlabel("Live API Wall-Clock Latency (ms)" if is_live else "Offline CPU Wall-Clock Latency (ms, no network/LLM RTT)")
    ax1.set_ylabel("")
    note = "\n".join(f"{r['strategy']}: p50={r['p50']:.2f} | p95={r['p95']:.2f} | p99={r['p99']:.2f} ms" for _, r in s_df.iterrows())
    build_ms = payload.get("vector_index_build_ms_total")
    if build_ms is not None and not is_live:
        note += f"\n(Vector RAG pre-index build total: {float(build_ms):.1f} ms excl.)"
    ax1.text(0.98, 0.96, note, transform=ax1.transAxes, ha="right", va="top", fontsize=7.6,
             bbox={"boxstyle": "round,pad=0.35", "facecolor": "white", "edgecolor": "#CBD5E1", "alpha": 0.92})

    sns.barplot(data=s_df, x="strategy", y="mean_tokens", hue="strategy", palette=_STRATEGY_COLORS, legend=False, ax=ax2)
    ax2.set_title("Mean Estimated Input Tokens & Pulled Chunks per Incident", fontsize=10.5, fontweight="bold")
    ax2.set_xlabel("")
    ax2.set_ylabel("Mean Estimated Input Tokens (incl. selector)")
    ax2.set_ylim(0, float(s_df["mean_tokens"].max()) * 1.25)
    for idx, (_, row) in enumerate(s_df.iterrows()):
        ax2.text(
            idx,
            row["mean_tokens"] + float(s_df["mean_tokens"].max()) * 0.03,
            f"{row['mean_tokens']:.0f} tok\n({row['mean_pulls']:.1f} chunks)",
            ha="center",
            va="bottom",
            fontsize=8.5,
            fontweight="bold",
            color="#1E293B",
        )

    fig.suptitle(
        f"4-Way {'Live LLM' if is_live else 'Offline Benchmark'} Latency & Token Budget Profile ({payload['dataset_version']})",
        fontsize=13,
        fontweight="bold",
        y=1.02,
    )
    fig.tight_layout()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, dpi=300, bbox_inches="tight")
    plt.close(fig)
    return output_path


def plot_four_way_category_recall_heatmap(payload: dict[str, Any], output_path: Path) -> Path:
    """6개 장애 카테고리 × 4종 검색 전략의 Evidence Recall 히트맵을 생성한다."""
    configure_plot_style()
    categories = sorted(payload["summaries"][0]["per_category_recall"].keys())
    data: dict[str, list[float]] = {}
    for s in payload["summaries"]:
        label = _STRATEGY_LABELS.get(s["strategy"], s["strategy"])
        data[label] = [float(s["per_category_recall"][cat]) * 100.0 for cat in categories]

    df = pd.DataFrame(data, index=[_CATEGORY_LABELS.get(c, c) for c in categories])
    fig, ax = plt.subplots(figsize=(9.6, 5.2))
    sns.heatmap(
        df,
        annot=True,
        fmt=".1f",
        cmap="YlGnBu",
        vmin=40.0,
        vmax=100.0,
        linewidths=0.8,
        linecolor="#CBD5E1",
        cbar_kws={"label": "Evidence Recall (%)"},
        annot_kws={"size": 10.5, "weight": "bold"},
        ax=ax,
    )
    ax.set_title(
        f"Category × Strategy Evidence Recall Heatmap (%, {payload['dataset_version']})",
        fontsize=12,
        fontweight="bold",
        pad=12,
    )
    ax.set_xlabel("Retrieval Strategy", fontsize=10, labelpad=8)
    ax.set_ylabel("Incident Category", fontsize=10, labelpad=8)
    ax.tick_params(axis="y", rotation=0)

    fig.tight_layout()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, dpi=300, bbox_inches="tight")
    plt.close(fig)
    return output_path


def generate_four_way_charts(
    four_way_json_path: str | Path,
    output_dir: str | Path,
    *,
    prefix: str = "phase6",
) -> list[Path]:
    """4종 DR-DCI 벤치마크 JSON 리포트에서 4종 비교 차트를 일괄 생성한다."""
    payload = _load_json(four_way_json_path)
    out = Path(output_dir)
    return [
        plot_four_way_pareto(payload, out / f"{prefix}-pareto-token-vs-quality.png"),
        plot_four_way_groundedness_and_safety(payload, out / f"{prefix}-groundedness-and-safety.png"),
        plot_four_way_latency_and_steps(payload, out / f"{prefix}-latency-and-steps.png"),
        plot_four_way_category_recall_heatmap(payload, out / f"{prefix}-category-recall-heatmap.png"),
    ]


_PARADIGM_LABELS = {
    "native_rag": "Native RAG",
    "hybrid_rag_rrf": "RAG + BM25 + RRF",
    "dci": "DCI (Full Context)",
    "dr_dci": "DR-DCI (Dynamic)",
}
_PARADIGM_COLORS = {
    "Native RAG": "#F59E0B",
    "RAG + BM25 + RRF": "#10B981",
    "DCI (Full Context)": "#64748B",
    "DR-DCI (Dynamic)": "#2563EB",
}
_PARADIGM_MARKERS = {
    "Native RAG": "o",
    "RAG + BM25 + RRF": "^",
    "DCI (Full Context)": "s",
    "DR-DCI (Dynamic)": "D",
}


def plot_scaling_dashboard(payload: dict[str, Any], output_path: Path) -> Path:
    """선형 데이터 증가(N)에 따른 4대 패러다임 2x2 종합 성능 대시보드 차트를 생성한다."""
    configure_plot_style()
    df = pd.DataFrame(payload["points"])
    df["label"] = df["paradigm"].map(_PARADIGM_LABELS)
    df["recall_pct"] = df["mean_evidence_recall"] * 100.0
    df["mh_recall_pct"] = df["multi_hop_evidence_recall"] * 100.0
    df["f1_pct"] = df["macro_f1"] * 100.0
    df["grounded_pct"] = df["groundedness_rate"] * 100.0

    fig, axes = plt.subplots(2, 2, figsize=(14.0, 9.6))
    ax_tok, ax_mh = axes[0]
    ax_f1, ax_lat = axes[1]

    order = ["Native RAG", "RAG + BM25 + RRF", "DCI (Full Context)", "DR-DCI (Dynamic)"]
    for label in order:
        sub = df[df["label"] == label].sort_values("chunks_per_incident")
        kw = dict(
            color=_PARADIGM_COLORS[label],
            marker=_PARADIGM_MARKERS[label],
            linewidth=2.3,
            markersize=7.5,
            label=label,
        )
        ax_tok.plot(sub["chunks_per_incident"], sub["mean_input_tokens"], **kw)
        ax_mh.plot(sub["chunks_per_incident"], sub["mh_recall_pct"], **kw)
        ax_f1.plot(sub["chunks_per_incident"], sub["f1_pct"], **kw)
        ax_lat.plot(sub["chunks_per_incident"], sub["latency_p95_ms"], **kw)

    limit_tok = int(payload.get("context_limit_tokens", 32000))
    ax_tok.axhline(limit_tok, color="#DC2626", linestyle="--", linewidth=1.3, label=f"Context Limit ({limit_tok:,} tok)")
    ax_tok.set_title("1. Mean Input Tokens vs. Corpus Scale (↓ lower is cheaper)", fontsize=11, fontweight="bold")
    ax_tok.set_xlabel("Chunks per Incident (N)")
    ax_tok.set_ylabel("Mean Input Tokens (incl. selector)")
    ax_tok.legend(loc="upper left", frameon=True, fontsize=8.5)

    ax_mh.set_title("2. Multi-Hop Evidence Recall vs. Corpus Scale (↑ higher is better)", fontsize=11, fontweight="bold")
    ax_mh.set_xlabel("Chunks per Incident (N)")
    ax_mh.set_ylabel("Multi-Hop Recall (%)")
    ax_mh.set_ylim(0, 112)
    ax_mh.legend(loc="lower left", frameon=True, fontsize=8.5)

    ax_f1.set_title("3. End-to-End Macro-F1 vs. Corpus Scale (↑ higher is better)", fontsize=11, fontweight="bold")
    ax_f1.set_xlabel("Chunks per Incident (N)")
    ax_f1.set_ylabel("Macro-F1 (%)")
    ax_f1.set_ylim(0, 112)
    ax_f1.legend(loc="lower left", frameon=True, fontsize=8.5)

    is_live = payload.get("latency_scope") == "live_llm_api_inclusive"
    ax_lat.set_title(
        "4. Live End-to-End p95 Latency (incl. OpenAI)"
        if is_live
        else "4. Offline CPU p95 Query Latency (Pre-Indexed; excl. LLM/MCP RTT)",
        fontsize=10.2,
        fontweight="bold",
    )
    ax_lat.set_xlabel("Chunks per Incident (N)")
    ax_lat.set_ylabel("Live End-to-End p95 Latency (ms)" if is_live else "Offline CPU p95 Latency (ms)")
    ax_lat.legend(loc="upper left", frameon=True, fontsize=8.5)

    scales = payload["scales"]
    for ax in (ax_tok, ax_mh, ax_f1, ax_lat):
        ax.set_xticks(scales)

    fig.suptitle(
        f"Linear Corpus Scaling (N = {scales[0]} → {scales[-1]} chunks/incident, {payload['incidents_per_scale']} incidents/point)\n"
        "Native RAG vs. RAG+BM25+RRF vs. DCI (Full) vs. DR-DCI (Dynamic)",
        fontsize=13.5,
        fontweight="bold",
        y=1.01,
    )
    fig.tight_layout()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, dpi=300, bbox_inches="tight")
    plt.close(fig)
    return output_path


def plot_scaling_groundedness_and_latency(payload: dict[str, Any], output_path: Path) -> Path:
    """선형 데이터 증가에 따른 Groundedness(%) 및 p50/p99 tail latency 비교 차트를 생성한다."""
    configure_plot_style()
    df = pd.DataFrame(payload["points"])
    df["label"] = df["paradigm"].map(_PARADIGM_LABELS)
    df["grounded_pct"] = df["groundedness_rate"] * 100.0
    df["recall_pct"] = df["mean_evidence_recall"] * 100.0

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(13.6, 5.0))
    order = ["Native RAG", "RAG + BM25 + RRF", "DCI (Full Context)", "DR-DCI (Dynamic)"]
    for label in order:
        sub = df[df["label"] == label].sort_values("chunks_per_incident")
        color = _PARADIGM_COLORS[label]
        marker = _PARADIGM_MARKERS[label]
        ax1.plot(sub["chunks_per_incident"], sub["grounded_pct"], color=color, marker=marker, linewidth=2.2, markersize=7, label=label)
        ax2.plot(sub["chunks_per_incident"], sub["latency_p50_ms"], color=color, marker=marker, linewidth=2.1, markersize=6.5, linestyle="-", label=f"{label} p50")
        ax2.plot(sub["chunks_per_incident"], sub["latency_p99_ms"], color=color, marker=marker, linewidth=1.6, markersize=5, linestyle="--", alpha=0.72, label=f"{label} p99")

    scales = payload["scales"]
    ax1.set_xticks(scales)
    ax1.set_ylim(0, 112)
    ax1.set_xlabel("Chunks per Incident (N)")
    ax1.set_ylabel("Groundedness (%)")
    ax1.set_title("Groundedness Stability Under Linear Scale Growth", fontsize=11, fontweight="bold")
    ax1.legend(loc="lower left", frameon=True, fontsize=8.8)

    ax2.set_xticks(scales)
    ax2.set_xlabel("Chunks per Incident (N)")
    is_live = payload.get("latency_scope") == "live_llm_api_inclusive"
    ax2.set_ylabel("Live End-to-End Latency (ms)" if is_live else "Offline CPU Latency (ms, no LLM/MCP RTT)")
    ax2.set_title(
        ("Live End-to-End Latency" if is_live else "Pre-Indexed Offline Query Latency")
        + ": p50 (solid) vs. p99 (dashed)",
        fontsize=10.5,
        fontweight="bold",
    )
    ax2.legend(loc="upper left", frameon=True, fontsize=7.8, ncol=2)

    fig.suptitle(
        f"Groundedness & Tail Latency Scaling (N = {scales[0]} → {scales[-1]} chunks/incident)",
        fontsize=13,
        fontweight="bold",
        y=1.02,
    )
    fig.tight_layout()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, dpi=300, bbox_inches="tight")
    plt.close(fig)
    return output_path


def generate_scaling_charts(
    scaling_json_path: str | Path,
    output_dir: str | Path,
    *,
    prefix: str = "scaling-4paradigm",
) -> list[Path]:
    payload = _load_json(scaling_json_path)
    out = Path(output_dir)
    return [
        plot_scaling_dashboard(payload, out / f"{prefix}-dashboard.png"),
        plot_scaling_groundedness_and_latency(payload, out / f"{prefix}-groundedness-and-latency.png"),
    ]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate matplotlib/seaborn benchmark charts from raw JSON reports.")
    parser.add_argument("--judge-json", default=None, help="Path to EvidenceJudge evaluation JSON report")
    parser.add_argument("--dr-dci-json", default=None, help="Path to DR-DCI baseline JSON report")
    parser.add_argument("--four-way-json", default=None, help="Path to Phase 6 4-way DR-DCI benchmark JSON report")
    parser.add_argument("--scaling-json", default=None, help="Path to 4-paradigm linear scaling benchmark JSON report")
    parser.add_argument("--output-dir", required=True, help="Directory to save generated PNG charts")
    parser.add_argument("--prefix", default="phase5", help="Output filename prefix (default: phase5)")
    args = parser.parse_args(argv)

    if not args.judge_json and not args.dr_dci_json and not args.four_way_json and not args.scaling_json:
        raise ValueError("Provide at least one of --judge-json, --dr-dci-json, --four-way-json, or --scaling-json.")

    generated: list[Path] = []
    out_dir = Path(args.output_dir)
    if args.judge_json:
        generated.extend(generate_judge_charts(args.judge_json, out_dir, prefix=args.prefix))
    if args.dr_dci_json:
        payload = _load_json(args.dr_dci_json)
        generated.append(plot_dr_dci_budget_curve(payload, out_dir / f"{args.prefix}-dr-dci-budget-curve.png"))
    if args.four_way_json:
        generated.extend(generate_four_way_charts(args.four_way_json, out_dir, prefix=args.prefix))
    if args.scaling_json:
        generated.extend(generate_scaling_charts(args.scaling_json, out_dir, prefix=args.prefix))

    for path in generated:
        print(f"Generated chart: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
