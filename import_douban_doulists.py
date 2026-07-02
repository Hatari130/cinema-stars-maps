"""Import Douban doulists into editorial.db canon versions.

Usage:
  python import_douban_doulists.py
  python import_douban_doulists.py --db editorial.db
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import time
from dataclasses import dataclass
from typing import Iterable

import requests
from bs4 import BeautifulSoup


DOULISTS = {
    "douban-doulist-105743": {
        "doulist_id": 105743,
        "label": "《电影手册》世上最美的100部电影",
        "dated": "2007-12",
        "expected_count": 100,
        "notes": "Imported from Douban doulist 105743.",
    },
    "douban-doulist-1518184": {
        "doulist_id": 1518184,
        "label": "IMDB TOP 250 UPDATE 20260627",
        "dated": "2026-06-27",
        "expected_count": 248,
        "notes": "Imported from Douban doulist 1518184.",
    },
    "douban-doulist-132137": {
        "doulist_id": 132137,
        "label": "美国著名影评人 Rosenbaum 最喜欢的100电影（世界篇）",
        "dated": "2008-03",
        "expected_count": 100,
        "notes": "Imported from Douban doulist 132137.",
    },
    "douban-doulist-189971": {
        "doulist_id": 189971,
        "label": "Roger Ebert推荐的100部电影",
        "dated": "2008-03",
        "expected_count": 103,
        "notes": "Imported from Douban doulist 189971.",
    },
    "douban-doulist-24141": {
        "doulist_id": 24141,
        "label": "戛纳电影节金棕榈影片",
        "dated": "2010-05",
        "expected_count": 77,
        "notes": "Imported from Douban doulist 24141.",
    },
}


HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Referer": "https://m.douban.com/",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


@dataclass
class Item:
    rank: int
    title_zh: str
    year: int | None
    note: str


def connect(db: str) -> sqlite3.Connection:
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    return con


def fetch_html(session: requests.Session, url: str, retries: int = 4, delay: float = 1.5) -> str:
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            response = session.get(url, timeout=30)
            response.raise_for_status()
            return response.text
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt < retries:
                time.sleep(delay * attempt)
    raise RuntimeError(f"failed to fetch {url}: {last_error}") from last_error


def parse_count(soup: BeautifulSoup) -> int | None:
    text = soup.get_text(" ", strip=True)
    m = re.search(r"全部\s*\((\d+)\)", text)
    return int(m.group(1)) if m else None


def parse_items(html: str) -> list[Item]:
    soup = BeautifulSoup(html, "html.parser")
    items: list[Item] = []
    for block in soup.select(".doulist-item"):
        number_el = block.select_one(".doulist-number")
        rank = int(number_el.get_text(strip=True)) if number_el and number_el.get_text(strip=True).isdigit() else len(items) + 1
        subject_link = block.select_one('.title a[href*="/subject/"]')
        subject_id = ""
        if subject_link and subject_link.get("href"):
            m = re.search(r"/subject/(\d+)/", subject_link["href"])
            if m:
                subject_id = m.group(1)
        title_el = block.select_one(".title a")
        title_text = title_el.get_text(" ", strip=True) if title_el else ""
        title_zh = title_text.split(" ")[0].strip()
        if not title_zh:
            continue
        text = block.get_text("\n", strip=True)
        year = None
        m = re.search(r"年份:\s*(\d{4})", text)
        if m:
            year = int(m.group(1))
        note = subject_id
        items.append(Item(rank=rank, title_zh=title_zh, year=year, note=note))
    return items


def scrape_doulist(session: requests.Session, doulist_id: int) -> tuple[str, int | None, list[Item]]:
    all_items: list[Item] = []
    title = ""
    expected = None
    seen_keys: set[str] = set()
    for start in range(0, 1000, 25):
        url = f"https://www.douban.com/doulist/{doulist_id}/?start={start}"
        html = fetch_html(session, url)
        soup = BeautifulSoup(html, "html.parser")
        if not title and soup.title:
            title = soup.title.get_text(strip=True)
        if expected is None:
            expected = parse_count(soup)
        page_items = parse_items(html)
        if not page_items:
            break
        new_count = 0
        for item in page_items:
            key = item.note or f"{item.title_zh}|{item.year or ''}"
            if key in seen_keys:
                continue
            seen_keys.add(key)
            all_items.append(item)
            new_count += 1
        if new_count == 0:
            break
        time.sleep(0.8)
    # Normalize ranks from crawl order so repeated page-local numbering cannot leak into the database.
    for idx, item in enumerate(all_items, start=1):
        item.rank = idx
    return title, expected, all_items


def ensure_version(con: sqlite3.Connection, version_id: str, label: str, expected_count: int | None, dated: str, notes: str) -> None:
    con.execute(
        """
        INSERT INTO canon_versions(id, list_id, label, dated, expected_count, notes)
        VALUES(?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET
            list_id=excluded.list_id,
            label=excluded.label,
            dated=excluded.dated,
            expected_count=excluded.expected_count,
            notes=excluded.notes
        """,
        (version_id, "douban", label, dated, expected_count, notes),
    )


def ensure_list(con: sqlite3.Connection) -> None:
    con.execute(
        """
        INSERT INTO canon_lists(id, name_zh, name_en, publisher, type, notes)
        VALUES(?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET
            name_zh=excluded.name_zh,
            name_en=excluded.name_en,
            publisher=excluded.publisher,
            type=excluded.type,
            notes=excluded.notes
        """,
        (
            "douban",
            "豆瓣正典库",
            "Douban Canon Library",
            "豆瓣",
            "民间正典",
            "Collected Douban list snapshots imported as canon versions.",
        ),
    )


def import_version(con: sqlite3.Connection, version_id: str, items: Iterable[Item]) -> None:
    rows = []
    for item in items:
        rows.append((version_id, item.rank, item.title_zh, item.year, 0, item.note))
    con.execute("DELETE FROM canon_entries WHERE version_id = ?", (version_id,))
    con.executemany("INSERT INTO canon_entries VALUES(?,?,?,?,?,?)", rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="editorial.db")
    args = parser.parse_args()

    session = requests.Session()
    session.headers.update(HEADERS)

    con = connect(args.db)
    ensure_list(con)

    for version_id, cfg in DOULISTS.items():
        title, observed_count, items = scrape_doulist(session, cfg["doulist_id"])
        label = cfg["label"] or title
        if observed_count is not None and cfg.get("expected_count") is None:
            cfg["expected_count"] = observed_count
        ensure_version(con, version_id, label, cfg.get("expected_count"), cfg["dated"], cfg["notes"])
        import_version(con, version_id, items)
        con.commit()
        print(f"imported {version_id}: {len(items)} items")

    con.close()


if __name__ == "__main__":
    main()
