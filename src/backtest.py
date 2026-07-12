"""
backtest.py — 日次ポートフォリオ・シミュレータ、指標計算、walk-forward。

エントリー/エグジット規則(ルックアヘッド排除)
------------------------------------------------
* エントリー: 決算開示日の「翌営業日の寄付(Open)」で買う。
  発表が場中でも引け後でも一律「翌営業日寄付」に統一することで、開示当日の
  値動きを取りに行くルックアヘッドを完全に排除する。
* エグジット: エントリーから N営業日後の「引け(Close)」で手仕舞い。
* 損切り: -Z% を割ったら手仕舞い。日中安値(Low)が損切り価格に触れた日に約定。
  ギャップダウンで寄付が既に損切り価格以下なら寄付(Open)で約定させる(保守的)。

ポジション管理
--------------
* 1銘柄あたり資金の position_fraction(=10%)、同時最大 max_positions(=8)。
* サイジングは初期資金基準の固定比率(fixed fractional)。複利による経路依存を
  避け、パラメータ間の期待値比較を公平にするため。DD・年次リターンは日次時価
  評価のエクイティカーブから算出する。
* 手数料+スリッページは片道 fee_rate(=0.1%)を売買それぞれで控除。
* エントリー枠が競合した日は、サプライズ率の大きい順に優先して埋める。
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

import config


@dataclass
class Trade:
    code: str
    entry_date: pd.Timestamp
    entry_price: float
    exit_date: pd.Timestamp
    exit_price: float
    shares: int
    surprise_pct: float
    exit_reason: str          # "time" | "stop"
    gross_pnl: float          # 手数料前損益(円)
    fees: float               # 往復手数料(円)
    net_pnl: float            # 手数料後損益(円)
    return_pct: float         # 手数料後リターン(%)、エントリー約定額基準


# ---------------------------------------------------------------------------
# 営業日カレンダー・ヘルパ
# ---------------------------------------------------------------------------
def _next_trading_day_index(dates: pd.DatetimeIndex, after: pd.Timestamp) -> int | None:
    """after より厳密に後の最初の営業日の位置(index)。無ければ None。"""
    pos = dates.searchsorted(after, side="right")
    return int(pos) if pos < len(dates) else None


# ---------------------------------------------------------------------------
# シミュレーション本体
# ---------------------------------------------------------------------------
def run_backtest(
    signals: pd.DataFrame,
    quotes_by_code: dict[str, pd.DataFrame],
    params: "config.StrategyParams",
    portfolio: "config.PortfolioConfig" = config.PORTFOLIO,
    trading_calendar: pd.DatetimeIndex | None = None,
) -> tuple[list[Trade], pd.DataFrame]:
    """
    シグナルからトレードを生成し、日次エクイティカーブを構築する。

    返り値: (trades, equity_curve)
      equity_curve: index=営業日, 列=["equity"] の DataFrame
    """
    # 全銘柄共通の営業日カレンダー(価格が存在する日の和集合)
    if trading_calendar is None:
        all_dates = pd.DatetimeIndex([])
        for df in quotes_by_code.values():
            if df is not None and not df.empty:
                all_dates = all_dates.union(pd.DatetimeIndex(df["Date"]))
        trading_calendar = all_dates.sort_values()

    # 各銘柄の価格を日付 index の DataFrame にしておく(高速アクセス)
    price_map = {
        code: df.set_index("Date").sort_index()
        for code, df in quotes_by_code.items()
        if df is not None and not df.empty
    }

    # 開示日 → その日開示のシグナル一覧(サプライズ降順)
    signals = signals.sort_values(["DisclosedDate", "SurprisePct"], ascending=[True, False])

    # エントリー予定日ごとにシグナルを割り付ける。
    #   エントリー日 = 開示日の翌営業日(その銘柄に価格がある最初の日)
    pending: dict[pd.Timestamp, list[dict]] = {}
    for s in signals.itertuples(index=False):
        code = str(s.Code)
        dates = price_map.get(code)
        if dates is None:
            continue
        idx = _next_trading_day_index(dates.index, s.DisclosedDate)
        if idx is None:
            continue
        entry_date = dates.index[idx]
        pending.setdefault(entry_date, []).append(
            {"code": code, "surprise": s.SurprisePct, "entry_idx": idx}
        )

    position_cost = portfolio.initial_capital * portfolio.position_fraction

    cash = portfolio.initial_capital
    open_positions: list[dict] = []
    trades: list[Trade] = []
    equity_records = []

    for day in trading_calendar:
        # --- 1) エグジット処理(その日にエグジット条件を満たすもの) ---
        still_open = []
        for pos in open_positions:
            pdf = price_map[pos["code"]]
            if day not in pdf.index:
                still_open.append(pos)
                continue
            row = pdf.loc[day]
            exit_price = None
            reason = None

            # 損切り判定(保有初日以降、当日を含む各日でチェック)
            if params.stop_loss is not None and day >= pos["entry_date"]:
                stop_price = pos["entry_price"] * (1 - params.stop_loss / 100.0)
                if row["Open"] <= stop_price:
                    # ギャップダウン: 寄付で約定(損切り価格より不利)
                    exit_price, reason = float(row["Open"]), "stop"
                elif row["Low"] <= stop_price:
                    # 日中に損切り価格へタッチ: 損切り価格で約定
                    exit_price, reason = stop_price, "stop"

            # 時間切れエグジット(N営業日後の引け)。損切りが優先。
            if exit_price is None and day >= pos["exit_date"]:
                exit_price, reason = float(row["Close"]), "time"

            if exit_price is None:
                still_open.append(pos)
                continue

            # 約定・損益計算(往復手数料控除)
            entry_val = pos["entry_price"] * pos["shares"]
            exit_val = exit_price * pos["shares"]
            fees = (entry_val + exit_val) * portfolio.fee_rate
            gross = exit_val - entry_val
            net = gross - fees
            cash += exit_val - exit_val * portfolio.fee_rate  # 売却代金 − 売り手数料
            trades.append(
                Trade(
                    code=pos["code"],
                    entry_date=pos["entry_date"],
                    entry_price=pos["entry_price"],
                    exit_date=day,
                    exit_price=exit_price,
                    shares=pos["shares"],
                    surprise_pct=pos["surprise"],
                    exit_reason=reason,
                    gross_pnl=gross,
                    fees=fees,
                    net_pnl=net,
                    return_pct=net / entry_val * 100.0,
                )
            )
        open_positions = still_open

        # --- 2) エントリー処理(その日がエントリー日のシグナル、枠が空いていれば) ---
        todays = pending.get(day, [])
        todays.sort(key=lambda d: d["surprise"], reverse=True)  # サプライズ大きい順
        for sig in todays:
            if len(open_positions) >= portfolio.max_positions:
                break
            pdf = price_map[sig["code"]]
            if day not in pdf.index:
                continue
            open_price = float(pdf.loc[day, "Open"])
            if open_price <= 0:
                continue
            shares = int(position_cost // (open_price * portfolio.lot_size)) * portfolio.lot_size
            if shares <= 0:
                continue
            entry_val = open_price * shares
            buy_cost = entry_val + entry_val * portfolio.fee_rate  # 買付代金 + 買い手数料
            if buy_cost > cash:
                continue
            cash -= buy_cost
            # N営業日後の引けをエグジット予定日として算出
            exit_idx = sig["entry_idx"] + params.hold_days
            exit_date = pdf.index[min(exit_idx, len(pdf.index) - 1)]
            open_positions.append(
                {
                    "code": sig["code"],
                    "entry_date": day,
                    "entry_price": open_price,
                    "shares": shares,
                    "surprise": sig["surprise"],
                    "exit_date": exit_date,
                }
            )

        # --- 3) 日次時価評価(現金 + 保有ポジションの当日終値評価) ---
        holdings_value = 0.0
        for pos in open_positions:
            pdf = price_map[pos["code"]]
            if day in pdf.index:
                holdings_value += float(pdf.loc[day, "Close"]) * pos["shares"]
            else:
                holdings_value += pos["entry_price"] * pos["shares"]  # 直近値でつなぐ
        equity_records.append({"Date": day, "equity": cash + holdings_value})

    equity_curve = pd.DataFrame(equity_records).set_index("Date")
    return trades, equity_curve


# ---------------------------------------------------------------------------
# 指標計算
# ---------------------------------------------------------------------------
def compute_metrics(
    trades: list[Trade],
    equity_curve: pd.DataFrame,
    portfolio: "config.PortfolioConfig" = config.PORTFOLIO,
) -> dict:
    """
    要件の出力指標を計算して dict で返す。
      トレード数, 勝率, 平均利益, 平均損失, 期待値(円/率),
      プロフィットファクター, 最大DD, 年次リターン(CAGR)
    """
    n = len(trades)
    if n == 0:
        return {
            "n_trades": 0, "win_rate": np.nan, "avg_win": np.nan, "avg_loss": np.nan,
            "expectancy_yen": np.nan, "expectancy_pct": np.nan, "profit_factor": np.nan,
            "max_drawdown_pct": np.nan, "annual_return_pct": np.nan, "total_return_pct": np.nan,
        }

    pnls = np.array([t.net_pnl for t in trades])
    rets = np.array([t.return_pct for t in trades])
    wins = pnls[pnls > 0]
    losses = pnls[pnls < 0]

    win_rate = len(wins) / n * 100.0
    avg_win = wins.mean() if len(wins) else 0.0
    avg_loss = losses.mean() if len(losses) else 0.0
    expectancy_yen = pnls.mean()             # 1トレードあたり期待値(円)
    expectancy_pct = rets.mean()             # 1トレードあたり期待リターン(%)= 第一評価軸
    gross_profit = wins.sum()
    gross_loss = -losses.sum()
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else np.inf

    # 最大ドローダウン(日次時価エクイティから)
    eq = equity_curve["equity"]
    running_max = eq.cummax()
    drawdown = (eq - running_max) / running_max
    max_dd = drawdown.min() * 100.0 if len(eq) else np.nan

    # 年次リターン(CAGR):エクイティ始点→終点、営業日数を年換算(252営業日)
    total_return_pct = (eq.iloc[-1] / eq.iloc[0] - 1.0) * 100.0 if len(eq) else np.nan
    years = len(eq) / 252.0 if len(eq) else np.nan
    if years and years > 0 and eq.iloc[0] > 0:
        cagr = ((eq.iloc[-1] / eq.iloc[0]) ** (1.0 / years) - 1.0) * 100.0
    else:
        cagr = np.nan

    return {
        "n_trades": n,
        "win_rate": win_rate,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "expectancy_yen": expectancy_yen,
        "expectancy_pct": expectancy_pct,
        "profit_factor": profit_factor,
        "max_drawdown_pct": max_dd,
        "annual_return_pct": cagr,
        "total_return_pct": total_return_pct,
    }


def trades_to_dataframe(trades: list[Trade]) -> pd.DataFrame:
    """トレードログを CSV 出力用の DataFrame に変換。"""
    if not trades:
        return pd.DataFrame(
            columns=[
                "Code", "EntryDate", "EntryPrice", "ExitDate", "ExitPrice",
                "Shares", "SurprisePct", "ExitReason", "GrossPnL", "Fees",
                "NetPnL", "ReturnPct",
            ]
        )
    return pd.DataFrame(
        [
            {
                "Code": t.code,
                "EntryDate": t.entry_date.date(),
                "EntryPrice": round(t.entry_price, 2),
                "ExitDate": t.exit_date.date(),
                "ExitPrice": round(t.exit_price, 2),
                "Shares": t.shares,
                "SurprisePct": round(t.surprise_pct, 2),
                "ExitReason": t.exit_reason,
                "GrossPnL": round(t.gross_pnl, 0),
                "Fees": round(t.fees, 0),
                "NetPnL": round(t.net_pnl, 0),
                "ReturnPct": round(t.return_pct, 3),
            }
            for t in trades
        ]
    )


# ---------------------------------------------------------------------------
# 期間フィルタと walk-forward
# ---------------------------------------------------------------------------
def _filter_signals_by_period(
    signals: pd.DataFrame, start: str, end: str
) -> pd.DataFrame:
    """開示日が [start, end] に入るシグナルのみ抽出。"""
    s, e = pd.Timestamp(start), pd.Timestamp(end)
    mask = (signals["DisclosedDate"] >= s) & (signals["DisclosedDate"] <= e)
    return signals.loc[mask].reset_index(drop=True)


def grid_search(
    surprises: pd.DataFrame,
    quotes_by_code: dict[str, pd.DataFrame],
    period_start: str,
    period_end: str,
    grid: "config.ParamGrid" = config.PARAM_GRID,
    portfolio: "config.PortfolioConfig" = config.PORTFOLIO,
) -> pd.DataFrame:
    """
    指定期間(in-sample)で全パラメータ組をバックテストし、指標表を返す。
    signals は各パラメータの X 閾値に依存するため、組ごとに生成する。
    """
    from src import signals as sig_mod

    results = []
    for params in grid.combinations():
        sigs = sig_mod.generate_signals(surprises, quotes_by_code, params, portfolio)
        sigs = _filter_signals_by_period(sigs, period_start, period_end)
        trades, equity = run_backtest(sigs, quotes_by_code, params, portfolio)
        metrics = compute_metrics(trades, equity, portfolio)
        results.append(
            {
                "label": params.label,
                "X": params.surprise_threshold,
                "N": params.hold_days,
                "Z": "none" if params.stop_loss is None else params.stop_loss,
                **metrics,
            }
        )
    return pd.DataFrame(results)


def select_best(
    grid_results: pd.DataFrame, metric: str = config.SELECTION_METRIC
) -> pd.Series:
    """
    in-sample 成績から最良パラメータを選択。
    第一評価軸は「1トレードあたり期待リターン(%)」。
    同点時はプロフィットファクター、次いで最大DD(浅い方)で優先度をつける。
    トレード数が極端に少ない組は過適合・偶然の温床なので最低本数で足切り。
    """
    df = grid_results.copy()
    df = df[df["n_trades"] >= 10]  # 最低トレード数フィルタ
    if df.empty:
        # 足切りで全滅した場合は本数条件を外して選ぶ
        df = grid_results.copy()
    df = df.sort_values(
        by=[metric, "profit_factor", "max_drawdown_pct"],
        ascending=[False, False, False],  # DD は負値なので大きい(=浅い)方が良い
    )
    return df.iloc[0]
