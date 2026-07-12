"""
report.py — グリッドサーチ結果のCSV/ヒートマップ、OOSサマリのMarkdown、トレードログ出力。
"""
from __future__ import annotations

from pathlib import Path

import matplotlib
matplotlib.use("Agg")  # 画面なしでPNG出力
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

import config


def save_grid_csv(grid_results: pd.DataFrame, path: Path | None = None) -> Path:
    """グリッドサーチ全結果をCSVで保存。"""
    path = path or (config.RESULTS_DIR / "grid_search_results.csv")
    grid_results.to_csv(path, index=False, encoding="utf-8-sig")
    return path


def save_trade_log(trade_df: pd.DataFrame, name: str = "trade_log.csv") -> Path:
    """個別トレードログをCSVで保存。"""
    path = config.RESULTS_DIR / name
    trade_df.to_csv(path, index=False, encoding="utf-8-sig")
    return path


def save_expectancy_heatmaps(
    grid_results: pd.DataFrame, metric: str = "expectancy_pct"
) -> list[Path]:
    """
    X(サプライズ閾値)× N(保有日数)の期待値ヒートマップを Z ごとにPNG出力。
    """
    paths = []
    z_values = list(grid_results["Z"].unique())
    for z in z_values:
        sub = grid_results[grid_results["Z"] == z]
        pivot = sub.pivot(index="X", columns="N", values=metric)
        pivot = pivot.sort_index(ascending=True)

        fig, ax = plt.subplots(figsize=(6, 5))
        data = pivot.values.astype(float)
        im = ax.imshow(data, cmap="RdYlGn", aspect="auto",
                       vmin=np.nanmin(data), vmax=np.nanmax(data))

        ax.set_xticks(range(len(pivot.columns)))
        ax.set_xticklabels(pivot.columns)
        ax.set_yticks(range(len(pivot.index)))
        ax.set_yticklabels(pivot.index)
        # matplotlib の既定フォントは日本語グリフを持たないため軸ラベルは ASCII。
        ax.set_xlabel("N (holding days)")
        ax.set_ylabel("X (surprise threshold %)")
        z_disp = "none" if z == "none" else f"-{z}%"
        ax.set_title(f"Expectancy per trade (%)   Z={z_disp}")

        # セルに数値を描画
        for i in range(data.shape[0]):
            for j in range(data.shape[1]):
                val = data[i, j]
                if not np.isnan(val):
                    ax.text(j, i, f"{val:.2f}", ha="center", va="center", fontsize=8)

        fig.colorbar(im, ax=ax, label="expectancy_pct")
        fig.tight_layout()
        path = config.RESULTS_DIR / f"heatmap_expectancy_Z{z}.png"
        fig.savefig(path, dpi=120)
        plt.close(fig)
        paths.append(path)
    return paths


def _fmt(v, pct=False, yen=False):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "N/A"
    if isinstance(v, float) and np.isinf(v):
        return "∞"
    if yen:
        return f"{v:,.0f} 円"
    if pct:
        return f"{v:.2f} %"
    if isinstance(v, float):
        return f"{v:.2f}"
    return str(v)


def _metrics_table(m: dict) -> str:
    rows = [
        ("トレード数", _fmt(m["n_trades"])),
        ("勝率", _fmt(m["win_rate"], pct=True)),
        ("平均利益", _fmt(m["avg_win"], yen=True)),
        ("平均損失", _fmt(m["avg_loss"], yen=True)),
        ("期待値(円/トレード)", _fmt(m["expectancy_yen"], yen=True)),
        ("期待リターン(%/トレード)", _fmt(m["expectancy_pct"], pct=True)),
        ("プロフィットファクター", _fmt(m["profit_factor"])),
        ("最大ドローダウン", _fmt(m["max_drawdown_pct"], pct=True)),
        ("年次リターン(CAGR)", _fmt(m["annual_return_pct"], pct=True)),
        ("累計リターン", _fmt(m["total_return_pct"], pct=True)),
    ]
    lines = ["| 指標 | 値 |", "| --- | ---: |"]
    lines += [f"| {k} | {v} |" for k, v in rows]
    return "\n".join(lines)


def save_oos_report(
    best_params: "config.StrategyParams",
    is_metrics: dict,
    oos_metrics: dict,
    best_row: pd.Series,
    n_universe: int,
    path: Path | None = None,
) -> Path:
    """
    最良パラメータの in-sample / out-of-sample 成績サマリを Markdown で保存。
    """
    path = path or (config.RESULTS_DIR / "oos_summary.md")
    wf = config.WALK_FORWARD
    z_disp = "なし" if best_params.stop_loss is None else f"{best_params.stop_loss:g}%"

    md = f"""# PEAD(決算後ドリフト)戦略 walk-forward 検証レポート

日本株の決算サプライズ戦略「サプライズ銘柄を翌営業日寄付で買い、N営業日後の引けで手仕舞い」を
walk-forward で検証した結果サマリ。第一評価軸は勝率ではなく **1トレードあたり期待リターン** と **最大DD**。

## 検証設計

- **in-sample(パラメータ選択)**: {wf.is_start} 〜 {wf.is_end}
- **out-of-sample(検証)**: {wf.oos_start} 〜 {wf.oos_end}
- ユニバース銘柄数: {n_universe}(廃止銘柄を含め生存者バイアスを排除)
- 資金 {config.PORTFOLIO.initial_capital:,.0f} 円 / 1銘柄 {config.PORTFOLIO.position_fraction:.0%} / 同時最大 {config.PORTFOLIO.max_positions} 銘柄
- 手数料+スリッページ 片道 {config.PORTFOLIO.fee_rate:.1%}
- エントリーフィルタ(すべて開示日より前のデータのみで判定):
  - 流動性: 直近{config.PORTFOLIO.turnover_window}日平均売買代金 ≥ {config.PORTFOLIO.min_turnover:,.0f} 円
  - 営業利益率 ≥ {config.PORTFOLIO.min_operating_margin:g}%(営業利益 ÷ 売上高)
  - RSI({config.PORTFOLIO.rsi_period}) ≤ {config.PORTFOLIO.rsi_upper:g}(過熱・急騰後を除外)
  - 年率ボラティリティ(直近{config.PORTFOLIO.vol_window}日) ≤ {config.PORTFOLIO.max_annual_vol:g}%

## 選択された最良パラメータ(in-sample 最適)

- サプライズ閾値 **X = {best_params.surprise_threshold:g}%**
- 保有日数 **N = {best_params.hold_days} 営業日**
- 損切り **Z = {z_disp}**

選択基準: 1トレードあたり期待リターン最大(同点時は PF → 最大DD の順)。

## in-sample 成績

{_metrics_table(is_metrics)}

## out-of-sample 成績(本命)

{_metrics_table(oos_metrics)}

## 解釈メモ

- in-sample と out-of-sample の期待リターン/PF の乖離が大きい場合、パラメータが
  過適合している可能性が高い。OOS の期待値がプラスかつ最大DDが許容範囲かで実運用可否を判断する。
- 本バックテストはルックアヘッド排除(翌営業日寄付エントリー、開示日前のみで流動性判定)と
  生存者バイアス排除(廃止銘柄を含むユニバース)を実装済みだが、
  約定の完全再現(寄付の板厚・比例配分・IPO直後の値付き)までは織り込んでいない点に留意。

---
*このレポートは `run_pipeline.py` により自動生成されます。*
"""
    path.write_text(md, encoding="utf-8")
    return path
