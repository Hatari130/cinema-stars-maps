# -*- coding: utf-8 -*-
"""每个人的电影史 · 后端
匿名模型:每片天空 = 公开slug + 私密edit_token(链接即身份)。
覆盖语义:重新导入 = 同一slug,天空生长。删除 = 真删。"""
import json, os, random, re, secrets, sqlite3, datetime, time
from fastapi import BackgroundTasks, FastAPI, UploadFile, Form, HTTPException
from fastapi.responses import HTMLResponse, Response, JSONResponse, RedirectResponse
from pipeline import parse_csv, build_sky, poster_svg, Editorial
from regions import REGIONS, CONTINENTS, band_of
from scrape_douban_collect import (
    BASE_URL,
    HEADERS,
    PAGE_SIZE,
    MovieRow,
    extract_total,
    parse_page,
    request_html,
)
import requests

BASE = os.path.dirname(os.path.abspath(__file__))
APP_DB = os.path.join(BASE, "app.db")
TPL = open(os.path.join(BASE, "templates", "sky.html"), encoding="utf-8").read()
BUILD_TPL = open(os.path.join(BASE, "templates", "build.html"), encoding="utf-8").read()
LANDING_TPL = open(os.path.join(BASE, "templates", "landing.html"), encoding="utf-8").read()
ED = Editorial(os.path.join(BASE, "editorial.db"))
DOUBAN_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
DOUBAN_DELAY_MIN = float(os.getenv("DOUBAN_DELAY_MIN", "3.0"))
DOUBAN_DELAY_MAX = float(os.getenv("DOUBAN_DELAY_MAX", "6.0"))
DOUBAN_COOLDOWN_MIN = float(os.getenv("DOUBAN_COOLDOWN_MIN", "45.0"))
DOUBAN_COOLDOWN_MAX = float(os.getenv("DOUBAN_COOLDOWN_MAX", "90.0"))

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
    return con

app = FastAPI(title="每个人的电影史 API")

LANDING = """<!DOCTYPE html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>每个人的电影史</title>
<style>
:root{--bg:#060F22;--gold:#F2D88F;--dim:#C9A86A;--ivory:#F0E6CB;--line:rgba(201,168,106,.34);--serif:"Songti SC","STSong","Noto Serif SC","SimSun",Georgia,serif}
*{box-sizing:border-box}
html,body{margin:0;min-height:100%;background:var(--bg);color:var(--dim);font-family:var(--serif)}
body{min-height:100vh;display:grid;place-items:center;overflow:hidden}
body:before{content:"";position:fixed;inset:-20%;background:
radial-gradient(circle at 50% 48%,rgba(242,216,143,.13) 0 1px,transparent 1.5px),
radial-gradient(circle at 28% 24%,rgba(217,194,138,.12) 0 1px,transparent 1.5px),
radial-gradient(circle at 72% 34%,rgba(217,194,138,.1) 0 1px,transparent 1.5px);
background-size:92px 92px,137px 137px,181px 181px;opacity:.55}
body:after{content:"";position:fixed;width:min(72vw,680px);aspect-ratio:1;border:1px solid rgba(201,168,106,.16);border-radius:50%;box-shadow:0 0 0 44px rgba(201,168,106,.025),0 0 0 92px rgba(201,168,106,.018);transform:translateY(-2vh);pointer-events:none}
main{position:relative;z-index:1;width:min(88vw,460px);text-align:center;padding:28px 0}
h1{margin:0;color:var(--gold);font-weight:400;letter-spacing:5px;font-size:24px}
.lead{margin:18px auto 0;font-size:13.5px;line-height:1.9;opacity:.86}
form{margin:26px auto 0}
.field{width:100%;height:48px;padding:0 16px;background:rgba(6,15,34,.68);border:1px solid var(--line);border-radius:8px;color:var(--ivory);font:15px var(--serif);text-align:center;outline:none}
.field::placeholder{color:rgba(240,230,203,.48)}
.field:focus{border-color:rgba(242,216,143,.72);box-shadow:0 0 0 3px rgba(242,216,143,.08)}
.title{margin-top:10px;height:42px;font-size:13px}
.submit{margin-top:20px;height:42px;padding:0 38px;border:1px solid rgba(201,168,106,.68);border-radius:999px;background:rgba(6,15,34,.34);color:var(--gold);font:14px var(--serif);letter-spacing:2px;cursor:pointer}
.submit:hover{background:rgba(242,216,143,.09)}
.submit[disabled]{cursor:wait;opacity:.7}
details{margin-top:19px;font-size:12px;opacity:.72}
summary{cursor:pointer;list-style:none}
summary::-webkit-details-marker{display:none}
.csv{margin-top:12px;padding-top:14px;border-top:1px solid rgba(201,168,106,.18)}
.csv input[type=file]{display:block;margin:0 auto 12px;color:var(--dim);font-size:12px;max-width:100%}
.foot{margin:24px 0 0;font-size:11px;opacity:.52;letter-spacing:.5px}
@media(max-width:520px){h1{font-size:20px;letter-spacing:3px}.lead{font-size:13px}.field{text-align:left}.submit{width:100%}}
</style>
<body>
<main>
<h1>每个人的电影史</h1>
<p class="lead">输入豆瓣 ID，生成你的电影史星空。</p>
<form method="post" action="/api/jobs/douban" id="doubanForm">
<input class="field" type="text" name="douban_id" inputmode="text" autocomplete="off" required
 placeholder="238593631 或豆瓣观影链接">
<input class="field title" type="text" name="title" maxlength="40" placeholder="给这片天空起个名(可不填)">
<button class="submit" type="submit">点亮星空</button>
</form>
<details>
<summary>使用 CSV 导入</summary>
<form class="csv" method="post" action="/api/skies" enctype="multipart/form-data">
<input type="file" name="file" accept=".csv" required>
<input class="field title" type="text" name="title" maxlength="40" placeholder="给这片天空起个名(可不填)">
<button class="submit" type="submit">点亮星空</button>
</form>
</details>
<p class="foot">匿名 · 无账号 · 链接即身份 · 可随时删除</p>
</main>
<script>
document.querySelectorAll("form").forEach(function(form){
  form.addEventListener("submit",function(){
    var btn=form.querySelector("button[type=submit]");
    if(btn){btn.disabled=true;btn.textContent="正在点亮";}
  });
});
</script>
</body></html>"""

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
        films.append({"t": title, "y": y, "s": band_of(country), "c": country})
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
    } for r in rows], ensure_ascii=False)

def _rows_from_json(text):
    return [MovieRow(**row) for row in json.loads(text)]

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

def _fetch_page(session, user_id, start, known_total=None):
    cached = _cached_page(user_id, start)
    if cached:
        return cached[0], cached[1], True
    html = request_html(session, _page_url(user_id, start), 20, 3, 1.0)
    total = known_total if known_total is not None else extract_total(html)
    rows = parse_page(html)
    _save_page(user_id, start, total, rows)
    return total, rows, False

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

def _run_douban_job(job_id):
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
    try:
        total, first_rows, from_cache = _fetch_page(session, user_id, 0)
        rows.extend(first_rows)
        _set_job_progress(
            job_id,
            total,
            rows,
            0,
            "读取缓存页" if from_cache else "正在翻阅第 1 页",
        )
        for start in range(PAGE_SIZE, total, PAGE_SIZE):
            cached = _cached_page(user_id, start)
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
                _, page_rows, _ = _fetch_page(session, user_id, start, total)
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
                    _, page_rows, _ = _fetch_page(session, user_id, start, total)
                except Exception as exc2:
                    _finish_douban_job(job_id, rows, total, True, str(exc2))
                    return
            rows.extend(page_rows)
            _set_job_progress(job_id, total, rows, start, f"已翻阅第 {page_no}/{page_count} 页")
        _finish_douban_job(job_id, rows, total, False)
    except Exception as exc:
        if len(rows) >= 3:
            _finish_douban_job(job_id, rows, total, True, str(exc))
        else:
            _update_job(job_id, status="failed", message="豆瓣抓取失败", error=str(exc)[:500])

@app.get("/", response_class=HTMLResponse)
def landing():
    return LANDING_TPL

def _render(row):
    slug, title, sky_json, stats_json = row
    sky = json.loads(sky_json)
    js = lambda o: json.dumps(o, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    reg_client = [{"n": r["n"], "c": r["c"], "a0": r["a0"], "a1": r["a1"]} for r in REGIONS]
    html = TPL.replace("__NM__", js(sky["nm"])) \
              .replace("__LINES__", js(sky["lines"])) \
              .replace("__DOTS__", js(sky["dots"])) \
              .replace("__REGIONS__", js(reg_client)) \
              .replace("__CONTINENTS__", js(CONTINENTS)) \
              .replace("__TITLE__", (title or "我的电影史").replace("<", "&lt;")) \
              .replace("__COUNT__", str(json.loads(stats_json)["films"]))
    return html

@app.post("/api/skies")
async def create_sky(file: UploadFile, title: str = Form("")):
    films = parse_csv(await file.read())
    if len(films) < 3:
        raise HTTPException(400, "CSV解析后不足3部影片,请检查格式(电影名/年份/国别)")
    sky, stats = build_sky(films, ED)
    return _store_sky(title, sky, stats)

@app.post("/api/jobs/douban")
@app.post("/api/skies/douban")
async def create_douban_job(
    background_tasks: BackgroundTasks,
    douban_id: str = Form(...),
    title: str = Form(""),
):
    user_id = _douban_user_id(douban_id)
    job_id = secrets.token_urlsafe(10)
    now = _now()
    con = db()
    con.execute(
        "INSERT INTO jobs(id,source,status,douban_id,title,message,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
        (job_id, "douban", "queued", user_id, title.strip()[:40], "等待观测台启动", now, now),
    )
    con.commit(); con.close()
    background_tasks.add_task(_run_douban_job, job_id)
    return RedirectResponse(url=f"/build/{job_id}", status_code=303)

@app.get("/build/{job_id}", response_class=HTMLResponse)
def build_page(job_id: str):
    if not _job(job_id):
        raise HTTPException(404, "任务不存在")
    return BUILD_TPL.replace("__JOB_ID__", job_id)

@app.get("/api/jobs/{job_id}.json")
def job_json(job_id: str):
    return JSONResponse(_job_payload(_job(job_id)))

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

@app.get("/sky/{slug}/poster.svg")
def poster(slug: str):
    con = db()
    row = con.execute("SELECT title,sky_json,stats_json FROM skies WHERE slug=?", (slug,)).fetchone()
    con.close()
    if not row:
        raise HTTPException(404)
    svg = poster_svg(json.loads(row[1]), row[0] or "我的电影史", json.loads(row[2])["films"])
    return Response(svg, media_type="image/svg+xml")

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
