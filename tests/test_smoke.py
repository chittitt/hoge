"""
test_smoke.py — パイプライン疎通と主要ロジックの最小テスト。

依存を増やさないため pytest なしでも動くよう assert ベースの関数群にし、
`python -m tests.test_smoke` でも実行できるようにしている(pytest でも拾える)。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

import config
from src import backtest, signals, synthetic


def test_surprise_calculation():
    """期間按分に対するサプライズ率が定義どおり計算されること。"""
    stmt = pd.DataFrame(
        [{
            "LocalCode": "10000",
            "DisclosedDate": "2020-05-10",
            "DisclosedTime": "15:00",
            "TypeOfCurrentPeriod": "2Q",       # 進捗率 0.5
            "OperatingProfit": "660",          # 累計実績
            "ForecastOperatingProfit": "1200", # 通期計画 → 按分 600
        }]
    )
    out = signals.compute_surprises(stmt)
    assert len(out) == 1
    # (660 / (1200*0.5) - 1) * 100 = +10%
    assert abs(out.iloc[0]["SurprisePct"] - 10.0) < 1e-9


def test_negative_forecast_excluded():
    """赤字計画(分母<=0)はサプライズ計算から除外されること。"""
    stmt = pd.DataFrame(
        [{
            "LocalCode": "10000", "DisclosedDate": "2020-05-10", "DisclosedTime": "15:00",
            "TypeOfCurrentPeriod": "2Q", "OperatingProfit": "100",
            "ForecastOperatingProfit": "-500",
        }]
    )
    out = signals.compute_surprises(stmt)
    assert out.empty


def test_no_lookahead_turnover():
    """流動性判定が開示日当日以降の売買代金を使わないこと。"""
    dates = pd.bdate_range("2020-01-01", periods=40)
    q = pd.DataFrame({
        "Date": dates, "Code": "10000",
        "Open": 100.0, "High": 101.0, "Low": 99.0, "Close": 100.0,
        "Volume": 1000.0,
        # 開示日当日に売買代金を激増させる。使われていなければ平均は跳ねない。
        "TurnoverValue": [1e8] * 39 + [1e15],
    })
    as_of = dates[39]  # 最終日を開示日とみなす
    avg = signals.average_turnover_before(q, as_of, window=20)
    assert avg < 2e8  # 巨大な当日値が混入していないこと


def test_entry_is_next_business_day():
    """エントリーが開示日翌営業日の寄付になること(ルックアヘッド排除)。"""
    dates = pd.bdate_range("2020-01-06", periods=30)  # 月曜開始
    q = pd.DataFrame({
        "Date": dates, "Code": "10000",
        "Open": np.linspace(100, 130, 30),
        "High": np.linspace(101, 131, 30),
        "Low": np.linspace(99, 129, 30),
        "Close": np.linspace(100, 130, 30),
        "Volume": 1e6, "TurnoverValue": 2e8,
    })
    sig = pd.DataFrame([{
        "Code": "10000", "DisclosedDate": dates[5], "PeriodType": "1Q",
        "SurprisePct": 20.0, "AvgTurnover": 2e8,
    }])
    params = config.StrategyParams(surprise_threshold=5.0, hold_days=5, stop_loss=None)
    trades, _ = backtest.run_backtest(sig, {"10000": q}, params)
    assert len(trades) == 1
    # 開示日 dates[5] の翌営業日 dates[6] の寄付でエントリー
    assert trades[0].entry_date == dates[6]
    assert abs(trades[0].entry_price - q.iloc[6]["Open"]) < 1e-9


def test_stop_loss_triggers():
    """損切りラインを割ったら time exit より前に stop で手仕舞いされること。"""
    dates = pd.bdate_range("2020-01-06", periods=30)
    close = np.full(30, 100.0)
    low = np.full(30, 99.0)
    # エントリー翌々日に急落させる
    entry_i = 6
    low[entry_i + 2] = 80.0   # -20% 安値
    close[entry_i + 2] = 82.0
    q = pd.DataFrame({
        "Date": dates, "Code": "10000",
        "Open": close, "High": close + 1, "Low": low, "Close": close,
        "Volume": 1e6, "TurnoverValue": 2e8,
    })
    sig = pd.DataFrame([{
        "Code": "10000", "DisclosedDate": dates[5], "PeriodType": "1Q",
        "SurprisePct": 20.0, "AvgTurnover": 2e8,
    }])
    params = config.StrategyParams(surprise_threshold=5.0, hold_days=10, stop_loss=8.0)
    trades, _ = backtest.run_backtest(sig, {"10000": q}, params)
    assert len(trades) == 1
    assert trades[0].exit_reason == "stop"
    # 損切り価格 = 100 * (1-0.08) = 92 付近で約定(ギャップ寄付80 <= 92 なので寄付約定)
    assert trades[0].exit_price <= 92.0 + 1e-9


def test_fees_deducted():
    """往復手数料が損益に反映されていること(fee>0)。"""
    dates = pd.bdate_range("2020-01-06", periods=30)
    q = pd.DataFrame({
        "Date": dates, "Code": "10000",
        "Open": 100.0, "High": 101.0, "Low": 99.0, "Close": 100.0,
        "Volume": 1e6, "TurnoverValue": 2e8,
    })
    sig = pd.DataFrame([{
        "Code": "10000", "DisclosedDate": dates[5], "PeriodType": "1Q",
        "SurprisePct": 20.0, "AvgTurnover": 2e8,
    }])
    params = config.StrategyParams(surprise_threshold=5.0, hold_days=5, stop_loss=None)
    trades, _ = backtest.run_backtest(sig, {"10000": q}, params)
    assert len(trades) == 1
    assert trades[0].fees > 0
    # 横ばい相場では手数料ぶんだけマイナス
    assert trades[0].net_pnl < 0


def test_operating_margin_in_surprises():
    """営業利益率が 営業利益÷売上高 で計算されること。"""
    stmt = pd.DataFrame(
        [{
            "LocalCode": "10000", "DisclosedDate": "2020-05-10", "DisclosedTime": "15:00",
            "TypeOfCurrentPeriod": "2Q", "OperatingProfit": "600",
            "ForecastOperatingProfit": "1200", "NetSales": "6000",  # 600/6000=10%
        }]
    )
    out = signals.compute_surprises(stmt)
    assert abs(out.iloc[0]["OperatingMargin"] - 10.0) < 1e-9


def test_operating_margin_filter():
    """営業利益率が下限(8%)未満のシグナルは除外されること。"""
    quotes = pd.DataFrame({
        "Date": pd.bdate_range("2019-06-03", periods=60), "Code": "10000",
        "Open": 100.0, "High": 101.0, "Low": 99.0, "Close": 100.0,
        "Volume": 1e6, "TurnoverValue": 2e8,
    })
    # 営業利益率 5%(<8%)なのでサプライズが大きくても弾かれる
    sur = pd.DataFrame([{
        "Code": "10000", "DisclosedDate": pd.Timestamp("2019-09-01"),
        "DisclosedTime": "15:00", "PeriodType": "2Q",
        "ActualOP": 500.0, "ForecastOP": 800.0, "ProratedForecast": 400.0,
        "SurprisePct": 25.0, "NetSales": 10000.0, "OperatingMargin": 5.0,
    }])
    params = config.StrategyParams(surprise_threshold=5.0, hold_days=5, stop_loss=None)
    out = signals.generate_signals(sur, {"10000": quotes}, params)
    assert out.empty


def test_rsi_upper_filter():
    """強い連騰で RSI が上限(55)超なら除外、緩やかなら通過すること。"""
    # 連騰(毎日上昇)→ RSI は 100 近辺
    up = pd.DataFrame({
        "Date": pd.bdate_range("2019-06-03", periods=60), "Code": "10000",
        "Open": np.linspace(100, 200, 60), "High": np.linspace(101, 201, 60),
        "Low": np.linspace(99, 199, 60), "Close": np.linspace(100, 200, 60),
        "Volume": 1e6, "TurnoverValue": 2e8,
    })
    as_of = pd.Timestamp("2019-09-01")
    rsi_up = signals.rsi_before(up, as_of, period=14)
    assert rsi_up > 55.0

    sur = pd.DataFrame([{
        "Code": "10000", "DisclosedDate": as_of, "DisclosedTime": "15:00",
        "PeriodType": "2Q", "ActualOP": 500.0, "ForecastOP": 800.0,
        "ProratedForecast": 400.0, "SurprisePct": 25.0,
        "NetSales": 5000.0, "OperatingMargin": 10.0,
    }])
    params = config.StrategyParams(surprise_threshold=5.0, hold_days=5, stop_loss=None)
    out = signals.generate_signals(sur, {"10000": up}, params)
    assert out.empty  # RSI 過熱で除外


def test_volatility_filter():
    """年率ボラティリティが上限(60%)超なら除外されること。"""
    rng = np.random.default_rng(0)
    n = 60
    # 日次リターン std 6% → 年率 ≈ 95%(>60%)
    rets = rng.normal(0, 0.06, n)
    close = 1000 * np.exp(np.cumsum(rets))
    vol_q = pd.DataFrame({
        "Date": pd.bdate_range("2019-06-03", periods=n), "Code": "10000",
        "Open": close, "High": close * 1.01, "Low": close * 0.99, "Close": close,
        "Volume": 1e6, "TurnoverValue": 2e8,
    })
    as_of = pd.Timestamp("2019-09-01")
    ann_vol = signals.annualized_vol_before(vol_q, as_of, window=20)
    assert ann_vol > 60.0

    sur = pd.DataFrame([{
        "Code": "10000", "DisclosedDate": as_of, "DisclosedTime": "15:00",
        "PeriodType": "2Q", "ActualOP": 500.0, "ForecastOP": 800.0,
        "ProratedForecast": 400.0, "SurprisePct": 25.0,
        "NetSales": 5000.0, "OperatingMargin": 10.0,
    }])
    params = config.StrategyParams(surprise_threshold=5.0, hold_days=5, stop_loss=None)
    out = signals.generate_signals(sur, {"10000": vol_q}, params)
    assert out.empty  # 高ボラで除外


def test_all_filters_pass():
    """全条件(サプライズ/流動性/利益率/RSI/ボラ)を満たせば通過すること。"""
    rng = np.random.default_rng(3)
    n = 60
    # 低ボラかつ横ばい(RSI ~ 50)の系列
    rets = rng.normal(0, 0.01, n)   # 年率 ≈ 16%
    close = 1000 * np.exp(np.cumsum(rets))
    q = pd.DataFrame({
        "Date": pd.bdate_range("2019-06-03", periods=n), "Code": "10000",
        "Open": close, "High": close * 1.005, "Low": close * 0.995, "Close": close,
        "Volume": 1e6, "TurnoverValue": 2e8,
    })
    as_of = pd.Timestamp("2019-09-01")
    sur = pd.DataFrame([{
        "Code": "10000", "DisclosedDate": as_of, "DisclosedTime": "15:00",
        "PeriodType": "2Q", "ActualOP": 500.0, "ForecastOP": 800.0,
        "ProratedForecast": 400.0, "SurprisePct": 25.0,
        "NetSales": 5000.0, "OperatingMargin": 10.0,  # 10% >= 8%
    }])
    params = config.StrategyParams(surprise_threshold=5.0, hold_days=5, stop_loss=None)
    out = signals.generate_signals(sur, {"10000": q}, params)
    assert len(out) == 1
    assert out.iloc[0]["RSI"] <= config.PORTFOLIO.rsi_upper
    assert out.iloc[0]["AnnualVol"] <= config.PORTFOLIO.max_annual_vol
    assert out.iloc[0]["OperatingMargin"] >= config.PORTFOLIO.min_operating_margin


def test_pipeline_end_to_end():
    """合成データで signals→backtest→metrics が例外なく通ること。"""
    quotes, stmts = synthetic.generate(n_codes=2, start="2018-06-01", end="2019-12-31")
    frames = [signals.compute_surprises(s) for s in stmts.values()]
    surprises = pd.concat(frames, ignore_index=True)
    params = config.StrategyParams(surprise_threshold=5.0, hold_days=5, stop_loss=8.0)
    sigs = signals.generate_signals(surprises, quotes, params)
    trades, equity = backtest.run_backtest(sigs, quotes, params)
    metrics = backtest.compute_metrics(trades, equity)
    assert "expectancy_pct" in metrics
    assert len(equity) > 0


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  PASS {fn.__name__}")
    print(f"\n{len(fns)} tests passed.")


if __name__ == "__main__":
    _run_all()
