# 日本株 PEAD(決算後ドリフト)バックテスト

決算サプライズ銘柄を **翌営業日の寄付で買い、N営業日後の引けで手仕舞い** する戦略の
期待値を walk-forward で検証する pandas ベースの環境。評価軸は勝率ではなく
**1トレードあたり期待リターン** と **最大ドローダウン**。

## 特徴

- **ルックアヘッドバイアス排除**
  - エントリーは開示日の翌営業日寄付に統一(場中/引け後の発表でも一律)
  - 流動性フィルタは開示日より前の売買代金のみ参照(`Date < 開示日`)
- **生存者バイアス排除**
  - ユニバースは「期間中に価格が存在した全銘柄」の和集合(廃止銘柄も含む)
- 調整済み株価(分割・併合調整済み)を使用
- 取得データは `data/` に parquet でキャッシュし、再実行時は API を叩かない

## セットアップ

```bash
pip install -r requirements.txt
cp .env.example .env      # J_QUANTS_MAIL / J_QUANTS_PASS を記入
```

## 使い方(小さく確認してから全体へ)

```bash
# 1) まず疎通確認: 合成データ 1銘柄・1年でパイプラインが通るか
python run_pipeline.py --smoke

# 2) 合成データ全体で walk-forward 一式(APIキー不要・再現的な動作確認)
python run_pipeline.py --synthetic

# 3) J-Quants 実データで本番実行(.env が必要)
python run_pipeline.py

# 銘柄発掘: 全フィルタを満たす直近候補を一覧(発表日の新しい順)
python run_pipeline.py --screen                 # 実データ
python run_pipeline.py --screen --synthetic     # 合成データで動作確認
#   → results/screen_candidates.csv にも保存
#   オプション: --screen-top N(表示件数) / --screen-x X(サプライズ閾値)

# ユニットテスト
python -m tests.test_smoke      # または: pytest tests/
```

## 戦略ルール(すべて `config.py` でパラメータ化)

| 記号 | 意味 | グリッド |
| --- | --- | --- |
| X | サプライズ閾値(通期計画の期間按分対比 営業利益 +X% 以上) | 5, 10, 15, 20 % |
| N | 保有営業日数(N営業日後の引けで手仕舞い) | 3, 5, 10, 20 |
| Z | 損切りライン(-Z%) | なし, 5, 8 % |

- サプライズ = (累計実績営業利益 / 通期計画×進捗率 − 1) × 100
  - 進捗率: 1Q=0.25 / 2Q=0.50 / 3Q=0.75 / FY=1.00
- 資金250万円 / 1銘柄10% / 同時最大8銘柄 / 手数料+スリッページ 片道0.1%

### エントリーフィルタ(すべて `config.py` の `PortfolioConfig`。開示日より前のデータのみで判定=ルックアヘッド排除)

| 条件 | 既定 | 意図 |
| --- | --- | --- |
| 直近20日平均売買代金 | ≥ 1億円 | 流動性 |
| 営業利益率(営業利益 ÷ 売上高) | ≥ 8% | 収益性 |
| RSI(14, Wilder) | ≤ 55 | 過熱・急騰後(出尽くし)を除外 |
| 年率ボラティリティ(直近20日) | ≤ 60% | 荒すぎる値動き(≒日次±3.8%超)を除外し DD を抑制 |

## walk-forward

- in-sample **2019〜2022** で 4×4×3=48 通りをグリッドサーチ → 最良パラメータ選択
  (第一評価軸: 期待リターン%、同点時は PF → 最大DD)
- out-of-sample **2023〜2025** で最良パラメータを検証

## 出力(`results/`)

| ファイル | 内容 |
| --- | --- |
| `grid_search_results.csv` | in-sample 全48組の成績 |
| `heatmap_expectancy_Z*.png` | X×N の期待リターン・ヒートマップ(Zごと) |
| `oos_summary.md` | 最良パラメータの IS / OOS 成績サマリ |
| `trade_log_is.csv` / `trade_log_oos.csv` | 個別トレードログ(銘柄・日付・損益) |
| `screen_candidates.csv` | `--screen` 実行時の候補銘柄一覧(サプライズ/利益率/RSI/ボラ付き) |

## モジュール構成

```
config.py            パラメータ・パス・グリッド定義
src/data_fetch.py    J-Quants クライアント + parquet キャッシュ
src/signals.py       サプライズ算出 + 流動性フィルタ
src/backtest.py      日次ポートフォリオ・シミュレータ + walk-forward + 指標
src/report.py        CSV / ヒートマップ / Markdown レポート
src/synthetic.py     合成データ生成器(APIキー無しの検証用)
run_pipeline.py      オーケストレーション(--smoke / --synthetic / 実データ)
tests/test_smoke.py  疎通・ロジックのユニットテスト
```

## 注意・限界

- J-Quants **ライトプラン**は取得可能な履歴期間・遅延・レート制限に制約がある。
  期間・銘柄数によっては実データが揃わない場合がある。
- 約定の完全再現(寄付の板厚・比例配分・ストップ約定の滑り)までは織り込んでいない。
  手数料+スリッページ 0.1%/片道で近似している。
- `--synthetic` の結果は**人工データ**であり戦略の実力評価ではない。配線確認用。
- まだ機械学習(LightGBM)は使わない。ルールベース検証を優先する方針。
