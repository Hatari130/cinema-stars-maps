import argparse
import csv
import re
import sys
import time
from dataclasses import dataclass
from html import unescape
from pathlib import Path
from typing import Iterable

import requests
from bs4 import BeautifulSoup


BASE_URL = "https://movie.douban.com/people/{user_id}/collect"
PAGE_SIZE = 15

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://movie.douban.com/",
}

COUNTRY_OR_REGION_NAMES = {
    "中国",
    "中国大陆",
    "中国内地",
    "中国香港",
    "香港",
    "中国台湾",
    "台湾",
    "中国澳门",
    "澳门",
    "美国",
    "英国",
    "法国",
    "德国",
    "日本",
    "韩国",
    "朝鲜",
    "意大利",
    "西班牙",
    "葡萄牙",
    "荷兰",
    "比利时",
    "卢森堡",
    "爱尔兰",
    "冰岛",
    "丹麦",
    "挪威",
    "瑞典",
    "芬兰",
    "瑞士",
    "奥地利",
    "希腊",
    "土耳其",
    "俄罗斯",
    "苏联",
    "西德",
    "东德",
    "捷克",
    "斯洛伐克",
    "捷克斯洛伐克",
    "波兰",
    "匈牙利",
    "罗马尼亚",
    "保加利亚",
    "塞尔维亚",
    "黑山",
    "克罗地亚",
    "斯洛文尼亚",
    "波黑",
    "波斯尼亚和黑塞哥维那",
    "北马其顿",
    "马其顿",
    "阿尔巴尼亚",
    "南斯拉夫",
    "乌克兰",
    "白俄罗斯",
    "立陶宛",
    "拉脱维亚",
    "爱沙尼亚",
    "格鲁吉亚",
    "亚美尼亚",
    "阿塞拜疆",
    "摩尔多瓦",
    "加拿大",
    "墨西哥",
    "巴西",
    "阿根廷",
    "智利",
    "秘鲁",
    "哥伦比亚",
    "委内瑞拉",
    "乌拉圭",
    "巴拉圭",
    "玻利维亚",
    "厄瓜多尔",
    "古巴",
    "多米尼加",
    "海地",
    "危地马拉",
    "洪都拉斯",
    "尼加拉瓜",
    "哥斯达黎加",
    "巴拿马",
    "牙买加",
    "澳大利亚",
    "新西兰",
    "印度",
    "巴基斯坦",
    "孟加拉国",
    "斯里兰卡",
    "尼泊尔",
    "不丹",
    "马尔代夫",
    "泰国",
    "越南",
    "柬埔寨",
    "老挝",
    "缅甸",
    "马来西亚",
    "新加坡",
    "印度尼西亚",
    "菲律宾",
    "文莱",
    "蒙古",
    "伊朗",
    "伊拉克",
    "以色列",
    "巴勒斯坦",
    "黎巴嫩",
    "叙利亚",
    "约旦",
    "沙特阿拉伯",
    "阿联酋",
    "阿拉伯联合酋长国",
    "卡塔尔",
    "科威特",
    "巴林",
    "阿曼",
    "也门",
    "哈萨克斯坦",
    "乌兹别克斯坦",
    "吉尔吉斯斯坦",
    "塔吉克斯坦",
    "土库曼斯坦",
    "南非",
    "埃及",
    "摩洛哥",
    "突尼斯",
    "阿尔及利亚",
    "利比亚",
    "苏丹",
    "埃塞俄比亚",
    "肯尼亚",
    "坦桑尼亚",
    "乌干达",
    "卢旺达",
    "刚果",
    "刚果民主共和国",
    "尼日利亚",
    "加纳",
    "塞内加尔",
    "喀麦隆",
    "科特迪瓦",
    "马里",
    "布基纳法索",
    "安哥拉",
    "莫桑比克",
    "津巴布韦",
    "赞比亚",
    "博茨瓦纳",
    "纳米比亚",
    "马达加斯加",
    "毛里求斯",
    "卡塔尔",
    "马耳他",
    "塞浦路斯",
    "列支敦士登",
    "摩纳哥",
    "安道尔",
    "圣马力诺",
    "梵蒂冈",
}

YEAR_OVERRIDES_BY_TITLE = {
    "伴侣": "2022",
    "无处可依": "2022",
    "神话 / Phenomena": "1985",
    "劳工之爱情": "1922",
    "橡皮头 / L'homme à la tête en caoutchouc": "1901",
    "醉美人生": "2015",
    "高米迪 / Gormiti: The Lords of Nature Return!": "2008",
    "山水情": "1988",
    "辛普森一家 第二季 / The Simpsons Season 2": "1990",
}


@dataclass
class MovieRow:
    movie_name: str
    year: str
    country_or_region: str


def request_html(session: requests.Session, url: str, timeout: int, retries: int, delay: float) -> str:
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            response = session.get(url, timeout=timeout)
            response.raise_for_status()
            if "检测到有异常请求" in response.text or "sec.douban.com" in response.url:
                raise RuntimeError("Douban returned an anti-bot verification page")
            return response.text
        except Exception as exc:  # noqa: BLE001 - keep retry handling compact for CLI use.
            last_error = exc
            if attempt < retries:
                time.sleep(delay * attempt)
    raise RuntimeError(f"Failed to fetch {url}: {last_error}") from last_error


def extract_total(html: str) -> int:
    soup = BeautifulSoup(html, "html.parser")
    title = soup.find("title")
    candidates = []
    if title:
        candidates.append(title.get_text(" ", strip=True))
    subject_num = soup.select_one(".subject-num")
    if subject_num:
        candidates.append(subject_num.get_text(" ", strip=True))

    for text in candidates:
        match = re.search(r"(?:/|影视\()\s*(\d+)", text)
        if match:
            return int(match.group(1))
    raise ValueError("Could not determine total collect count from the first page")


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", unescape(text)).strip()


def extract_year(intro: str) -> str:
    match = re.search(r"\b(18|19|20)\d{2}\b", intro)
    return match.group(0) if match else ""


def extract_countries(intro: str) -> str:
    parts = [clean_text(part) for part in intro.split(" / ")]
    countries = []
    for part in parts:
        if part in COUNTRY_OR_REGION_NAMES and part not in countries:
            countries.append(part)
    return " / ".join(countries)


def parse_page(html: str) -> list[MovieRow]:
    soup = BeautifulSoup(html, "html.parser")
    rows: list[MovieRow] = []

    for item in soup.select(".grid-view .item"):
        title_el = item.select_one("li.title em")
        intro_el = item.select_one("li.intro")
        if not title_el:
            continue

        movie_name = clean_text(title_el.get_text(" ", strip=True))
        intro = clean_text(intro_el.get_text(" ", strip=True)) if intro_el else ""
        year = extract_year(intro) or YEAR_OVERRIDES_BY_TITLE.get(movie_name, "")
        rows.append(
            MovieRow(
                movie_name=movie_name,
                year=year,
                country_or_region=extract_countries(intro),
            )
        )

    return rows


def collect_movies(
    user_id: str,
    sleep_seconds: float,
    timeout: int,
    retries: int,
    allow_partial: bool = False,
) -> list[MovieRow]:
    session = requests.Session()
    session.headers.update(HEADERS)

    def page_url(start: int) -> str:
        return (
            f"{BASE_URL.format(user_id=user_id)}"
            f"?start={start}&sort=time&rating=all&filter=all&mode=grid"
        )

    first_url = page_url(0)
    first_html = request_html(session, first_url, timeout, retries, sleep_seconds)
    total = extract_total(first_html)
    rows = parse_page(first_html)

    for start in range(PAGE_SIZE, total, PAGE_SIZE):
        expected_rows = min(PAGE_SIZE, total - start)
        page_rows: list[MovieRow] = []
        for attempt in range(1, retries + 1):
            time.sleep(sleep_seconds)
            try:
                html = request_html(session, page_url(start), timeout, retries, sleep_seconds)
            except Exception:
                if allow_partial and len(rows) >= 3:
                    print(
                        f"Stopped at start={start}; returning {len(rows)} parsed rows.",
                        file=sys.stderr,
                    )
                    return rows
                raise
            page_rows = parse_page(html)
            if len(page_rows) == expected_rows:
                break
            if attempt < retries:
                print(
                    f"Retrying start={start}: expected {expected_rows}, got {len(page_rows)}",
                    file=sys.stderr,
                )
        rows.extend(page_rows)
        print(f"Fetched {min(start + PAGE_SIZE, total)}/{total}", file=sys.stderr)

    if len(rows) != total:
        print(
            f"Warning: expected {total} rows from page metadata, parsed {len(rows)} rows.",
            file=sys.stderr,
        )

    return rows[:total]


def write_csv(rows: Iterable[MovieRow], output_path: Path) -> None:
    with output_path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=["电影名", "年份", "国别"])
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "电影名": row.movie_name,
                    "年份": row.year,
                    "国别": row.country_or_region,
                }
            )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Scrape public Douban movie collect list: movie name, year, country/region."
    )
    parser.add_argument("--user-id", default="242612259")
    parser.add_argument("--output", default="douban_242612259_collect_movies.csv")
    parser.add_argument("--sleep", type=float, default=1.0, help="Seconds between page requests.")
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--retries", type=int, default=3)
    args = parser.parse_args()

    rows = collect_movies(args.user_id, args.sleep, args.timeout, args.retries)
    output_path = Path(args.output)
    write_csv(rows, output_path)

    missing_year = sum(1 for row in rows if not row.year)
    missing_country = sum(1 for row in rows if not row.country_or_region)
    print(f"Wrote {len(rows)} rows to {output_path}")
    print(f"Missing year: {missing_year}; missing country/region: {missing_country}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
