# -*- coding: utf-8 -*-
"""每个人的电影史 · 后端
匿名模型:每片天空 = 公开slug + 私密edit_token(链接即身份)。
覆盖语义:重新导入 = 同一slug,天空生长。删除 = 真删。"""
import json, os, random, re, secrets, sqlite3, datetime, time, html as html_lib, urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from fastapi import BackgroundTasks, Body, FastAPI, Request, UploadFile, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response, JSONResponse, RedirectResponse
from pipeline import parse_csv, build_sky, poster_svg, Editorial
from regions import REGIONS, CONTINENTS, band_of
from scrape_douban_collect import (
    BASE_URL,
    HEADERS,
    PAGE_SIZE,
    MovieRow,
    extract_countries,
    extract_genres,
    extract_profile_name,
    extract_runtime_from_detail,
    extract_runtime_minutes,
    extract_total,
    extract_year,
    parse_page,
    request_html,
)
import requests

BASE = os.path.dirname(os.path.abspath(__file__))
APP_DB = os.path.join(BASE, "app.db")
TPL = open(os.path.join(BASE, "templates", "sky.html"), encoding="utf-8").read()
BUILD_TPL = open(os.path.join(BASE, "templates", "build.html"), encoding="utf-8").read()
LANDING_TPL = open(os.path.join(BASE, "templates", "landing.html"), encoding="utf-8").read()
SHARE_TPL = open(os.path.join(BASE, "templates", "share.html"), encoding="utf-8").read()
ED = Editorial(os.path.join(BASE, "editorial.db"))
DOUBAN_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
DOUBAN_DELAY_MIN = float(os.getenv("DOUBAN_DELAY_MIN", "3.0"))
DOUBAN_DELAY_MAX = float(os.getenv("DOUBAN_DELAY_MAX", "6.0"))
DOUBAN_COOLDOWN_MIN = float(os.getenv("DOUBAN_COOLDOWN_MIN", "45.0"))
DOUBAN_COOLDOWN_MAX = float(os.getenv("DOUBAN_COOLDOWN_MAX", "90.0"))
DOUBAN_RUNTIME_WORKERS = max(1, int(os.getenv("DOUBAN_RUNTIME_WORKERS", "4")))

def db():
    con = sqlite3.connect(APP_DB, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("""CREATE TABLE IF NOT EXISTS skies(
        slug TEXT PRIMARY KEY, edit_token TEXT NOT NULL, title TEXT,
        sky_json TEXT NOT NULL, stats_json TEXT NOT NULL,
        created_at TEXT, updated_at TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS jobs(
        id TEXT PRIMARY KEY, source TEXT NOT NULL, status TEXT NOT NULL,
        douban_id TEXT, title TEXT, total INTEGER DEFAULT 0,
        fetched INTEGER DEFAULT 0, usable INTEGER DEFAULT 0,
        current_start INTEGER DEFAULT 0, message TEXT, error TEXT,
        slug TEXT, edit_token TEXT, created_at TEXT, updated_at TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS douban_pages(
        douban_id TEXT NOT NULL, start INTEGER NOT NULL, total INTEGER NOT NULL,
        rows_json TEXT NOT NULL, fetched_at TEXT NOT NULL,
        PRIMARY KEY(douban_id, start))""")
    con.execute("""CREATE TABLE IF NOT EXISTS douban_runtimes(
        subject_id TEXT PRIMARY KEY, runtime_minutes INTEGER NOT NULL,
        fetched_at TEXT NOT NULL)""")
    con.execute("""CREATE TABLE IF NOT EXISTS poster_cache(
        key TEXT PRIMARY KEY, title TEXT, year INTEGER, poster_url TEXT,
        subject_url TEXT, source TEXT, status TEXT, fetched_at TEXT)""")
    return con

app = FastAPI(title="每个人的电影史 API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://movie.douban.com", "http://movie.douban.com"],
    allow_methods=["POST", "OPTIONS"],
    allow_headers=["*"],
)

def _now():
    return datetime.datetime.now(datetime.UTC).isoformat()

def _insert_sky(title, sky, stats):
    slug, token = secrets.token_urlsafe(5), secrets.token_urlsafe(18)
    now = _now()
    con = db()
    con.execute("INSERT INTO skies VALUES(?,?,?,?,?,?,?)",
                (slug, token, title.strip()[:40], json.dumps(sky, ensure_ascii=False),
                 json.dumps(stats, ensure_ascii=False), now, now))
    con.commit(); con.close()
    return slug, token

def _store_sky(title, sky, stats):
    slug, token = _insert_sky(title, sky, stats)
    return RedirectResponse(url=f"/sky/{slug}?created=1&token={token}", status_code=303)

def _update_job(job_id, **fields):
    if not fields:
        return
    fields["updated_at"] = _now()
    keys = list(fields)
    con = db()
    con.execute(
        "UPDATE jobs SET " + ",".join(f"{k}=?" for k in keys) + " WHERE id=?",
        [fields[k] for k in keys] + [job_id],
    )
    con.commit(); con.close()

def _job(job_id):
    con = db()
    row = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    con.close()
    return row

def _job_payload(row):
    if not row:
        raise HTTPException(404, "任务不存在")
    redirect_url = None
    if row["slug"]:
        redirect_url = f"/sky/{row['slug']}?created=1&token={row['edit_token']}"
    collect_url = _page_url(row["douban_id"], 0) if row["douban_id"] else ""
    return {
        "id": row["id"],
        "status": row["status"],
        "source": row["source"],
        "douban_id": row["douban_id"],
        "title": row["title"],
        "total": row["total"] or 0,
        "fetched": row["fetched"] or 0,
        "usable": row["usable"] or 0,
        "current_start": row["current_start"] or 0,
        "message": row["message"] or "",
        "error": row["error"] or "",
        "redirect_url": redirect_url,
        "collect_url": collect_url,
        "browser_import_code_url": f"/api/jobs/{row['id']}/browser-import-code",
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }

def _douban_user_id(raw: str) -> str:
    value = (raw or "").strip()
    match = re.search(r"(?:https?://)?(?:movie\.)?douban\.com/people/([^/?#]+)/?", value)
    if match:
        value = match.group(1)
    value = value.strip().strip("/")
    if not DOUBAN_ID_RE.fullmatch(value):
        raise HTTPException(400, "豆瓣ID格式不正确")
    return value

def _films_from_douban_rows(rows):
    films, seen = [], set()
    for row in rows:
        title = (row.movie_name or "").split(" / ")[0].strip()
        ystr = re.sub(r"\D", "", row.year or "")
        if not title or not ystr:
            continue
        y = max(1895, min(2026, int(ystr)))
        key = (title, y)
        if key in seen:
            continue
        seen.add(key)
        country = (row.country_or_region or "").strip()
        film = {"t": title, "y": y, "s": band_of(country), "c": country}
        if getattr(row, "runtime_minutes", None):
            film["rt"] = int(row.runtime_minutes)
        genres = (getattr(row, "genres", None) or "").strip()
        if genres:
            film["g"] = genres.split()
        films.append(film)
    return films

def _page_url(user_id, start):
    return (
        f"{BASE_URL.format(user_id=user_id)}"
        f"?start={start}&sort=time&rating=all&filter=all&mode=grid"
    )

def _rows_json(rows):
    return json.dumps([{
        "movie_name": r.movie_name,
        "year": r.year,
        "country_or_region": r.country_or_region,
        "runtime_minutes": getattr(r, "runtime_minutes", None),
        "subject_url": getattr(r, "subject_url", ""),
        "genres": getattr(r, "genres", ""),
        "poster_url": getattr(r, "poster_url", ""),
    } for r in rows], ensure_ascii=False)

def _rows_from_json(text):
    return [MovieRow(**{**row, "runtime_minutes": row.get("runtime_minutes"),
                       "subject_url": row.get("subject_url", ""),
                       "genres": row.get("genres", ""),
                       "poster_url": row.get("poster_url", "")}) for row in json.loads(text)]

def _browser_rows(raw_rows):
    rows = []
    for raw in raw_rows if isinstance(raw_rows, list) else []:
        if not isinstance(raw, dict):
            continue
        title = (raw.get("movie_name") or raw.get("title") or "").strip()
        intro = (raw.get("intro") or "").strip()
        if not title:
            continue
        year = str(raw.get("year") or extract_year(intro) or "").strip()
        country = (raw.get("country_or_region") or extract_countries(intro) or "").strip()
        genres = (raw.get("genres") or extract_genres(intro) or "").strip()
        runtime = raw.get("runtime_minutes") or extract_runtime_minutes(intro)
        try:
            runtime = int(runtime) if runtime else None
        except (TypeError, ValueError):
            runtime = None
        rows.append(MovieRow(
            movie_name=title,
            year=year,
            country_or_region=country,
            runtime_minutes=runtime,
            subject_url=(raw.get("subject_url") or "").strip(),
            genres=genres,
            poster_url=(raw.get("poster_url") or "").strip(),
        ))
    return rows

def _find_cached_poster_url(title, year):
    target_title = (title or "").strip()
    try:
        target_year = int(year)
    except (TypeError, ValueError):
        target_year = None
    if not target_title or not target_year:
        return ""
    con = db()
    rows = con.execute(
        "SELECT rows_json FROM douban_pages WHERE rows_json LIKE ? ORDER BY fetched_at DESC",
        (f"%{target_title}%",),
    ).fetchall()
    con.close()
    for row in rows:
        try:
            movies = json.loads(row["rows_json"])
        except Exception:
            continue
        for movie in movies:
            if (movie.get("movie_name") or "").split(" / ")[0].strip() != target_title:
                continue
            try:
                movie_year = int(movie.get("year") or 0)
            except (TypeError, ValueError):
                movie_year = 0
            if movie_year == target_year and movie.get("poster_url"):
                return movie["poster_url"].strip()
    return ""

def _poster_key(title, year):
    compact_title = re.sub(r"\s+", "", (title or "").strip()).lower()
    return f"{compact_title}|{int(year or 0)}"

def _find_subject_url(title, year):
    target_title = (title or "").strip()
    try:
        target_year = int(year)
    except (TypeError, ValueError):
        target_year = None
    if not target_title or not target_year:
        return ""
    con = db()
    rows = con.execute(
        "SELECT rows_json FROM douban_pages WHERE rows_json LIKE ? ORDER BY fetched_at DESC",
        (f"%{target_title}%",),
    ).fetchall()
    con.close()
    for row in rows:
        try:
            movies = json.loads(row["rows_json"])
        except Exception:
            continue
        for movie in movies:
            if (movie.get("movie_name") or "").split(" / ")[0].strip() != target_title:
                continue
            try:
                movie_year = int(movie.get("year") or 0)
            except (TypeError, ValueError):
                movie_year = 0
            if movie_year == target_year and movie.get("subject_url"):
                return movie["subject_url"].strip()
    return ""

def _cache_poster(title, year, poster_url, subject_url, status):
    con = db()
    con.execute(
        """
        INSERT OR REPLACE INTO poster_cache(key,title,year,poster_url,subject_url,source,status,fetched_at)
        VALUES(?,?,?,?,?,?,?,?)
        """,
        (_poster_key(title, year), title, int(year or 0), poster_url or "", subject_url or "", "douban", status, _now()),
    )
    con.commit()
    con.close()

def _fetch_douban_poster(subject_url):
    if not subject_url:
        return ""
    subject_id = _subject_id(subject_url)
    if subject_id:
        session = requests.Session()
        session.headers.update({
            **HEADERS,
            "Referer": subject_url,
        })
        for url in (
            f"https://m.douban.com/rexxar/api/v2/movie/{subject_id}",
            f"https://m.douban.com/rexxar/api/v2/tv/{subject_id}",
        ):
            try:
                response = session.get(url, timeout=15)
                if response.ok:
                    data = response.json()
                    poster = (
                        (data.get("pic") or {}).get("normal")
                        or data.get("cover_url")
                        or (((data.get("cover") or {}).get("image") or {}).get("normal") or {}).get("url")
                        or (((data.get("cover") or {}).get("image") or {}).get("small") or {}).get("url")
                    )
                    if poster:
                        return poster.strip()
            except Exception:
                pass
    session = requests.Session()
    session.headers.update(HEADERS)
    html = request_html(session, subject_url, 18, 2, 0.8)
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    img = soup.select_one("#mainpic img") or soup.select_one('img[rel="v:image"]')
    if not img:
        return ""
    return (img.get("src") or "").strip()

def _cached_page(user_id, start):
    con = db()
    row = con.execute(
        "SELECT total, rows_json FROM douban_pages WHERE douban_id=? AND start=?",
        (user_id, start),
    ).fetchone()
    con.close()
    if not row:
        return None
    return row["total"], _rows_from_json(row["rows_json"])

def _save_page(user_id, start, total, rows):
    con = db()
    con.execute(
        "INSERT OR REPLACE INTO douban_pages(douban_id,start,total,rows_json,fetched_at) VALUES(?,?,?,?,?)",
        (user_id, start, total, _rows_json(rows), _now()),
    )
    con.commit(); con.close()

def _clear_douban_pages(user_id):
    con = db()
    con.execute("DELETE FROM douban_pages WHERE douban_id=?", (user_id,))
    con.commit(); con.close()

def _clear_all_douban_pages():
    con = db()
    con.execute("DELETE FROM douban_pages")
    con.commit(); con.close()

def _fetch_page(session, user_id, start, known_total=None, use_cache=True, save_cache=True):
    cached = _cached_page(user_id, start) if use_cache else None
    if cached:
        return cached[0], cached[1], True, ""
    html = request_html(session, _page_url(user_id, start), 20, 3, 1.0)
    total = known_total if known_total is not None else extract_total(html)
    rows = parse_page(html)
    if save_cache:
        _save_page(user_id, start, total, rows)
    profile_name = extract_profile_name(html) if start == 0 else ""
    return total, rows, False, profile_name

# douban_pages is only a short-lived import buffer now, not a persistent cache.
_clear_all_douban_pages()

def _cache_summary(user_id):
    con = db()
    pages = con.execute(
        "SELECT start,total,rows_json,fetched_at FROM douban_pages WHERE douban_id=? ORDER BY start",
        (user_id,),
    ).fetchall()
    con.close()
    rows_count = 0
    total = 0
    fetched_at = ""
    starts = []
    for page in pages:
        starts.append(page["start"])
        total = max(total, int(page["total"] or 0))
        fetched_at = max(fetched_at, page["fetched_at"] or "")
        rows_count += len(_rows_from_json(page["rows_json"]))
    return {
        "has_cache": bool(pages),
        "pages": len(pages),
        "rows": rows_count,
        "total": total,
        "starts": starts,
        "fetched_at": fetched_at,
    }

def _fetch_profile_name(session, user_id):
    try:
        html = request_html(session, _page_url(user_id, 0), 20, 2, 1.0)
    except Exception:
        return ""
    return extract_profile_name(html)

def _browser_import_script(api_base, job_id, user_id):
    return r"""
(async function(){
  const API_BASE = "__API_BASE__";
  const JOB = "__JOB_ID__";
  const USER = "__USER_ID__";
  const PAGE_SIZE = 15;
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  function $(sel, root){ return root.querySelector(sel); }
  function $$(sel, root){ return Array.from(root.querySelectorAll(sel)); }
  function clean(s){ return (s || "").replace(/\s+/g, " ").trim(); }
  function overlay(){
    let el = document.getElementById("every-cinema-importer");
    if (el) return el;
    el = document.createElement("div");
    el.id = "every-cinema-importer";
    el.style.cssText = "position:fixed;right:18px;bottom:18px;z-index:2147483647;width:260px;padding:14px 16px;background:#08101f;color:#f2d88f;border:1px solid rgba(242,216,143,.45);border-radius:8px;font:14px/1.7 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;box-shadow:0 14px 42px rgba(0,0,0,.35)";
    el.textContent = "电影史导入准备中";
    document.body.appendChild(el);
    return el;
  }
  const box = overlay();
  function say(msg){ box.textContent = msg; }
  function totalOf(doc){
    const candidates = [clean(doc.title), clean($(".subject-num", doc)?.textContent)];
    for (const text of candidates) {
      const m = text && text.match(/(?:\/|影视\()\s*(\d+)/);
      if (m) return parseInt(m[1], 10);
    }
    return 0;
  }
  function profileName(doc){
    const candidates = [
      clean($("#db-usr-profile .info h1", doc)?.textContent),
      clean($("#db-usr-profile h1", doc)?.textContent),
      clean($("h1", doc)?.textContent),
      clean(doc.title)
    ];
    for (let text of candidates) {
      text = text.replace(/\s*[-_—|]\s*豆瓣电影.*$/, "").replace(/^豆瓣电影\s*[-_—|]\s*/, "").replace(/\s*\(\d+\)\s*$/, "");
      for (const suffix of ["看过的电影","想看的电影","在看的电视剧","看过的电视剧","的电影","的影视"]) {
        if (text.endsWith(suffix)) text = text.slice(0, -suffix.length);
      }
      text = clean(text);
      if (text && text !== "豆瓣电影" && text !== "我看过的电影") return text.slice(0, 40);
    }
    return "";
  }
  function rowsOf(doc){
    return $$(".grid-view .item", doc).map(item => {
      const title = clean($("li.title em", item)?.textContent);
      const intro = clean($("li.intro", item)?.textContent);
      const link = $("li.title a[href]", item);
      const poster = $(".pic img[src]", item);
      const year = (intro.match(/\b(?:18|19|20)\d{2}\b/) || [""])[0];
      const runtime = (intro.match(/(\d{2,4})\s*分钟/) || [])[1] || "";
      return {
        movie_name: title,
        year,
        intro,
        runtime_minutes: runtime ? parseInt(runtime, 10) : null,
        subject_url: link ? link.href : "",
        poster_url: poster ? poster.src : ""
      };
    }).filter(row => row.movie_name);
  }
  function pageUrl(start){
    const url = new URL(location.href);
    const mine = location.pathname === "/mine";
    url.pathname = mine ? "/mine" : "/people/" + USER + "/collect";
    if (mine) url.searchParams.set("status", "collect");
    url.searchParams.set("start", String(start));
    url.searchParams.set("sort", "time");
    url.searchParams.set("rating", "all");
    url.searchParams.set("filter", "all");
    url.searchParams.set("mode", "grid");
    return url.toString();
  }
  async function upload(path, payload){
    const r = await fetch(API_BASE + path, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(payload)
    });
    if (!r.ok) throw new Error(await r.text());
    return r.json();
  }
  const isPeopleCollect = /\/people\/[^/]+\/collect/.test(location.pathname);
  const isMineCollect = location.pathname === "/mine" && (new URL(location.href)).searchParams.get("status") === "collect";
  if (location.hostname !== "movie.douban.com" || (!isPeopleCollect && !isMineCollect)) {
    alert("请先打开你的豆瓣「看过」页面，再点击书签栏里的「电影史导入器」。登录后从豆瓣进入的 /mine?status=collect 页面也可以。");
    return;
  }
  const current = (location.pathname.match(/\/people\/([^/]+)\/collect/) || [])[1];
  if (current && current !== USER && !confirm("当前页面的豆瓣 ID 和任务不一致，仍然继续导入当前页面吗？")) return;
  let start = 0, total = 0, imported = 0;
  try {
    while (true) {
      let doc;
      say("正在读取第 " + (Math.floor(start / PAGE_SIZE) + 1) + " 页");
      const html = await fetch(pageUrl(start), {credentials:"include"}).then(r => {
        if (!r.ok) throw new Error("豆瓣返回 " + r.status);
        return r.text();
      });
      doc = new DOMParser().parseFromString(html, "text/html");
      if (!total) total = totalOf(doc);
      const rows = rowsOf(doc);
      if (!rows.length) {
        if (!imported) throw new Error("没有读到影片，请确认当前账号的豆瓣「看过」页面可见，且页面不是验证码或异常页。");
        break;
      }
      await upload("/api/jobs/" + JOB + "/browser-import-page", {
        start,
        total: total || (start + rows.length),
        profile_name: start === 0 ? profileName(doc) : "",
        rows
      });
      imported += rows.length;
      say("已读取 " + imported + (total ? " / " + total : "") + " 部");
      if (total && start + PAGE_SIZE >= total) break;
      start += PAGE_SIZE;
      await sleep(550 + Math.random() * 650);
    }
    if (imported < 3) throw new Error("只读到 " + imported + " 部影片，至少需要 3 部才能生成星空。");
    const done = await upload("/api/jobs/" + JOB + "/browser-import-finish", {total: total || imported});
    say("导入完成，正在回到电影史页面");
    setTimeout(() => { location.href = API_BASE + (done.build_url || ("/build/" + JOB)); }, 700);
  } catch (err) {
    console.error(err);
    say("导入中断：" + (err && err.message ? err.message : err));
    alert("导入中断：" + (err && err.message ? err.message : err));
  }
})();
""".strip().replace("__API_BASE__", json.dumps(api_base)[1:-1]) \
    .replace("__JOB_ID__", json.dumps(job_id)[1:-1]) \
    .replace("__USER_ID__", json.dumps(user_id)[1:-1])

def _browser_import_bookmarklet(api_base, job_id, user_id):
    code = _browser_import_script(api_base, job_id, user_id)
    return "javascript:" + urllib.parse.quote(code, safe="()'!*,:;=+./?")

def _subject_id(url):
    match = re.search(r"/subject/(\d+)", url or "")
    return match.group(1) if match else ""


def _runtime_cache(subject_ids):
    found = {}
    con = db()
    ids = list(dict.fromkeys(subject_ids))
    for offset in range(0, len(ids), 800):
        chunk = ids[offset:offset + 800]
        marks = ",".join("?" for _ in chunk)
        if marks:
            for row in con.execute(
                f"SELECT subject_id,runtime_minutes FROM douban_runtimes WHERE subject_id IN ({marks})",
                chunk,
            ):
                found[row["subject_id"]] = row["runtime_minutes"]
    con.close()
    return found


def _save_runtimes(items):
    if not items:
        return
    con = db()
    con.executemany(
        "INSERT OR REPLACE INTO douban_runtimes(subject_id,runtime_minutes,fetched_at) VALUES(?,?,?)",
        [(subject_id, minutes, _now()) for subject_id, minutes in items],
    )
    con.commit()
    con.close()


def _enrich_runtimes(job_id, rows):
    candidates = [(row, _subject_id(row.subject_url)) for row in rows if not row.runtime_minutes]
    candidates = [(row, subject_id) for row, subject_id in candidates if subject_id]
    if not candidates:
        return

    cached = _runtime_cache(subject_id for _, subject_id in candidates)
    pending = []
    for row, subject_id in candidates:
        if subject_id in cached:
            row.runtime_minutes = cached[subject_id]
        else:
            pending.append((row, subject_id))

    if not pending:
        return

    known = sum(1 for row in rows if row.runtime_minutes)
    _update_job(job_id, message=f"正在补全影片时长 · 已知 {known}/{len(rows)} 部")

    def fetch_runtime(item):
        row, subject_id = item
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            html = request_html(session, row.subject_url, 15, 2, 0.5)
            return row, subject_id, extract_runtime_from_detail(html)
        except Exception:
            return row, subject_id, None
        finally:
            session.close()

    saved = []
    with ThreadPoolExecutor(max_workers=DOUBAN_RUNTIME_WORKERS) as pool:
        futures = [pool.submit(fetch_runtime, item) for item in pending]
        for index, future in enumerate(as_completed(futures), 1):
            row, subject_id, minutes = future.result()
            if minutes:
                row.runtime_minutes = minutes
                saved.append((subject_id, minutes))
            if len(saved) >= 25:
                _save_runtimes(saved)
                saved.clear()
            if index % 25 == 0 or index == len(futures):
                known = sum(1 for movie in rows if movie.runtime_minutes)
                _update_job(job_id, message=f"正在补全影片时长 · 已知 {known}/{len(rows)} 部")
    _save_runtimes(saved)

def _apply_cached_runtimes(rows):
    candidates = [(row, _subject_id(row.subject_url)) for row in rows if not row.runtime_minutes]
    candidates = [(row, subject_id) for row, subject_id in candidates if subject_id]
    if not candidates:
        return
    cached = _runtime_cache(subject_id for _, subject_id in candidates)
    for row, subject_id in candidates:
        if subject_id in cached:
            row.runtime_minutes = cached[subject_id]


def _set_job_progress(job_id, total, rows, current_start, message, status="running"):
    films = _films_from_douban_rows(rows)
    _update_job(
        job_id,
        status=status,
        total=total or 0,
        fetched=len(rows),
        usable=len(films),
        current_start=current_start,
        message=message,
        error="",
    )

def _douban_failure_message(exc):
    text = str(exc)
    if any(token in text for token in ("403", "sec.douban.com", "error code: 004", "Login")):
        return "豆瓣暂时拒绝服务器读取，可换用浏览器导入"
    return "豆瓣抓取失败"

def _finish_douban_job(job_id, rows, total, partial, reason=""):
    row = _job(job_id)
    if not row:
        return
    films = _films_from_douban_rows(rows)
    if len(films) < 3:
        _update_job(
            job_id,
            status="failed",
            message="可分析的观影记录不足",
            error=reason or "可分析的观影记录不足3部",
        )
        return
    sky, stats = build_sky(films, ED)
    stats["source"] = "douban"
    stats["douban_id"] = row["douban_id"]
    stats["douban_total"] = total
    stats["douban_fetched"] = len(rows)
    stats["partial"] = bool(partial)
    if partial and reason:
        stats["partial_reason"] = reason[:240]
    slug, token = _insert_sky(row["title"] or "", sky, stats)
    _update_job(
        job_id,
        status="partial" if partial else "complete",
        total=total or 0,
        fetched=len(rows),
        usable=len(films),
        message="豆瓣暂停回应，已先生成当前星空" if partial else "星空生成完成",
        error=reason[:500] if partial else "",
        slug=slug,
        edit_token=token,
    )

def _run_douban_job(job_id, mode="normal"):
    row = _job(job_id)
    if not row:
        return
    user_id = row["douban_id"]
    title = row["title"] or ""
    _update_job(job_id, status="running", title=title, message="正在连接豆瓣观影页", error="")
    session = requests.Session()
    session.headers.update(HEADERS)
    rows = []
    total = 0
    if mode != "cache_only":
        _clear_douban_pages(user_id)
    try:
        if mode == "cache_only":
            rows, cached_total = _rows_from_cache(user_id)
            if len(rows) >= 3:
                total = cached_total or len(rows)
                _set_job_progress(job_id, total, rows, 0, "正在读取缓存观影记录")
                _apply_cached_runtimes(rows)
                if _job(job_id)["status"] == "running":
                    _finish_douban_job(job_id, rows, total, cached_total and len(rows) < cached_total, "使用缓存生成")
                return
            _update_job(job_id, message="缓存数据不足，正在重新连接豆瓣")
        use_cache = False
        total, first_rows, from_cache, profile_name = _fetch_page(session, user_id, 0, use_cache=use_cache)
        if not title:
            title = profile_name or _fetch_profile_name(session, user_id)
            if title:
                _update_job(job_id, title=title)
        rows.extend(first_rows)
        _set_job_progress(
            job_id,
            total,
            rows,
            0,
            "读取缓存页" if from_cache else "正在翻阅第 1 页",
        )
        for start in range(PAGE_SIZE, total, PAGE_SIZE):
            cached = _cached_page(user_id, start) if use_cache else None
            if cached:
                _, page_rows = cached
                rows.extend(page_rows)
                _set_job_progress(job_id, total, rows, start, "读取已缓存的观影片页")
                continue
            delay = random.uniform(DOUBAN_DELAY_MIN, DOUBAN_DELAY_MAX)
            page_no = start // PAGE_SIZE + 1
            page_count = (total + PAGE_SIZE - 1) // PAGE_SIZE
            _set_job_progress(
                job_id,
                total,
                rows,
                start,
                f"等待 {delay:.1f} 秒后翻阅第 {page_no}/{page_count} 页",
            )
            time.sleep(delay)
            try:
                _, page_rows, _, _ = _fetch_page(session, user_id, start, total, use_cache=use_cache)
            except Exception as exc:
                cooldown = random.uniform(DOUBAN_COOLDOWN_MIN, DOUBAN_COOLDOWN_MAX)
                _update_job(
                    job_id,
                    status="cooling",
                    current_start=start,
                    message=f"豆瓣暂停回应，冷却 {int(cooldown)} 秒后重试一次",
                    error=str(exc)[:500],
                )
                time.sleep(cooldown)
                try:
                    _, page_rows, _, _ = _fetch_page(session, user_id, start, total, use_cache=use_cache)
                except Exception as exc2:
                    _finish_douban_job(job_id, rows, total, True, str(exc2))
                    return
            rows.extend(page_rows)
            _set_job_progress(job_id, total, rows, start, f"已翻阅第 {page_no}/{page_count} 页")
        _enrich_runtimes(job_id, rows)
        if _job(job_id)["status"] == "running":
            _finish_douban_job(job_id, rows, total, False)
    except Exception as exc:
        if len(rows) >= 3:
            _finish_douban_job(job_id, rows, total, True, str(exc))
        else:
            _update_job(job_id, status="failed", message=_douban_failure_message(exc), error=str(exc)[:500])
    finally:
        _clear_douban_pages(user_id)

@app.get("/", response_class=HTMLResponse)
def landing():
    return LANDING_TPL

def _render(row):
    slug, title, sky_json, stats_json = row
    sky = json.loads(sky_json)
    stats = json.loads(stats_json)
    js = lambda o: json.dumps(o, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    reg_client = [{"n": r["n"], "c": r["c"], "a0": r["a0"], "a1": r["a1"]} for r in REGIONS]
    html = TPL.replace("__NM__", js(sky["nm"])) \
              .replace("__LINES__", js(sky["lines"])) \
              .replace("__DOTS__", js(sky["dots"])) \
              .replace("__REGIONS__", js(reg_client)) \
              .replace("__CONTINENTS__", js(CONTINENTS)) \
              .replace("__STATS__", js(stats)) \
              .replace("__SLUG__", slug) \
              .replace("__TITLE__", html_lib.escape(title or "我的电影史", quote=True)) \
              .replace("__TITLE_BLOCK__", _sky_title_block(title)) \
              .replace("__COUNT__", str(stats["films"]))
    return html

def _sky_title_block(title):
    clean = (title or "我的电影史").strip()
    if clean.endswith("看过") and len(clean) > 2:
        name = clean[:-2].strip()
        if name:
            return '%s<span class="title-action">看过</span>' % html_lib.escape(name, quote=True)
    return html_lib.escape(clean, quote=True)

def _share_title_block(title):
    clean = (title or "我的电影史").strip()
    if clean.endswith("看过") and len(clean) > 2:
        name = clean[:-2].strip()
        if name:
            return (
                '<div class="title" aria-label="%s">'
                '<span class="title-main">%s</span>'
                '<span class="title-action">看过</span>'
                '</div>'
            ) % (
                html_lib.escape(clean, quote=True),
                html_lib.escape(name, quote=True),
            )
    return '<h1 class="title-main">%s</h1>' % html_lib.escape(clean, quote=True)

def _share_card_svg(sky, title, stats):
    W, H = 1080, 1920
    clean_title = (title or "我的电影史").strip()
    title_name, title_action = clean_title, ""
    if clean_title.endswith("看过") and len(clean_title) > 2:
        title_name = clean_title[:-2].strip() or clean_title
        title_action = "看过"
    title_size = 92 if len(title_name) <= 8 else max(48, 92 - (len(title_name) - 8) * 5)
    safe_title = html_lib.escape(title_name, quote=True)
    safe_action = html_lib.escape(title_action, quote=True)
    poster = poster_svg(sky, clean_title, int(stats.get("films") or 0))
    poster_data = urllib.parse.quote(poster)
    films = int(stats.get("films") or 0)
    runtime = int(stats.get("runtime_minutes") or 0)
    runtime_known = int(stats.get("runtime_known") or 0)
    years = int(stats.get("watch_years") or (
        (stats.get("year_max") or 0) - (stats.get("year_min") or 0) + 1
        if stats.get("year_max") and stats.get("year_min") else 0
    ))
    fmt = lambda value: f"{int(value):,}" if value else "--"
    def metric_text(x, y, value, unit, number_size, shrink_at=6):
        text = fmt(value)
        size = number_size
        if len(text) >= shrink_at + 2:
            size -= 10
        elif len(text) >= shrink_at:
            size -= 6
        return (
            f'<text x="{x}" y="{y}" fill="#F2D88F" font-family="Georgia,serif" '
            f'font-size="{size}">{html_lib.escape(text, quote=True)}'
            f'<tspan dx="18" fill="#C9A86A" opacity=".70" '
            f'font-family="Songti SC,STSong,serif" font-size="20">'
            f'{html_lib.escape(unit, quote=True)}</tspan></text>'
        )
    films_metric = metric_text(96, 1435, films, "部影片", 52, 6)
    runtime_metric = metric_text(416, 1435, runtime, "分钟", 42, 8)
    years_metric = metric_text(696, 1435, years, "观影年数", 52, 5)
    if runtime and runtime_known and runtime_known < films:
        runtime_note = f"已统计 {runtime_known:,} 部"
    elif not runtime:
        runtime_note = "片长待补全"
    else:
        runtime_note = ""
    stars = []
    for i in range(90):
        x = (97 * i + 53) % W
        y = (151 * i + 89) % H
        if 300 < y < 1280 and 110 < x < 970:
            continue
        r = 1 + (i % 3) * 0.35
        op = 0.10 + (i % 5) * 0.035
        stars.append(f'<circle cx="{x}" cy="{y}" r="{r:.1f}" fill="#F0E6CB" opacity="{op:.2f}"/>')
    action_markup = (
        f'<tspan dx="18" fill="#C9A86A" opacity=".78" font-family="Songti SC,STSong,serif" '
        f'font-size="42" letter-spacing="7">{safe_action}</tspan>'
        if safe_action else ""
    )
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">
<defs>
  <linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0" stop-color="#07142B"/>
    <stop offset="1" stop-color="#030917"/>
  </linearGradient>
</defs>
<rect width="{W}" height="{H}" fill="url(#bg)"/>
<rect x="1" y="1" width="{W-2}" height="{H-2}" fill="none" stroke="#C9A86A" stroke-opacity=".34"/>
{''.join(stars)}
<text x="96" y="120" fill="#F0E6CB" opacity=".45" font-family="Georgia,serif" font-size="19" letter-spacing="10">EVERYONE · EVERY CINEMA</text>
<text x="96" y="245" fill="#F2D88F" font-family="Georgia,'Songti SC','STSong',serif" font-size="{title_size}" letter-spacing="12">{safe_title}{action_markup}</text>
<text x="96" y="304" fill="#C9A86A" opacity=".74" font-family="Songti SC,STSong,serif" font-size="24" letter-spacing="3">每个人的电影史 · 1895—2026</text>
<rect x="96" y="360" width="888" height="980" fill="#060F22" opacity=".52"/>
<image x="110" y="386" width="860" height="950" preserveAspectRatio="xMidYMid meet" href="data:image/svg+xml;charset=utf-8,{poster_data}"/>
<line x1="96" y1="1394" x2="386" y2="1394" stroke="#C9A86A" stroke-opacity=".28"/>
<line x1="416" y1="1394" x2="666" y2="1394" stroke="#C9A86A" stroke-opacity=".28"/>
<line x1="696" y1="1394" x2="984" y2="1394" stroke="#C9A86A" stroke-opacity=".28"/>
{films_metric}
{runtime_metric}
<text x="416" y="1485" fill="#F0E6CB" opacity=".34" font-family="Songti SC,STSong,serif" font-size="17">{html_lib.escape(runtime_note, quote=True)}</text>
{years_metric}
<rect x="96" y="1578" width="408" height="76" fill="#050D1D" fill-opacity=".54" stroke="#C9A86A" stroke-opacity=".42"/>
<rect x="534" y="1578" width="450" height="76" fill="#050D1D" fill-opacity=".54" stroke="#C9A86A" stroke-opacity=".42"/>
<text x="300" y="1628" text-anchor="middle" fill="#F2D88F" font-family="Songti SC,STSong,serif" font-size="22" letter-spacing="2">分享星图</text>
<text x="759" y="1628" text-anchor="middle" fill="#F2D88F" font-family="Songti SC,STSong,serif" font-size="22" letter-spacing="2">查看完整星图</text>
<text x="96" y="1724" fill="#F0E6CB" opacity=".38" font-family="Georgia,serif" font-size="16" letter-spacing="4">FILM HISTORY</text>
<text x="96" y="1754" fill="#F0E6CB" opacity=".38" font-family="Georgia,serif" font-size="16" letter-spacing="4">CONSTELLATION</text>
<text x="96" y="1848" fill="#C9A86A" opacity=".70" font-family="Georgia,serif" font-size="20" letter-spacing="1.5">https://cinema-stars-maps.onrender.com/</text>
</svg>'''

@app.post("/api/skies")
async def create_sky(file: UploadFile, title: str = Form("")):
    films = parse_csv(await file.read())
    if len(films) < 3:
        raise HTTPException(400, "CSV解析后不足3部影片,请检查格式(电影名/年份/国别)")
    sky, stats = build_sky(films, ED)
    return _store_sky(title, sky, stats)

@app.get("/api/douban-cache")
def douban_cache(douban_id: str):
    user_id = _douban_user_id(douban_id)
    summary = _cache_summary(user_id)
    summary["douban_id"] = user_id
    return JSONResponse(summary)

@app.post("/api/jobs/douban")
@app.post("/api/skies/douban")
async def create_douban_job(
    background_tasks: BackgroundTasks,
    douban_id: str = Form(...),
    title: str = Form(""),
    import_mode: str = Form("normal"),
):
    user_id = _douban_user_id(douban_id)
    mode = (import_mode or "normal").strip().lower()
    if mode not in ("normal", "cache", "refresh"):
        mode = "normal"
    job_id = secrets.token_urlsafe(10)
    now = _now()
    con = db()
    con.execute(
        "INSERT INTO jobs(id,source,status,douban_id,title,message,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
        (job_id, "douban", "queued", user_id, title.strip()[:40], "等待观测台启动", now, now),
    )
    con.commit(); con.close()
    background_tasks.add_task(_run_douban_job, job_id, "cache_only" if mode == "cache" else mode)
    return RedirectResponse(url=f"/build/{job_id}", status_code=303)

@app.get("/build/{job_id}", response_class=HTMLResponse)
def build_page(job_id: str, request: Request):
    row = _job(job_id)
    if not row:
        raise HTTPException(404, "任务不存在")
    api_base = str(request.base_url).rstrip("/")
    html = BUILD_TPL.replace("__JOB_ID__", job_id) \
                    .replace("__BOOKMARKLET_HREF__", html_lib.escape(_browser_import_bookmarklet(api_base, job_id, row["douban_id"]), quote=True))
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})

@app.get("/api/jobs/{job_id}.json")
def job_json(job_id: str):
    return JSONResponse(_job_payload(_job(job_id)))

@app.get("/api/jobs/{job_id}/browser-import-code")
def browser_import_code(job_id: str, request: Request):
    row = _job(job_id)
    if not row:
        raise HTTPException(404, "任务不存在")
    api_base = str(request.base_url).rstrip("/")
    code = _browser_import_script(api_base, job_id, row["douban_id"])
    return Response(code, media_type="application/javascript; charset=utf-8")

@app.post("/api/jobs/{job_id}/browser-import-page")
def browser_import_page(job_id: str, payload: dict = Body(...)):
    row = _job(job_id)
    if not row:
        raise HTTPException(404, "任务不存在")
    if row["slug"]:
        return {"ok": True, "status": row["status"], "done": True}
    try:
        start = max(0, int(payload.get("start") or 0))
    except (TypeError, ValueError):
        start = 0
    rows = _browser_rows(payload.get("rows") or [])
    if not rows:
        raise HTTPException(400, "没有可导入的影片")
    try:
        total = int(payload.get("total") or row["total"] or start + len(rows))
    except (TypeError, ValueError):
        total = start + len(rows)
    profile_name = (payload.get("profile_name") or "").strip()[:40]
    if start == 0:
        _clear_douban_pages(row["douban_id"])
    _save_page(row["douban_id"], start, total, rows)
    all_rows, _ = _rows_from_cache(row["douban_id"])
    fields = {}
    if profile_name and not (row["title"] or "").strip():
        fields["title"] = profile_name
    if fields:
        _update_job(job_id, **fields)
    _set_job_progress(
        job_id,
        total,
        all_rows,
        start,
        f"浏览器导入中 · 已读取 {len(all_rows)}/{total} 部",
        status="browser_importing",
    )
    return {"ok": True, "fetched": len(all_rows), "total": total}

@app.post("/api/jobs/{job_id}/browser-import-finish")
def browser_import_finish(job_id: str, payload: dict = Body(default={})):
    row = _job(job_id)
    if not row:
        raise HTTPException(404, "任务不存在")
    if row["slug"]:
        return {"ok": True, "status": row["status"], "build_url": f"/build/{job_id}"}
    rows, _ = _rows_from_cache(row["douban_id"])
    if len(rows) < 3:
        _clear_douban_pages(row["douban_id"])
        raise HTTPException(400, "缓存数据不足，无法生成星空")
    try:
        total = int(payload.get("total") or row["total"] or len(rows))
    except (TypeError, ValueError):
        total = len(rows)
    _update_job(job_id, status="finalizing", message="浏览器导入完成，正在生成星空", error="")
    _finish_douban_job(job_id, rows, total, False)
    _clear_douban_pages(row["douban_id"])
    return {"ok": True, "status": "complete", "build_url": f"/build/{job_id}"}

def _rows_from_cache(user_id):
    con = db()
    pages = con.execute(
        "SELECT start, total, rows_json FROM douban_pages WHERE douban_id=? ORDER BY start",
        (user_id,),
    ).fetchall()
    con.close()
    if not pages:
        return [], 0
    rows = []
    total = 0
    for p in pages:
        total = max(total, int(p["total"] or 0))
        rows.extend(_rows_from_json(p["rows_json"]))
    return rows, total or len(rows)

@app.post("/api/jobs/{job_id}/finalize")
def finalize_job(job_id: str):
    row = _job(job_id)
    if not row:
        raise HTTPException(404, "任务不存在")
    if row["status"] in ("complete", "partial", "failed"):
        return {"ok": True, "status": row["status"]}
    rows, _ = _rows_from_cache(row["douban_id"])
    if len(rows) < 3:
        _clear_douban_pages(row["douban_id"])
        raise HTTPException(400, "缓存数据不足，无法生成星空")
    _update_job(job_id, status="finalizing", message="跳过时长补全，正在生成星空")
    _finish_douban_job(job_id, rows, row["total"] or len(rows), True, "用户跳过时长补全")
    _clear_douban_pages(row["douban_id"])
    return {"ok": True, "status": "partial"}

@app.post("/api/jobs/{job_id}/resume")
def resume_job(job_id: str, background_tasks: BackgroundTasks):
    row = _job(job_id)
    if not row:
        raise HTTPException(404, "任务不存在")
    if row["source"] != "douban":
        raise HTTPException(400, "只有豆瓣任务可以续抓")
    if row["status"] in ("running", "cooling", "queued"):
        return {"ok": True, "status": row["status"]}
    _update_job(job_id, status="queued", message="等待继续补齐", error="")
    background_tasks.add_task(_run_douban_job, job_id)
    return {"ok": True, "status": "queued"}

@app.get("/sky/{slug}", response_class=HTMLResponse)
def view_sky(slug: str):
    con = db()
    row = con.execute("SELECT slug,title,sky_json,stats_json FROM skies WHERE slug=?", (slug,)).fetchone()
    con.close()
    if not row:
        raise HTTPException(404, "这片天空不存在或已被删除")
    return _render(row)

@app.get("/api/skies/{slug}.json")
def sky_json(slug: str):
    con = db()
    row = con.execute("SELECT title,sky_json,stats_json,created_at,updated_at FROM skies WHERE slug=?",
                      (slug,)).fetchone()
    con.close()
    if not row:
        raise HTTPException(404)
    return JSONResponse({"title": row[0], "sky": json.loads(row[1]),
                         "stats": json.loads(row[2]), "created_at": row[3], "updated_at": row[4]})

@app.get("/sky/{slug}/share", response_class=HTMLResponse)
def share_sky(slug: str):
    con = db()
    row = con.execute("SELECT title,stats_json FROM skies WHERE slug=?", (slug,)).fetchone()
    con.close()
    if not row:
        raise HTTPException(404)
    title = row[0] or "我的电影史"
    stats = json.loads(row[1])
    films = int(stats.get("films") or 0)
    runtime = int(stats.get("runtime_minutes") or 0)
    runtime_known = int(stats.get("runtime_known") or 0)
    years = int(stats.get("watch_years") or (
        (stats.get("year_max") or 0) - (stats.get("year_min") or 0) + 1
        if stats.get("year_max") and stats.get("year_min") else 0
    ))
    fmt = lambda value: f"{int(value):,}" if value else "--"
    if runtime_known >= films and films:
        runtime_note = ""
    elif runtime_known:
        runtime_note = f"已统计 {runtime_known:,} 部"
    else:
        runtime_note = "片长待补全"
    return SHARE_TPL.replace("__TITLE_JS__", json.dumps(title, ensure_ascii=False)) \
        .replace("__TITLE__", html_lib.escape(title, quote=True)) \
        .replace("__TITLE_BLOCK__", _share_title_block(title)) \
        .replace("__FILMS__", fmt(films)) \
        .replace("__RUNTIME__", fmt(runtime)) \
        .replace("__RUNTIME_NOTE__", runtime_note) \
        .replace("__YEARS__", fmt(years)) \
        .replace("__SLUG__", slug)

@app.get("/sky/{slug}/share-card.svg")
def share_card(slug: str):
    con = db()
    row = con.execute("SELECT title,sky_json,stats_json FROM skies WHERE slug=?", (slug,)).fetchone()
    con.close()
    if not row:
        raise HTTPException(404)
    svg = _share_card_svg(json.loads(row["sky_json"]), row["title"] or "我的电影史", json.loads(row["stats_json"]))
    return Response(svg, media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=3600"})

@app.get("/sky/{slug}/poster.svg")
def poster(slug: str):
    con = db()
    row = con.execute("SELECT title,sky_json,stats_json FROM skies WHERE slug=?", (slug,)).fetchone()
    con.close()
    if not row:
        raise HTTPException(404)
    svg = poster_svg(json.loads(row[1]), row[0] or "我的电影史", json.loads(row[2])["films"])
    return Response(svg, media_type="image/svg+xml")

@app.get("/api/poster")
def movie_poster(title: str, year: int, m: int = 0):
    if m < 3:
        return {"ok": False, "reason": "below-threshold"}
    key = _poster_key(title, year)
    local_image_url = "/api/poster-image?" + urllib.parse.urlencode({"title": title, "year": year, "m": m})
    con = db()
    cached = con.execute("SELECT poster_url,status FROM poster_cache WHERE key=?", (key,)).fetchone()
    con.close()
    if cached and cached["status"] in ("ok", "missing-subject", "missing-poster"):
        ok = cached["status"] == "ok" and bool(cached["poster_url"])
        return {
            "ok": ok,
            "poster_url": cached["poster_url"] or "",
            "image_url": local_image_url if ok else "",
            "cached": True,
        }

    cached_poster = _find_cached_poster_url(title, year)
    if cached_poster:
        subject_url = _find_subject_url(title, year)
        _cache_poster(title, year, cached_poster, subject_url, "ok")
        return {
            "ok": True,
            "poster_url": cached_poster,
            "image_url": local_image_url,
            "cached": False,
        }

    subject_url = _find_subject_url(title, year)
    if not subject_url:
        _cache_poster(title, year, "", "", "missing-subject")
        return {"ok": False, "reason": "missing-subject"}
    try:
        poster_url = _fetch_douban_poster(subject_url)
    except Exception:
        _cache_poster(title, year, "", subject_url, "failed")
        return {"ok": False, "reason": "fetch-failed"}
    if not poster_url:
        _cache_poster(title, year, "", subject_url, "missing-poster")
        return {"ok": False, "reason": "missing-poster"}
    _cache_poster(title, year, poster_url, subject_url, "ok")
    return {
        "ok": True,
        "poster_url": poster_url,
        "image_url": local_image_url,
        "cached": False,
    }

@app.get("/api/poster-image")
def poster_image(title: str, year: int, m: int = 0):
    if m < 3:
        raise HTTPException(404)
    key = _poster_key(title, year)
    con = db()
    row = con.execute("SELECT poster_url FROM poster_cache WHERE key=? AND status='ok'", (key,)).fetchone()
    con.close()
    if not row or not row["poster_url"]:
        raise HTTPException(404)
    try:
        response = requests.get(
            row["poster_url"],
            headers={"User-Agent": HEADERS["User-Agent"], "Referer": "https://movie.douban.com/"},
            timeout=15,
        )
        response.raise_for_status()
    except Exception:
        raise HTTPException(404)
    content_type = response.headers.get("content-type") or "image/jpeg"
    if not content_type.startswith("image/"):
        raise HTTPException(404)
    return Response(
        response.content,
        media_type=content_type,
        headers={"Cache-Control": "public, max-age=86400"},
    )

@app.post("/api/skies/{slug}/reimport")
async def reimport(slug: str, file: UploadFile, token: str = Form(...)):
    con = db()
    row = con.execute("SELECT edit_token FROM skies WHERE slug=?", (slug,)).fetchone()
    if not row:
        con.close(); raise HTTPException(404)
    if not secrets.compare_digest(row[0], token):
        con.close(); raise HTTPException(403, "编辑令牌不符")
    films = parse_csv(await file.read())
    if len(films) < 3:
        con.close(); raise HTTPException(400, "CSV解析后不足3部影片")
    sky, stats = build_sky(films, ED)
    con.execute("UPDATE skies SET sky_json=?, stats_json=?, updated_at=? WHERE slug=?",
                (json.dumps(sky, ensure_ascii=False), json.dumps(stats, ensure_ascii=False),
                 datetime.datetime.utcnow().isoformat(), slug))
    con.commit(); con.close()
    return {"ok": True, "slug": slug, "films": stats["films"]}

@app.delete("/api/skies/{slug}")
def delete_sky(slug: str, token: str):
    con = db()
    row = con.execute("SELECT edit_token FROM skies WHERE slug=?", (slug,)).fetchone()
    if not row:
        con.close(); raise HTTPException(404)
    if not secrets.compare_digest(row[0], token):
        con.close(); raise HTTPException(403, "编辑令牌不符")
    con.execute("DELETE FROM skies WHERE slug=?", (slug,))
    con.commit(); con.close()
    return {"deleted": True}
