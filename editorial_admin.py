# -*- coding: utf-8 -*-
"""Small admin utility for editorial.db.

Examples:
  python editorial_admin.py summary
  python editorial_admin.py validate
  python editorial_admin.py import-canon --version douban-snapshot --csv douban250.csv --dated 2026-06-14
  python editorial_admin.py import-movement-films --csv movement_films.csv --replace
"""

from __future__ import annotations

import argparse
import csv
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path


TABLES = (
    "canon_lists",
    "canon_versions",
    "canon_entries",
    "movements",
    "movement_films",
    "epochs",
)


def connect(db: str) -> sqlite3.Connection:
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    return con


def read_csv(path: str) -> list[dict[str, str]]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        return [{k: (v or "").strip() for k, v in row.items()} for row in csv.DictReader(f)]


def to_int(value: str, field: str, row_no: int, required: bool = True) -> int | None:
    value = (value or "").strip()
    if not value:
        if required:
            raise ValueError(f"row {row_no}: missing {field}")
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"row {row_no}: {field} must be an integer, got {value!r}") from exc


def cmd_summary(args: argparse.Namespace) -> None:
    con = connect(args.db)
    print(f"db: {Path(args.db).resolve()}")
    print("\ntables")
    for table in TABLES:
        count = con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        print(f"  {table}: {count}")

    print("\ncanon coverage")
    rows = con.execute(
        """
        SELECT v.id, v.expected_count, COUNT(e.title_zh) AS actual
        FROM canon_versions v
        LEFT JOIN canon_entries e ON e.version_id = v.id
        GROUP BY v.id
        ORDER BY v.list_id, v.id
        """
    ).fetchall()
    for row in rows:
        expected = row["expected_count"]
        gap = "" if expected is None else f", gap={expected - row['actual']}"
        print(f"  {row['id']}: actual={row['actual']}, expected={expected}{gap}")

    print("\nsmallest movement film sets")
    rows = con.execute(
        """
        SELECT m.id, m.name_zh, COUNT(f.film_title_zh) AS films
        FROM movements m
        LEFT JOIN movement_films f ON f.movement_id = m.id
        GROUP BY m.id
        ORDER BY films ASC, m.start_year ASC
        LIMIT 12
        """
    ).fetchall()
    for row in rows:
        print(f"  {row['id']} / {row['name_zh']}: {row['films']}")
    con.close()


def cmd_validate(args: argparse.Namespace) -> None:
    con = connect(args.db)
    checks: list[tuple[str, list[sqlite3.Row]]] = [
        (
            "orphan canon_entries.version_id",
            con.execute(
                """
                SELECT version_id, COUNT(*) AS count
                FROM canon_entries
                WHERE version_id NOT IN (SELECT id FROM canon_versions)
                GROUP BY version_id
                """
            ).fetchall(),
        ),
        (
            "duplicate canon entries within one version",
            con.execute(
                """
                SELECT version_id, title_zh, year, COUNT(*) AS count
                FROM canon_entries
                GROUP BY version_id, title_zh, year
                HAVING COUNT(*) > 1
                """
            ).fetchall(),
        ),
        (
            "orphan movement_films.movement_id",
            con.execute(
                """
                SELECT movement_id, COUNT(*) AS count
                FROM movement_films
                WHERE movement_id NOT IN (SELECT id FROM movements)
                GROUP BY movement_id
                """
            ).fetchall(),
        ),
        (
            "duplicate movement films within one movement",
            con.execute(
                """
                SELECT movement_id, film_title_zh, year, COUNT(*) AS count
                FROM movement_films
                GROUP BY movement_id, film_title_zh, year
                HAVING COUNT(*) > 1
                """
            ).fetchall(),
        ),
    ]

    failed = False
    for label, rows in checks:
        if not rows:
            print(f"OK: {label}")
            continue
        failed = True
        print(f"FAIL: {label}")
        for row in rows[:20]:
            print("  " + ", ".join(f"{k}={row[k]}" for k in row.keys()))
    con.close()
    if failed:
        raise SystemExit(1)


def cmd_add_canon_version(args: argparse.Namespace) -> None:
    con = connect(args.db)
    list_exists = con.execute("SELECT 1 FROM canon_lists WHERE id = ?", (args.list_id,)).fetchone()
    if not list_exists:
        if not args.list_name_zh:
            raise SystemExit("--list-name-zh is required when --list-id does not exist")
        con.execute(
            "INSERT INTO canon_lists(id, name_zh, name_en, publisher, type, notes) VALUES(?,?,?,?,?,?)",
            (args.list_id, args.list_name_zh, args.list_name_en, args.publisher, args.type, args.notes),
        )
    con.execute(
        """
        INSERT INTO canon_versions(id, list_id, label, dated, expected_count, notes)
        VALUES(?,?,?,?,?,?)
        """,
        (args.version, args.list_id, args.label, args.dated, args.expected_count, args.version_notes),
    )
    con.commit()
    con.close()
    print(f"added canon version: {args.version}")


def cmd_import_canon(args: argparse.Namespace) -> None:
    con = connect(args.db)
    version = con.execute("SELECT id FROM canon_versions WHERE id = ?", (args.version,)).fetchone()
    if not version:
        raise SystemExit(f"unknown canon version: {args.version}")

    rows = read_csv(args.csv)
    entries = []
    seen = set()
    for i, row in enumerate(rows, start=2):
        title = row.get("title_zh", "")
        if not title:
            raise ValueError(f"row {i}: missing title_zh")
        year = to_int(row.get("year", ""), "year", i)
        rank = to_int(row.get("rank", ""), "rank", i, required=False)
        key = (title, year)
        if key in seen:
            raise ValueError(f"row {i}: duplicate title/year in CSV: {title} ({year})")
        seen.add(key)
        entries.append((args.version, rank, title, year, 0, row.get("note", "")))

    with con:
        con.execute("DELETE FROM canon_entries WHERE version_id = ?", (args.version,))
        con.executemany("INSERT INTO canon_entries VALUES(?,?,?,?,?,?)", entries)
        if args.dated:
            con.execute("UPDATE canon_versions SET dated = ? WHERE id = ?", (args.dated, args.version))
    con.close()
    print(f"imported {len(entries)} canon entries into {args.version}")


def cmd_import_movement_films(args: argparse.Namespace) -> None:
    con = connect(args.db)
    rows = read_csv(args.csv)
    known = {row[0] for row in con.execute("SELECT id FROM movements")}
    by_movement: dict[str, list[tuple[str, int | None, str, int, str]]] = defaultdict(list)

    for i, row in enumerate(rows, start=2):
        movement_id = row.get("movement_id", "")
        if movement_id not in known:
            raise ValueError(f"row {i}: unknown movement_id {movement_id!r}")
        title = row.get("film_title_zh", "")
        if not title:
            raise ValueError(f"row {i}: missing film_title_zh")
        year = to_int(row.get("year", ""), "year", i)
        ord_value = to_int(row.get("ord", ""), "ord", i, required=False)
        by_movement[movement_id].append((movement_id, ord_value, title, year, row.get("note", "")))

    existing_next_ord = {}
    for movement_id in by_movement:
        max_ord = con.execute(
            "SELECT COALESCE(MAX(ord), 0) FROM movement_films WHERE movement_id = ?", (movement_id,)
        ).fetchone()[0]
        existing_next_ord[movement_id] = 1 if args.replace else max_ord + 1

    entries = []
    for movement_id, items in by_movement.items():
        for item in items:
            ord_value = item[1]
            if ord_value is None:
                ord_value = existing_next_ord[movement_id]
                existing_next_ord[movement_id] += 1
            entries.append((movement_id, ord_value, item[2], item[3], item[4]))

    dupes = [key for key, count in Counter((e[0], e[2], e[3]) for e in entries).items() if count > 1]
    if dupes:
        raise ValueError(f"duplicate movement/title/year in CSV: {dupes[:5]}")

    with con:
        if args.replace:
            for movement_id in by_movement:
                con.execute("DELETE FROM movement_films WHERE movement_id = ?", (movement_id,))
        con.executemany("INSERT INTO movement_films VALUES(?,?,?,?,?)", entries)
    con.close()
    print(f"imported {len(entries)} movement films")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="editorial.db")
    sub = parser.add_subparsers(dest="command", required=True)

    summary = sub.add_parser("summary")
    summary.set_defaults(func=cmd_summary)

    validate = sub.add_parser("validate")
    validate.set_defaults(func=cmd_validate)

    add_version = sub.add_parser("add-canon-version")
    add_version.add_argument("--list-id", required=True)
    add_version.add_argument("--version", required=True)
    add_version.add_argument("--label", required=True)
    add_version.add_argument("--dated", default="")
    add_version.add_argument("--expected-count", type=int)
    add_version.add_argument("--version-notes", default="")
    add_version.add_argument("--list-name-zh", default="")
    add_version.add_argument("--list-name-en", default="")
    add_version.add_argument("--publisher", default="")
    add_version.add_argument("--type", default="")
    add_version.add_argument("--notes", default="")
    add_version.set_defaults(func=cmd_add_canon_version)

    canon = sub.add_parser("import-canon")
    canon.add_argument("--version", required=True)
    canon.add_argument("--csv", required=True)
    canon.add_argument("--dated", default="")
    canon.set_defaults(func=cmd_import_canon)

    movement_films = sub.add_parser("import-movement-films")
    movement_films.add_argument("--csv", required=True)
    movement_films.add_argument("--replace", action="store_true")
    movement_films.set_defaults(func=cmd_import_movement_films)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
