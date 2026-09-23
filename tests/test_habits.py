"""
test_habits.py — 習慣トラッカー(habits/)のユニットテスト。

test_smoke.py と同じく、pytest なしでも `python -m tests.test_habits` で走る
assert ベースの関数群にしている。外部依存なし(標準ライブラリのみ)。
"""
from __future__ import annotations

import datetime as dt
import io
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from habits import stats
from habits.cli import main
from habits.models import Entry, Habit, HabitError, Schedule, format_clock, parse_clock, parse_date
from habits.storage import Tracker

TODAY = dt.date(2026, 9, 21)  # 月曜


def _habit(**kw) -> Habit:
    base = dict(id="h", name="習慣", kind="check", schedule=Schedule("daily"),
                created=TODAY - dt.timedelta(days=30))
    base.update(kw)
    return Habit(**base)


def _entries(habit: Habit, pairs: list[tuple[int, float]]) -> list[Entry]:
    """(何日前, 値) の列から記録を作る。"""
    return [Entry(habit.id, TODAY - dt.timedelta(days=d), v) for d, v in pairs]


# ---------------------------------------------------------------------------
# 値の解釈
# ---------------------------------------------------------------------------
def test_clock_parsing_roundtrip():
    """時刻は 24時以降も含めて分に変換され、同じ表記に戻ること。"""
    assert parse_clock("06:05") == 365
    assert parse_clock("6:05") == 365
    assert parse_clock("2510") == 25 * 60 + 10
    assert format_clock(parse_clock("24:45")) == "24:45"
    for bad in ("25:99", "abc", "6"):
        try:
            parse_clock(bad)
        except HabitError:
            continue
        raise AssertionError(f"不正な時刻が通ってしまった: {bad}")


def test_night_target_wraps_past_midnight():
    """就寝(夜の目標)では 00:20 の記録が翌日 24:20 として扱われ、未達になること。"""
    sleep = _habit(kind="time", target=parse_clock("24:00"), cmp="<=")
    assert sleep.parse_value("00:20") == parse_clock("24:20")
    assert not sleep.achieved(sleep.parse_value("00:20"))
    assert sleep.achieved(sleep.parse_value("23:40"))
    # 朝の目標では日またぎ補正をしない(06:30 目標に対する 00:20 は達成扱い)
    wake = _habit(kind="time", target=parse_clock("06:30"), cmp="<=")
    assert wake.parse_value("00:20") == 20


def test_comparator_direction():
    """>= と <= が目標の向きどおりに判定されること。"""
    more = _habit(kind="number", target=2.0, cmp=">=", unit="h")
    assert more.achieved(2.0) and more.achieved(3.5) and not more.achieved(1.9)
    less = _habit(kind="number", target=2.0, cmp="<=")
    assert less.achieved(2.0) and less.achieved(0.5) and not less.achieved(2.1)


def test_check_habit_needs_no_value():
    """check 習慣は値なしで達成、明示的に 0 を渡すと未達になること。"""
    h = _habit()
    assert h.achieved(h.parse_value(None))
    assert not h.achieved(h.parse_value("0"))


def test_number_habit_requires_target_and_value():
    """数値習慣は目標なしで作れず、値なしでは記録できないこと。"""
    try:
        _habit(kind="number", target=None)
    except HabitError:
        pass
    else:
        raise AssertionError("目標なしの数値習慣が作れてしまった")
    try:
        _habit(kind="number", target=1.0).parse_value(None)
    except HabitError:
        return
    raise AssertionError("値なしの記録が通ってしまった")


def test_parse_date_relative():
    """相対日付(-1 / today)が解釈できること。"""
    assert parse_date("-1", today=TODAY) == TODAY - dt.timedelta(days=1)
    assert parse_date("today", today=TODAY) == TODAY
    assert parse_date("2026-01-05", today=TODAY) == dt.date(2026, 1, 5)


def test_schedule_parsing():
    """スケジュール指定が曜日集合・週N回に落ちること。"""
    assert Schedule.parse("weekdays").days == (0, 1, 2, 3, 4)
    assert Schedule.parse("平日").days == (0, 1, 2, 3, 4)
    assert Schedule.parse("mon,wed,fri").days == (0, 2, 4)
    assert Schedule.parse("weekly:3").times == 3
    assert not Schedule.parse("mon,wed,fri").is_scheduled(TODAY + dt.timedelta(days=1))  # 火
    try:
        Schedule.parse("someday")
    except HabitError:
        return
    raise AssertionError("不正なスケジュールが通ってしまった")


# ---------------------------------------------------------------------------
# 集計
# ---------------------------------------------------------------------------
def test_streak_counts_back_from_today():
    """直近4日連続で達成していれば streak=4 になること。"""
    h = _habit()
    entries = _entries(h, [(d, 1.0) for d in (1, 2, 3, 4)])
    s = stats.summarize(h, entries, TODAY - dt.timedelta(days=9), TODAY, today=TODAY)
    assert s.streak == 4


def test_today_unrecorded_does_not_break_streak():
    """今日ぶんが未記録でも、昨日までの連続は維持されること(母数にも入らない)。"""
    h = _habit()
    entries = _entries(h, [(d, 1.0) for d in (1, 2, 3)])
    s = stats.summarize(h, entries, TODAY - dt.timedelta(days=3), TODAY, today=TODAY)
    assert s.streak == 3
    assert s.pending == 1 and s.judged == 3 and s.rate == 1.0


def test_explicit_miss_breaks_streak():
    """未達を記録した日で連続が切れ、最長ストリークは履歴に残ること。"""
    h = _habit()
    entries = _entries(h, [(5, 1.0), (4, 1.0), (3, 1.0), (2, 0.0), (1, 1.0)])
    s = stats.summarize(h, entries, TODAY - dt.timedelta(days=9), TODAY, today=TODAY)
    assert s.streak == 1
    assert s.best_streak == 3


def test_skipped_day_counts_as_missed():
    """記録がないまま過ぎた日は未達として母数に入ること。"""
    h = _habit()
    entries = _entries(h, [(3, 1.0), (1, 1.0)])  # 2日前は記録なし
    s = stats.summarize(h, entries, TODAY - dt.timedelta(days=3), TODAY - dt.timedelta(days=1),
                        today=TODAY)
    assert (s.done, s.missed) == (2, 1)
    assert abs(s.rate - 2 / 3) < 1e-9


def test_days_before_start_are_excluded():
    """登録日より前の日付は母数に入らないこと(始める前で率が下がらない)。"""
    h = _habit(created=TODAY - dt.timedelta(days=2))
    entries = _entries(h, [(2, 1.0), (1, 1.0)])
    s = stats.summarize(h, entries, TODAY - dt.timedelta(days=30), TODAY, today=TODAY)
    assert s.judged == 2 and s.rate == 1.0


def test_weekday_schedule_ignores_offdays():
    """平日のみの習慣では土日が母数に入らないこと。"""
    h = _habit(schedule=Schedule.parse("weekdays"), created=TODAY - dt.timedelta(days=7))
    s = stats.summarize(h, [], TODAY - dt.timedelta(days=7), TODAY - dt.timedelta(days=1),
                        today=TODAY)
    assert s.judged == 5  # 直近7日ぶんのうち平日5日


def test_weekly_schedule_counts_by_week():
    """週3回の習慣は、週内3回で達成・2回で未達と判定されること。"""
    h = _habit(schedule=Schedule.parse("weekly:3"), created=TODAY - dt.timedelta(days=21))
    # 先週(月曜=TODAY-7)に3回、その前の週に2回
    entries = _entries(h, [(7, 1.0), (6, 1.0), (5, 1.0), (14, 1.0), (13, 1.0)])
    s = stats.summarize(h, entries, TODAY - dt.timedelta(days=20), TODAY, today=TODAY)
    last_week = [p for p in s.periods if p.start == TODAY - dt.timedelta(days=7)][0]
    prev_week = [p for p in s.periods if p.start == TODAY - dt.timedelta(days=14)][0]
    assert last_week.status == stats.DONE and last_week.count == 3
    assert prev_week.status == stats.MISSED
    assert s.streak == 1  # 今週はまだ pending、先週の1週ぶんが連続


def test_calendar_line_marks():
    """カレンダー行が 達成/未達/未記録 を記号で並べること。"""
    h = _habit(created=TODAY - dt.timedelta(days=2))
    entries = _entries(h, [(2, 1.0), (1, 0.0)])
    line = stats.calendar_line(h, entries, TODAY - dt.timedelta(days=3), TODAY, today=TODAY)
    assert line == "・■×□"  # 登録前・達成・未達・今日未記録


# ---------------------------------------------------------------------------
# 睡眠時間(就寝・起床から導出)
# ---------------------------------------------------------------------------
def _sleep_pair(with_roles: bool = False) -> tuple[Habit, Habit]:
    bed = _habit(id="sleep", name="24時までに寝る", kind="time",
                 target=parse_clock("24:00"), cmp="<=",
                 role="bedtime" if with_roles else "")
    wake = _habit(id="wake", name="6時30分までに起きる", kind="time",
                  target=parse_clock("06:30"), cmp="<=",
                  role="wakeup" if with_roles else "")
    return bed, wake


def test_sleep_pair_detected_from_targets():
    """役割の指定が無い場合は、目標時刻の位置から (就寝, 起床) を推定すること。"""
    bed, wake = _sleep_pair()
    deep = _habit(id="deep", kind="number", target=2.0)
    assert stats.detect_sleep_pair([wake, deep, bed]) == (bed, wake)
    # 起床候補が2つあると推定しない(昼寝などを時刻習慣にしている場合)
    nap = _habit(id="nap", kind="time", target=parse_clock("11:00"), cmp="<=")
    assert stats.detect_sleep_pair([wake, bed, nap]) is None
    # 片方しかなければ推定しない
    assert stats.detect_sleep_pair([wake]) is None


def test_explicit_roles_survive_other_time_habits():
    """帰宅時刻のような夜の時刻習慣が増えても、役割の指定があれば組が壊れないこと。"""
    bed, wake = _sleep_pair(with_roles=True)
    home = _habit(id="home", name="20時までに帰宅", kind="time",
                  target=parse_clock("20:00"), cmp="<=")
    # 役割なしの推測ではこの構成は判定不能になる
    assert stats.detect_sleep_pair(_sleep_pair() + (home,)) is None
    # 役割を明示していれば影響を受けない
    assert stats.detect_sleep_pair([wake, home, bed]) == (bed, wake)


def test_role_only_on_time_habits():
    """時刻以外の習慣に役割を付けるとエラーになること。"""
    try:
        _habit(id="deep", kind="number", target=2.0, role="bedtime")
    except HabitError:
        return
    raise AssertionError("数値習慣に役割が付いてしまった")


def test_sleep_duration_spans_midnight():
    """就寝(前日)と起床(翌日)から睡眠時間が出ること。24時以降の就寝も扱えること。"""
    bed, wake = _sleep_pair()
    night = TODAY - dt.timedelta(days=1)
    entries = [
        Entry(bed.id, night, parse_clock("23:00")),
        Entry(wake.id, TODAY, parse_clock("06:00")),
        Entry(bed.id, night - dt.timedelta(days=1), parse_clock("24:10")),
        Entry(wake.id, night, parse_clock("06:20")),
    ]
    got = dict(stats.sleep_nights(bed, wake, entries, night - dt.timedelta(days=1), TODAY))
    assert got[night] == 7 * 60                      # 23:00 → 06:00
    assert got[night - dt.timedelta(days=1)] == 370  # 24:10 → 06:20 = 6時間10分
    assert stats.format_duration(370) == "6時間10分"


def test_sleep_duration_skips_incomplete_nights():
    """就寝か起床の片方しかない夜は集計に含めないこと。"""
    bed, wake = _sleep_pair()
    night = TODAY - dt.timedelta(days=1)
    entries = [Entry(bed.id, night, parse_clock("23:00"))]  # 翌朝の起床が未記録
    assert stats.sleep_nights(bed, wake, entries, night, TODAY) == []


# ---------------------------------------------------------------------------
# 保存
# ---------------------------------------------------------------------------
def test_storage_roundtrip():
    """保存→読み込みで習慣定義と記録が保たれること。"""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "habits.json"
        t = Tracker(path=path)
        h = t.add_habit(_habit(id="wake", kind="time", target=parse_clock("06:30"), cmp="<="))
        t.record(h, TODAY, parse_clock("06:12"), note="早起き")
        t.save()

        loaded = Tracker.load(path)
        assert len(loaded.habits) == 1 and len(loaded.entries) == 1
        h2 = loaded.get("wake")
        assert h2.kind == "time" and h2.cmp == "<=" and h2.target == parse_clock("06:30")
        e = loaded.entry_on("wake", TODAY)
        assert e is not None and h2.format_value(e.value) == "06:12" and e.note == "早起き"


def test_record_overwrites_same_day():
    """同じ日の再記録は追加ではなく上書きになること。"""
    t = Tracker(path=Path(tempfile.gettempdir()) / "unused.json")
    h = t.add_habit(_habit(id="deep", kind="number", target=2.0, unit="h"))
    t.record(h, TODAY, 1.0)
    entry, overwritten = t.record(h, TODAY, 3.0)
    assert overwritten and entry.value == 3.0 and len(t.entries) == 1


def test_get_habit_by_prefix_and_missing():
    """ID の前方一致で解決でき、曖昧・不在はエラーになること。"""
    t = Tracker(path=Path(tempfile.gettempdir()) / "unused.json")
    t.add_habit(_habit(id="wake"))
    t.add_habit(_habit(id="walk"))
    t.add_habit(_habit(id="deep"))
    assert t.get("de").id == "deep"
    for bad in ("wa", "nope"):
        try:
            t.get(bad)
        except HabitError:
            continue
        raise AssertionError(f"解決してはいけない ID が通った: {bad}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _cli(path: Path, *argv: str) -> tuple[int, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = main(["--file", str(path), *argv])
    return code, buf.getvalue()


def test_cli_end_to_end():
    """add → done → today → report → undo → export が通ること。"""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "habits.json"
        assert _cli(path, "add", "wake", "--name", "6時に起きる",
                    "--kind", "time", "--target", "06:30", "--cmp", "<=")[0] == 0
        assert _cli(path, "add", "gym", "--schedule", "weekly:3")[0] == 0
        assert _cli(path, "done", "wake", "06:12")[0] == 0
        assert _cli(path, "done", "gym")[0] == 0

        code, out = _cli(path, "today")
        assert code == 0 and "6時に起きる" in out and "06:12" in out

        code, out = _cli(path, "report", "--days", "7")
        assert code == 0 and "達成率" in out

        code, out = _cli(path, "log", "wake")
        assert code == 0 and "06:12" in out

        assert _cli(path, "undo", "wake")[0] == 0
        assert Tracker.load(path).entry_on("wake", dt.date.today()) is None

        out_csv = Path(tmp) / "out.csv"
        assert _cli(path, "export", "--out", str(out_csv))[0] == 0
        assert out_csv.exists() and "gym" in out_csv.read_text(encoding="utf-8-sig")


def test_cli_reports_user_errors():
    """入力ミスは例外ではなく終了コード1とメッセージで返ること。"""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "habits.json"
        _cli(path, "add", "deep", "--kind", "number", "--target", "2", "--unit", "h")
        assert _cli(path, "done", "deep")[0] == 1          # 値なし
        assert _cli(path, "done", "nope", "1")[0] == 1     # 存在しない習慣
        assert _cli(path, "add", "deep")[0] == 1           # ID 重複
        assert _cli(path, "remove", "deep")[0] == 1        # --yes なし
        assert _cli(path, "remove", "deep", "--yes")[0] == 0


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  PASS {fn.__name__}")
    print(f"\n{len(fns)} tests passed.")


if __name__ == "__main__":
    _run_all()
