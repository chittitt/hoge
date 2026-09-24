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
- 流動性フィルタ: 直近20日平均売買代金 ≥ 1億円(既定)
- 資金250万円 / 1銘柄10% / 同時最大8銘柄 / 手数料+スリッページ 片道0.1%

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

habit.py             習慣トラッカーの入口(後述、バックテストとは独立)
habits/models.py     習慣・記録・スケジュールの定義と値の正規化
habits/storage.py    JSON 1ファイルへの原子的な読み書き
habits/stats.py      継続率・ストリークの集計
habits/cli.py        サブコマンド定義と端末向け整形
tests/test_habits.py 習慣トラッカーのユニットテスト
```

## 注意・限界

- J-Quants **ライトプラン**は取得可能な履歴期間・遅延・レート制限に制約がある。
  期間・銘柄数によっては実データが揃わない場合がある。
- 約定の完全再現(寄付の板厚・比例配分・ストップ約定の滑り)までは織り込んでいない。
  手数料+スリッページ 0.1%/片道で近似している。
- `--synthetic` の結果は**人工データ**であり戦略の実力評価ではない。配線確認用。
- まだ機械学習(LightGBM)は使わない。ルールベース検証を優先する方針。

---

# 習慣トラッカー(`habit.py`)

生活を律するための記録ツール。バックテスト側とは独立していて、**標準ライブラリだけ**で動く
(pandas 等は不要)。記録は JSON 1ファイル、判定軸は「やった/やらない」ではなく
**継続率(達成率)** と **ストリーク(連続達成)**。

## 使い方

```bash
# 1) 習慣を登録する
python habit.py add wake  --name "6時に起きる" --kind time   --target 06:30 --cmp "<="
python habit.py add sleep --name "24時前に寝る" --kind time   --target 24:00 --cmp "<="
python habit.py add deep  --name "集中作業"     --kind number --target 2 --unit h
python habit.py add gym   --name "筋トレ"       --schedule weekly:3
python habit.py add read  --name "読書"         --schedule weekdays

# 2) その日の実績を記録する(値は位置引数、日付は --date で遡れる)
python habit.py done wake 06:12
python habit.py done deep 2.5 --note "午前に90分+午後60分"
python habit.py done gym
python habit.py miss read --date -1      # できなかった日を明示的に残す
python habit.py undo wake --date -1      # 記録の取り消し

# 3) 見る
python habit.py today                    # 今日やること + 未記録の残り
python habit.py report --days 30         # 継続率・ストリーク・カレンダー
python habit.py log wake --limit 14      # 1習慣の記録を新しい順に
python habit.py export --out habits_data/habits.csv
```

`report` の出力イメージ:

```
ID     習慣          頻度   達成/対象  達成率  連続  最長  平均   目標
wake   6時に起きる   毎日   24/28      86%     4日   11日  06:09  目標 06:30 以下
deep   集中作業      毎日   19/28      68%     2日   7日   1.73h  目標 2h 以上
gym    筋トレ        週3回  3/4        75%     2週   3週   -      目標 実行する

直近21日 [■達成 ×未達 □未記録 ・対象外]
  wake   ■■■■×■■■■■■■×■■■■■■■□
```

## 習慣の種類

| `--kind` | 記録する値 | 達成判定 | 例 |
| --- | --- | --- | --- |
| `check`(既定) | なし(やったかどうか) | 記録があれば達成 | 筋トレ・掃除 |
| `number` | 実数(`--unit` で単位) | `--target` と `--cmp` で比較 | 集中作業 2h 以上 |
| `time` | `HH:MM` | 同上 | 起床 06:30 以下 |

## 睡眠時間(自動算出)

睡眠時間は記録させない。就寝と起床を入れた時点で決まる値なので、三重に聞くと手間が増えて続かなくなる。
就寝と起床の時刻習慣に役割を付けておくと、`report` と `today` が自動で算出する。

```bash
python habit.py add sleep --name "24時までに寝る" --kind time --target 24:00 --cmp "<=" --role bedtime
python habit.py add wake  --name "6時30分までに起きる" --kind time --target 06:30 --cmp "<=" --role wakeup
python habit.py role sleep bedtime      # 後から付け直す場合
```

```
睡眠時間(24時までに寝る → 6時30分までに起きる / 3泊)
  平均 6時間50分 / 最短 6時間10分 / 最長 7時間20分
  7時間以上の夜: 2/3(ばらつき 1時間10分)
```

- 就寝は当日、起床は翌日の記録なので、日付をまたいで組にする。両方そろっていない夜は集計に含めない。
- 平均だけでなく最短・最長も出す。睡眠は長さより**ばらつき**が効くため。
- 役割を1つも指定していない場合に限り、目標時刻の位置(夜=就寝、昼前=起床)から推測する。
  ただし帰宅時刻のような夜の時刻習慣を足すと推測は成り立たなくなるので、`--role` で明示するほうが確実。

## スケジュール(`--schedule`)

| 指定 | 意味 | 集計単位 |
| --- | --- | --- |
| `daily`(既定) | 毎日 | 1日 |
| `weekdays` / `weekend` | 平日 / 週末 | 1日(対象曜日のみ) |
| `mon,wed,fri` | 指定曜日 | 同上 |
| `weekly:3` | 週3回(曜日は問わない) | 1週(月曜はじまり) |

## 集計の決めごと

- **未記録の当日・未来は母数に入れない**(pending)。やる前から達成率が下がると記録が続かないため。
- **記録なしで過ぎた過去の対象日は未達**として母数に入る。記録漏れを「なかったこと」にしない。
- **登録日より前は対象外**。始める前の期間で率が下がらないようにする。遡って記録した日があればそこまで遡って集計する。
- ストリークは常に全履歴で数えるので、`--days` の取り方で値が変わらない。
- 就寝のような**夜の目標**(目標が20:00以降)では、`00:20` の記録を翌日 `24:20` として扱う。

## 記録ファイル

既定は `habits_data/habits.json`(`.gitignore` 済み、人が読める JSON)。
環境変数 `HABITS_FILE` か `--file` で差し替えられるので、同期フォルダに置いてもよい。
書き込みは一時ファイル + rename の原子的置換で、途中終了しても既存の記録を壊さない。

```bash
export HABITS_FILE=~/Dropbox/habits.json
python habit.py path      # 現在の保存先と件数を表示
```

## Claude Code に記録させる運用

自分で叩かず、毎朝7:30(JST)に Claude Code から声がかかる形でも使える。
夜だと就寝時刻が「予定」になり、そもそも寝ている日もあるため、朝に前日の実績をまとめて取る。

- 聞かれるのは「今朝の起床時刻 / 昨夜の就寝時刻 / 昨日の集中作業時間」の3つ。
- 記録先の日付は習慣ごとに振り分ける(ここを間違えると連続日数が壊れる):

  | 習慣 | 記録する日 |
  | --- | --- |
  | `wake`(起床) | 当日 |
  | `sleep`(就寝) | 前日(`00:30` は `24:30` として前夜に紐づく) |
  | `deep`(集中作業) | 前日 |

- 記録本体 `habits_data/habits.json` は **リポジトリで追跡する**(コンテナは毎回作り直されるため)。
  記録した日は Claude Code が `main` に1コミットする。書き出した CSV は追跡しない。
- セッションのコンテナは UTC 動作なので、日付は必ず JST で明示する:

  ```bash
  python habit.py done wake 06:12 --date "$(TZ=Asia/Tokyo date +%F)"
  ```

- 手元から直接叩く運用に戻したいときは、`HABITS_FILE` を自分のパスに向ければよい
  (リポジトリの記録とは別ファイルになる)。

## テスト

```bash
python -m tests.test_habits     # 標準ライブラリのみで完結(pytest でも可)
```
