"""
cli.py — 習慣トラッカーのコマンドライン。

    python habit.py add wake --name "6時に起きる" --kind time --target 06:30 --cmp "<="
    python habit.py done wake 06:12
    python habit.py today
    python habit.py report --days 30

表示は端末前提なので、全角を含む日本語でも列が崩れないよう幅を自前で数える。
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import sys
import unicodedata
from pathlib import Path

from .models import Habit, HabitError, Schedule, WEEKDAY_JP, parse_clock, parse_date
from .stats import (MARKS, HabitStats, calendar_line, detect_sleep_pair, format_duration,
                    sleep_nights, summarize)
from .storage import Tracker

CHECKBOX = {"done": "[■]", "missed": "[×]", "pending": "[ ]"}


# ---------------------------------------------------------------------------
# 表示ヘルパ
# ---------------------------------------------------------------------------
def width(text: str) -> int:
    """全角を 2 と数えた表示幅。"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def pad(text: str, size: int) -> str:
    return text + " " * max(0, size - width(text))


def render_table(header: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return ""
    sizes = [max(width(h), *(width(r[i]) for r in rows)) for i, h in enumerate(header)]
    lines = ["  ".join(pad(h, s) for h, s in zip(header, sizes)).rstrip()]
    lines.append("  ".join("-" * s for s in sizes))
    for row in rows:
        lines.append("  ".join(pad(c, s) for c, s in zip(row, sizes)).rstrip())
    return "\n".join(lines)


def date_label(day: dt.date) -> str:
    return f"{day.isoformat()}({WEEKDAY_JP[day.weekday()]})"


def offday_note(habit: Habit, day: dt.date) -> str:
    """対象外の曜日に記録したときの注記(記録自体は残すが集計には入らない)。"""
    if habit.schedule.is_weekly or habit.schedule.is_scheduled(day):
        return ""
    return f"(※ {habit.schedule.label()}の習慣なので、この日は集計対象外)"


def rate_label(stats: HabitStats) -> str:
    rate = stats.rate
    return "-" if rate is None else f"{rate * 100:.0f}%"


def average_label(stats: HabitStats) -> str:
    avg = stats.average
    if avg is None:
        return "-"
    return stats.habit.format_value(round(avg, 2) if stats.habit.kind == "number" else avg)


def last_night_sleep(tracker: Tracker, day: dt.date) -> str:
    """その日の朝までの睡眠時間(前夜の就寝→当日の起床)。取れなければ空文字。"""
    pair = detect_sleep_pair(tracker.habits)
    if pair is None:
        return ""
    night = day - dt.timedelta(days=1)
    nights = sleep_nights(pair[0], pair[1], tracker.entries, night, night)
    return format_duration(nights[0][1]) if nights else ""


def sleep_section(tracker: Tracker, since: dt.date, until: dt.date) -> str:
    """睡眠時間の集計。就寝・起床が揃っている夜だけを対象にする。

    睡眠は長さより**ばらつき**が効くので、平均と一緒に最短・最長も出す。
    """
    pair = detect_sleep_pair(tracker.habits)
    if pair is None:
        return ""
    bedtime, wakeup = pair
    nights = sleep_nights(bedtime, wakeup, tracker.entries, since - dt.timedelta(days=1), until)
    if not nights:
        return ""
    values = [m for _, m in nights]
    avg = sum(values) / len(values)
    enough = sum(1 for m in values if m >= 7 * 60)
    lines = [
        "",
        f"睡眠時間({bedtime.name} → {wakeup.name} / {len(nights)}泊)",
        f"  平均 {format_duration(avg)} / 最短 {format_duration(min(values))}"
        f" / 最長 {format_duration(max(values))}",
        f"  7時間以上の夜: {enough}/{len(nights)}(ばらつき {format_duration(max(values) - min(values))})",
    ]
    for night, minutes in nights[-7:]:
        lines.append(f"  {date_label(night)}夜  {format_duration(minutes)}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# 各コマンド
# ---------------------------------------------------------------------------
def cmd_add(tracker: Tracker, args: argparse.Namespace) -> int:
    kind = args.kind
    target = None
    if args.target is not None:
        target = parse_clock(args.target) if kind == "time" else float(args.target)
    if kind != "check" and target is None:
        raise HabitError(f"kind={kind} には --target が必要(例: --target 2.0 / --target 06:30)")
    habit = Habit(
        id=args.id,
        name=args.name or args.id,
        kind=kind,
        schedule=Schedule.parse(args.schedule),
        cmp=args.cmp,
        target=target,
        unit=args.unit,
        role=args.role or "",
        created=parse_date(args.since) if args.since else dt.date.today(),
    )
    tracker.add_habit(habit)
    tracker.save()
    print(f"追加: {habit.id} 「{habit.name}」 / {habit.schedule.label()} / 目標 {habit.target_label()}")
    return 0


def cmd_list(tracker: Tracker, args: argparse.Namespace) -> int:
    habits = tracker.habits if args.all else tracker.active_habits()
    if not habits:
        print("習慣がまだない。`habit.py add <id> --name ...` で登録する。")
        return 0
    rows = [
        [h.id, h.name, {"check": "実行", "number": "数値", "time": "時刻"}[h.kind],
         h.schedule.label(), h.target_label(),
         str(len(tracker.entries_of(h.id))), "休止" if h.archived else ""]
        for h in habits
    ]
    print(render_table(["ID", "習慣", "種別", "頻度", "目標", "記録数", "状態"], rows))
    return 0


def cmd_done(tracker: Tracker, args: argparse.Namespace) -> int:
    habit = tracker.get(args.id)
    day = parse_date(args.date)
    value = habit.parse_value(args.value)
    entry, overwritten = tracker.record(habit, day, value, args.note or "")
    tracker.save()
    mark = "達成" if habit.achieved(entry.value) else "未達"
    verb = "更新" if overwritten else "記録"
    print(f"{verb}: {date_label(day)} {habit.name} = {habit.format_value(entry.value)} → {mark}"
          f"{offday_note(habit, day)}")
    stats = summarize(habit, tracker.entries, day - dt.timedelta(days=30), day)
    print(f"  連続 {stats.streak}{stats.unit_label} / 最長 {stats.best_streak}{stats.unit_label}")
    return 0


def cmd_miss(tracker: Tracker, args: argparse.Namespace) -> int:
    habit = tracker.get(args.id)
    if habit.kind != "check":
        raise HabitError(f"{habit.id} は数値/時刻の習慣。実測値を `done {habit.id} <値>` で記録する")
    day = parse_date(args.date)
    tracker.record(habit, day, 0.0, args.note or "")
    tracker.save()
    print(f"記録: {date_label(day)} {habit.name} = 未達(連続は途切れる){offday_note(habit, day)}")
    return 0


def cmd_undo(tracker: Tracker, args: argparse.Namespace) -> int:
    habit = tracker.get(args.id)
    day = parse_date(args.date)
    entry = tracker.unrecord(habit, day)
    tracker.save()
    print(f"取消: {date_label(day)} {habit.name}({habit.format_value(entry.value)})の記録を削除した")
    return 0


def cmd_today(tracker: Tracker, args: argparse.Namespace) -> int:
    day = parse_date(args.date)
    habits = [h for h in tracker.active_habits() if h.schedule.is_scheduled(day)]
    if not habits:
        print(f"{date_label(day)}: 対象の習慣なし")
        return 0
    slept = last_night_sleep(tracker, day)
    print(f"{date_label(day)} の習慣 {len(habits)}件" + (f" / 前夜の睡眠 {slept}" if slept else ""))
    rows = []
    for habit in habits:
        entry = tracker.entry_on(habit.id, day)
        if entry is None:
            box, value = CHECKBOX["pending"], "-"
        else:
            box = CHECKBOX["done" if habit.achieved(entry.value) else "missed"]
            value = habit.format_value(entry.value)
        stats = summarize(habit, tracker.entries, day - dt.timedelta(days=29), day, today=day)
        rows.append([
            box, habit.id, habit.name, f"目標 {habit.target_label()}", value,
            f"連続 {stats.streak}{stats.unit_label}", f"直近30日 {rate_label(stats)}",
        ])
    print(render_table(["", "ID", "習慣", "目標", "実績", "継続", "達成率"], rows))
    remaining = sum(1 for h in habits if tracker.entry_on(h.id, day) is None)
    if remaining:
        print(f"\n未記録 {remaining}件 — 例: python habit.py done {habits[0].id}")
    return 0


def cmd_report(tracker: Tracker, args: argparse.Namespace) -> int:
    today = dt.date.today()
    until = parse_date(args.until) if args.until else today
    since = parse_date(args.since) if args.since else until - dt.timedelta(days=args.days - 1)
    if since > until:
        raise HabitError("--since が --until より後になっている")
    habits = tracker.habits if args.all else tracker.active_habits()
    if args.habit:
        habits = [tracker.get(args.habit)]
    if not habits:
        print("集計対象の習慣がない。")
        return 0

    print(f"集計期間: {since.isoformat()} 〜 {until.isoformat()}({(until - since).days + 1}日)")
    rows = []
    summaries = []
    for habit in habits:
        stats = summarize(habit, tracker.entries, since, until, today=today)
        summaries.append(stats)
        rows.append([
            habit.id, habit.name, habit.schedule.label(),
            f"{stats.done}/{stats.judged}", rate_label(stats),
            f"{stats.streak}{stats.unit_label}", f"{stats.best_streak}{stats.unit_label}",
            average_label(stats), f"目標 {habit.target_label()}",
        ])
    print()
    print(render_table(
        ["ID", "習慣", "頻度", "達成/対象", "達成率", "連続", "最長", "平均", "目標"], rows))

    span = min(args.calendar, (until - since).days + 1)
    if span > 0:
        cal_since = until - dt.timedelta(days=span - 1)
        print(f"\n直近{span}日 [{MARKS['done']}達成 {MARKS['missed']}未達 {MARKS['pending']}未記録 ・対象外]")
        print(f"  {' ' * 0}{cal_since.isoformat()} → {until.isoformat()}")
        for habit in habits:
            line = calendar_line(habit, tracker.entries, cal_since, until, today=today)
            print(f"  {pad(habit.id, max(width(h.id) for h in habits))}  {line}")

    print(sleep_section(tracker, since, until), end="")

    judged = sum(s.judged for s in summaries)
    done = sum(s.done for s in summaries)
    if judged:
        print(f"\n全体: {done}/{judged}(達成率 {done / judged * 100:.0f}%)")
    return 0


def cmd_log(tracker: Tracker, args: argparse.Namespace) -> int:
    habit = tracker.get(args.id)
    entries = tracker.entries_of(habit.id)[-args.limit:]
    if not entries:
        print(f"{habit.name}: 記録なし")
        return 0
    rows = [
        [date_label(e.date), habit.format_value(e.value),
         "達成" if habit.achieved(e.value) else "未達", e.note]
        for e in reversed(entries)
    ]
    print(f"{habit.id} 「{habit.name}」 目標 {habit.target_label()}")
    print(render_table(["日付", "実績", "判定", "メモ"], rows))
    return 0


def cmd_role(tracker: Tracker, args: argparse.Namespace) -> int:
    habit = tracker.get(args.id)
    role = "" if args.role == "none" else args.role
    if role and habit.kind != "time":
        raise HabitError(f"{habit.id} は時刻習慣ではないので就寝/起床の役割を付けられない")
    habit.role = role
    tracker.save()
    label = {"bedtime": "就寝", "wakeup": "起床", "none": "なし"}[args.role]
    print(f"役割: {habit.id}「{habit.name}」→ {label}")
    return 0


def cmd_archive(tracker: Tracker, args: argparse.Namespace) -> int:
    habit = tracker.set_archived(args.id, not args.unarchive)
    tracker.save()
    print(f"{'再開' if args.unarchive else '休止'}: {habit.id}「{habit.name}」")
    return 0


def cmd_remove(tracker: Tracker, args: argparse.Namespace) -> int:
    habit = tracker.get(args.id)
    count = len(tracker.entries_of(habit.id))
    if not args.yes:
        raise HabitError(
            f"{habit.id}「{habit.name}」と記録{count}件を削除する。実行するなら --yes を付ける"
            "(記録を残して止めるだけなら archive)"
        )
    tracker.remove_habit(habit.id)
    tracker.save()
    print(f"削除: {habit.id}(記録{count}件も削除)")
    return 0


def cmd_export(tracker: Tracker, args: argparse.Namespace) -> int:
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    habits = {h.id: h for h in tracker.habits}
    with out.open("w", encoding="utf-8-sig", newline="") as fp:
        writer = csv.writer(fp)
        writer.writerow(["date", "habit_id", "habit", "value", "raw_value", "achieved", "note"])
        for e in sorted(tracker.entries, key=lambda e: (e.date, e.habit_id)):
            habit = habits.get(e.habit_id)
            if habit is None:
                continue
            writer.writerow([
                e.date.isoformat(), habit.id, habit.name,
                habit.format_value(e.value), e.value,
                int(habit.achieved(e.value)), e.note,
            ])
    print(f"書き出し: {out}({len(tracker.entries)}件)")
    return 0


def cmd_path(tracker: Tracker, args: argparse.Namespace) -> int:
    print(tracker.path)
    print(f"存在: {'あり' if tracker.path.exists() else 'なし'} / "
          f"習慣 {len(tracker.habits)}件 / 記録 {len(tracker.entries)}件")
    return 0


# ---------------------------------------------------------------------------
# パーサ
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="habit.py",
        description="生活を律するための習慣トラッカー(記録 → 継続率・ストリークの可視化)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "例:\n"
            "  habit.py add wake --name '6時に起きる' --kind time --target 06:30 --cmp '<='\n"
            "  habit.py add deep --name '集中作業' --kind number --target 2 --unit h\n"
            "  habit.py add gym  --name '筋トレ' --schedule weekly:3\n"
            "  habit.py done wake 06:12 && habit.py done deep 2.5 && habit.py done gym\n"
            "  habit.py today / habit.py report --days 30\n"
        ),
    )
    parser.add_argument("--file", help="記録ファイルのパス(既定は環境変数 HABITS_FILE / habits_data/habits.json)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("add", help="習慣を登録する")
    p.add_argument("id", help="短い識別子(例 wake)")
    p.add_argument("--name", help="表示名(既定は id)")
    p.add_argument("--kind", choices=["check", "number", "time"], default="check",
                   help="check=やったか / number=数値目標 / time=時刻目標")
    p.add_argument("--schedule", default="daily",
                   help="daily / weekdays / weekend / mon,wed,fri / weekly:3")
    p.add_argument("--target", help="目標値(number は数値、time は HH:MM)")
    p.add_argument("--cmp", choices=[">=", "<="], default=">=", help="目標の向き(既定 >=)")
    p.add_argument("--unit", default="", help="単位(例 h, 回, ページ)")
    p.add_argument("--role", choices=["bedtime", "wakeup"],
                   help="時刻習慣の役割。就寝と起床に付けると睡眠時間を自動算出する")
    p.add_argument("--since", help="開始日(既定は今日)")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("list", help="登録済みの習慣を一覧する")
    p.add_argument("--all", action="store_true", help="休止中も含める")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("done", help="実績を記録する(同じ日への再記録は上書き)")
    p.add_argument("id")
    p.add_argument("value", nargs="?", help="number/time は必須(例 2.5 / 06:12)")
    p.add_argument("--value", dest="value_opt", help="位置引数の代わりに使う")
    p.add_argument("--date", help="記録日(既定は今日 / 2026-09-21 / -1)")
    p.add_argument("--note", help="メモ")
    p.set_defaults(func=cmd_done)

    p = sub.add_parser("miss", help="できなかった日を明示的に記録する(check のみ)")
    p.add_argument("id")
    p.add_argument("--date")
    p.add_argument("--note")
    p.set_defaults(func=cmd_miss)

    p = sub.add_parser("undo", help="その日の記録を取り消す")
    p.add_argument("id")
    p.add_argument("--date")
    p.set_defaults(func=cmd_undo)

    p = sub.add_parser("today", help="今日やることと状態を表示する")
    p.add_argument("--date", help="別の日を見る")
    p.set_defaults(func=cmd_today)

    p = sub.add_parser("report", help="継続率・ストリークを集計する")
    p.add_argument("--days", type=int, default=30, help="直近N日(既定 30)")
    p.add_argument("--since", help="集計開始日")
    p.add_argument("--until", help="集計終了日(既定 今日)")
    p.add_argument("--habit", help="1つの習慣だけ集計する")
    p.add_argument("--all", action="store_true", help="休止中も含める")
    p.add_argument("--calendar", type=int, default=28, help="カレンダー表示の日数(0 で非表示)")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("log", help="1つの習慣の記録を新しい順に見る")
    p.add_argument("id")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_log)

    p = sub.add_parser("role", help="時刻習慣に就寝/起床の役割を付ける(睡眠時間の算出用)")
    p.add_argument("id")
    p.add_argument("role", choices=["bedtime", "wakeup", "none"])
    p.set_defaults(func=cmd_role)

    p = sub.add_parser("archive", help="習慣を休止する(記録は残す)")
    p.add_argument("id")
    p.add_argument("--unarchive", action="store_true", help="休止を解除する")
    p.set_defaults(func=cmd_archive)

    p = sub.add_parser("remove", help="習慣と記録を削除する")
    p.add_argument("id")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_remove)

    p = sub.add_parser("export", help="記録を CSV に書き出す")
    p.add_argument("--out", default="habits_data/habits.csv")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("path", help="記録ファイルの場所を表示する")
    p.set_defaults(func=cmd_path)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "value_opt", None) is not None:
        args.value = args.value_opt
    tracker = Tracker.load(Path(args.file) if args.file else None)
    try:
        return args.func(tracker, args)
    except HabitError as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
