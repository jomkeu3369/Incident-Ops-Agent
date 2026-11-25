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

    sns.boxplot(
        data=df,
        y="judge",
        x="latency_ms",
        color="#DBEAFE",
        width=0.45,
        fliersize=0,
        linewidth=1.3,
        ax=ax,
    )
    sns.stripplot(
        data=df,
        y="judge",
        x="latency_ms",
        hue="correct",
        palette={"Correct": "#2563EB", "Missed": "#EA580C"},
        alpha=0.82,
        size=6,
        jitter=0.14,
        ax=ax,
    )

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


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate matplotlib/seaborn benchmark charts from raw JSON reports.")
    parser.add_argument("--judge-json", default=None, help="Path to EvidenceJudge evaluation JSON report")
    parser.add_argument("--dr-dci-json", default=None, help="Path to DR-DCI baseline JSON report")
    parser.add_argument("--output-dir", required=True, help="Directory to save generated PNG charts")
    parser.add_argument("--prefix", default="phase5", help="Output filename prefix (default: phase5)")
    args = parser.parse_args(argv)

    if not args.judge_json and not args.dr_dci_json:
        raise ValueError("Provide at least one of --judge-json or --dr-dci-json.")

    generated: list[Path] = []
    out_dir = Path(args.output_dir)
    if args.judge_json:
        generated.extend(generate_judge_charts(args.judge_json, out_dir, prefix=args.prefix))
    if args.dr_dci_json:
        payload = _load_json(args.dr_dci_json)
        generated.append(plot_dr_dci_budget_curve(payload, out_dir / f"{args.prefix}-dr-dci-budget-curve.png"))

    for path in generated:
        print(f"Generated chart: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
