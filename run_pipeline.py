#!/usr/bin/env python3
"""
run_pipeline.py — PEAD バックテストのオーケストレーション。

段取り(要件どおり、小さく確認してから全体へ)
------------------------------------------------
  --smoke     : 合成データ 1銘柄・1年でパイプラインの疎通のみ確認
  --synthetic : 合成データ 全銘柄・全期間で walk-forward 一式を実行(APIキー不要)
  (無指定)   : J-Quants 実データで walk-forward 一式を実行(.env に認証情報が必要)

出力(results/)
  - grid_search_results.csv    : in-sample 全パラメータ組の成績
  - heatmap_expectancy_Z*.png  : X×N の期待リターン・ヒートマップ(Zごと)
  - oos_summary.md             : 最良パラメータの OOS 成績サマリ
  - trade_log_is.csv / trade_log_oos.csv : 個別トレードログ
"""
from __future__ import annotations

import argparse
import sys

import pandas as pd

import config
from src import backtest, report, signals


def _prepare_surprises(statements_by_code: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """全銘柄の財務情報からサプライズ表(X非依存)を作る。"""
    frames = [signals.compute_surprises(s) for s in statements_by_code.values()]
    frames = [f for f in frames if not f.empty]
    if not frames:
        return signals.compute_surprises(pd.DataFrame())
    return pd.concat(frames, ignore_index=True)


def run_walk_forward(
    quotes_by_code: dict[str, pd.DataFrame],
    statements_by_code: dict[str, pd.DataFrame],
) -> None:
    """walk-forward 一式:IS グリッドサーチ → 最良選択 → OOS 検証 → 出力。"""
    wf = config.WALK_FORWARD
    surprises = _prepare_surprises(statements_by_code)
    n_universe = len([c for c, d in quotes_by_code.items() if not d.empty])

    print(f"[1/5] サプライズ計算: {len(surprises)} 件 / ユニバース {n_universe} 銘柄")

    # X非依存のシグナル特徴量(売買代金・RSI・ボラ)は全期間分を1回だけ計算し、
    # 最良パラメータの IS/OOS 再実行でも使い回す(grid_search 内は期間限定で別計算)。
    features_all = signals.compute_signal_features(surprises, quotes_by_code)

    # --- in-sample グリッドサーチ ---
    print(f"[2/5] in-sample グリッドサーチ ({wf.is_start}〜{wf.is_end}) ...")
    grid_results = backtest.grid_search(
        surprises, quotes_by_code, wf.is_start, wf.is_end
    )
    csv_path = report.save_grid_csv(grid_results)
    heatmaps = report.save_expectancy_heatmaps(grid_results)
    print(f"      → {csv_path}")
    for h in heatmaps:
        print(f"      → {h}")

    # --- 最良パラメータ選択 ---
    best_row = backtest.select_best(grid_results)
    best_params = config.StrategyParams(
        surprise_threshold=float(best_row["X"]),
        hold_days=int(best_row["N"]),
        stop_loss=None if best_row["Z"] == "none" else float(best_row["Z"]),
    )
    print(f"[3/5] 最良パラメータ: {best_params.label} "
          f"(IS 期待リターン {best_row['expectancy_pct']:.2f}%)")

    # --- IS の最良パラメータでトレードログを再生成 ---
    is_sigs = backtest._filter_signals_by_period(
        signals.generate_signals(
            surprises, quotes_by_code, best_params, features=features_all
        ),
        wf.is_start, wf.is_end,
    )
    is_trades, is_equity = backtest.run_backtest(is_sigs, quotes_by_code, best_params)
    is_metrics = backtest.compute_metrics(is_trades, is_equity)
    report.save_trade_log(backtest.trades_to_dataframe(is_trades), "trade_log_is.csv")

    # --- out-of-sample 検証 ---
    print(f"[4/5] out-of-sample 検証 ({wf.oos_start}〜{wf.oos_end}) ...")
    oos_sigs = backtest._filter_signals_by_period(
        signals.generate_signals(
            surprises, quotes_by_code, best_params, features=features_all
        ),
        wf.oos_start, wf.oos_end,
    )
    oos_trades, oos_equity = backtest.run_backtest(oos_sigs, quotes_by_code, best_params)
    oos_metrics = backtest.compute_metrics(oos_trades, oos_equity)
    oos_log = report.save_trade_log(
        backtest.trades_to_dataframe(oos_trades), "trade_log_oos.csv"
    )

    # --- レポート ---
    md = report.save_oos_report(
        best_params, is_metrics, oos_metrics, best_row, n_universe
    )
    print(f"[5/5] レポート出力:")
    print(f"      → {md}")
    print(f"      → {oos_log}")
    print(f"\nOOS 成績: トレード {oos_metrics['n_trades']} 件 / "
          f"期待リターン {oos_metrics['expectancy_pct']:.2f}% / "
          f"PF {oos_metrics['profit_factor']:.2f} / "
          f"最大DD {oos_metrics['max_drawdown_pct']:.2f}%")


def load_real_data() -> tuple[dict, dict]:
    """J-Quants 実データを取得(キャッシュ優先)。ユニバースは価格の存在で構成。"""
    from src import data_fetch

    wf = config.WALK_FORWARD
    # 上場銘柄一覧(廃止銘柄も拾うため IS 期首・OOS 期末の2時点で取得し和集合)
    listed = pd.concat(
        [
            data_fetch.fetch_listed_info(wf.is_start.replace("-", "")),
            data_fetch.fetch_listed_info(wf.oos_end.replace("-", "")),
        ],
        ignore_index=True,
    )
    codes = sorted(set(listed["Code"].astype(str)))
    print(f"上場銘柄一覧: {len(codes)} コード(2時点和集合、生存者バイアス対策)")

    quotes_by_code, statements_by_code = {}, {}
    for i, code in enumerate(codes, 1):
        quotes_by_code[code] = data_fetch.fetch_daily_quotes(
            code, wf.is_start, wf.oos_end
        )
        statements_by_code[code] = data_fetch.fetch_statements(code)
        if i % 100 == 0:
            print(f"  取得中 {i}/{len(codes)} ...")
    return quotes_by_code, statements_by_code


def main() -> int:
    ap = argparse.ArgumentParser(description="PEAD バックテスト")
    ap.add_argument("--smoke", action="store_true",
                    help="合成データ1銘柄・1年で疎通確認のみ")
    ap.add_argument("--synthetic", action="store_true",
                    help="合成データ全体で walk-forward 実行(APIキー不要)")
    ap.add_argument("--screen", action="store_true",
                    help="全フィルタを満たす直近の候補銘柄を一覧表示(発掘用)")
    ap.add_argument("--screen-top", type=int, default=20,
                    help="--screen で表示する最大件数(既定20)")
    ap.add_argument("--screen-x", type=float, default=None,
                    help="--screen のサプライズ閾値X(既定は最小グリッド値)")
    args = ap.parse_args()

    if args.smoke:
        return run_smoke()

    if args.screen:
        return run_screen(args)

    if args.synthetic:
        from src import synthetic
        print("=== 合成データで walk-forward 実行 ===")
        quotes, stmts = synthetic.generate()
        run_walk_forward(quotes, stmts)
        return 0

    # 実データ
    print("=== J-Quants 実データで walk-forward 実行 ===")
    quotes, stmts = load_real_data()
    run_walk_forward(quotes, stmts)
    return 0


def run_smoke() -> int:
    """
    1銘柄・1年分でパイプラインが端から端まで通ることを確認する最小実行。
    合成データを使うので API キー不要。
    """
    from src import synthetic

    print("=== スモークテスト: 1銘柄・1年 ===")
    quotes, stmts = synthetic.generate(
        n_codes=1, start="2018-06-01", end="2019-12-31", seed=1
    )
    code = next(iter(quotes))
    print(f"銘柄 {code}: 価格 {len(quotes[code])} 本 / 決算 {len(stmts[code])} 件")

    surprises = signals.compute_surprises(stmts[code])
    print(f"サプライズ計算: {len(surprises)} 件")

    params = config.StrategyParams(surprise_threshold=5.0, hold_days=5, stop_loss=8.0)
    sigs = signals.generate_signals(surprises, quotes, params)
    print(f"シグナル抽出: {len(sigs)} 件")

    trades, equity = backtest.run_backtest(sigs, quotes, params)
    metrics = backtest.compute_metrics(trades, equity)
    print(f"トレード: {metrics['n_trades']} 件 / "
          f"期待リターン {metrics['expectancy_pct']:.2f}% / "
          f"勝率 {metrics['win_rate']:.1f}%")
    print("スモークテスト OK: パイプライン疎通確認完了")
    return 0


def run_screen(args) -> int:
    """
    全エントリーフィルタ(サプライズ/流動性/営業利益率/RSI上限/年率ボラ上限)を
    満たす候補銘柄を、開示日の新しい順に一覧表示する「銘柄発掘」モード。

    --synthetic 併用で合成データ、無指定なら J-Quants 実データを対象にする。
    各条件は開示日より前のデータのみで判定しており、ルックアヘッドは無い。
    """
    if args.synthetic:
        from src import synthetic
        print("=== 候補銘柄スクリーニング(合成データ) ===")
        quotes, stmts = synthetic.generate()
    else:
        print("=== 候補銘柄スクリーニング(J-Quants 実データ) ===")
        quotes, stmts = load_real_data()

    surprises = _prepare_surprises(stmts)
    # 保有日数・損切りはスクリーニングに無関係。X はグリッド最小値 or 指定値。
    x = args.screen_x if args.screen_x is not None else min(config.PARAM_GRID.surprise_thresholds)
    params = config.StrategyParams(surprise_threshold=x, hold_days=5, stop_loss=None)
    sigs = signals.generate_signals(surprises, quotes, params)

    p = config.PORTFOLIO
    print(f"\nフィルタ条件: サプライズ≥{x:g}% / 売買代金≥{p.min_turnover:,.0f}円 / "
          f"営業利益率≥{p.min_operating_margin:g}% / RSI≤{p.rsi_upper:g} / "
          f"年率ボラ≤{p.max_annual_vol:g}%")
    print(f"該当: {len(sigs)} 件\n")

    if sigs.empty:
        print("条件を満たす銘柄はありませんでした。")
        return 0

    view = sigs.sort_values("DisclosedDate", ascending=False).head(args.screen_top).copy()
    view["DisclosedDate"] = view["DisclosedDate"].dt.date
    view = view.rename(columns={
        "SurprisePct": "Surprise%", "OperatingMargin": "OpMargin%",
        "AnnualVol": "AnnVol%", "AvgTurnover": "AvgTurnover(円)",
    })
    cols = ["Code", "DisclosedDate", "PeriodType", "Surprise%",
            "OpMargin%", "RSI", "AnnVol%", "AvgTurnover(円)"]
    with pd.option_context("display.max_rows", None, "display.width", 200,
                           "display.float_format", lambda v: f"{v:,.2f}"):
        print(view[cols].to_string(index=False))

    out = config.RESULTS_DIR / "screen_candidates.csv"
    view[cols].to_csv(out, index=False, encoding="utf-8-sig")
    print(f"\n→ {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
