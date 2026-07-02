# -*- coding: utf-8 -*-
"""每个人的电影史 · 数据管线
CSV(电影名/年份/国别) → 匹配编辑库 → 天空数据(nm/lines/dots) → 海报SVG
铁律:天空 = f(片单, 编辑库),纯函数,不掺随机。"""
import csv, io, re, math, sqlite3, json
from collections import defaultdict
from regions import band_of, REGIONS, CONTINENTS, IDX, REGION_NAMES

# ---------- 标准化与确定性哈希 ----------
_PUNCT = re.compile(r"[\s·・,,。.::;;!!??“”\"'《》〈〉()()\[\]【】\-—\~~/\\]+")

def norm(t: str) -> str:
    return _PUNCT.sub("", (t or "")).lower()

def hash_a(t: str) -> float:
    """复刻前端 hashA(FNV-1a, UTF-16码元, Math.imul语义),保证星与海报同位。"""
    h = 2166136261
    b = t.encode("utf-16-le")
    for i in range(0, len(b), 2):
        h = (h ^ (b[i] | (b[i + 1] << 8))) & 0xFFFFFFFF
        h = (h * 16777619) & 0xFFFFFFFF
    return h / 4294967296.0

# ---------- 解析爬虫CSV ----------
def parse_csv(data: bytes):
    text = data.decode("utf-8-sig", errors="replace")
    rows = list(csv.reader(io.StringIO(text)))
    films, seen = [], set()
    for r in rows[1:]:
        if len(r) < 3 or not r[0].strip():
            continue
        title = r[0].split(" / ")[0].strip() or r[0].strip()
        ystr = re.sub(r"\D", "", r[1])
        if not ystr:
            continue
        y = max(1895, min(2026, int(ystr)))
        key = (title, y)
        if key in seen:
            continue
        seen.add(key)
        runtime = None
        if len(r) >= 4:
            m = re.search(r"\d+", r[3] or "")
            if m:
                runtime = int(m.group(0))
        genres = r[4].strip() if len(r) >= 5 else ""
        film = {"t": title, "y": y, "s": band_of(r[2]), "c": r[2].strip()}
        if runtime:
            film["rt"] = runtime
        if genres:
            film["g"] = genres.replace("/", " ").split()
        films.append(film)
    return films

# ---------- 编辑库 ----------
class Editorial:
    def __init__(self, path="editorial.db"):
        con = sqlite3.connect(path)
        cur = con.cursor()
        # 正典:同一(片名,年份)可入多张清单 → 星等 = 去重后的清单数
        self.canon = defaultdict(set)   # norm(title) -> {(year, list_id)}
        for v, lid, t, y in cur.execute(
            "SELECT e.version_id, v.list_id, e.title_zh, e.year FROM canon_entries e "
            "JOIN canon_versions v ON v.id=e.version_id"):
            if t and y:
                self.canon[norm(t)].add((int(y), lid))
        # 运动:连线类别 + 是否进行中
        self.movements = []
        _MV_BAND = {"中国": "cn", "香港": "hk", "台湾": "tw", "日本": "jp", "韩国": "kr",
                    "法国": "fr", "德国": "de", "西德": "de", "丹麦": "nord", "意大利": "it",
                    "英国": "uk", "波兰": "ee", "捷克斯洛伐克": "ee", "希腊": "ee",
                    "罗马尼亚": "ee", "苏联": "su", "巴西": "la", "拉丁美洲": "la",
                    "伊朗": "ir", "美国": "us", "澳大利亚": "oc", "泰国": "sea"}
        _movs = cur.execute(
            "SELECT id, name_zh, end_year, is_linked, region_zh FROM movements").fetchall()
        for mid, name, end, linked, reg in _movs:
            if not linked:
                continue
            fl = cur.execute(
                "SELECT film_title_zh, year FROM movement_films WHERE movement_id=? ORDER BY ord",
                (mid,)).fetchall()
            band = IDX[_MV_BAND[reg]] if reg in _MV_BAND else None
            self.movements.append({"id": mid, "n": name, "open": end is None,
                                   "band": band, "films": [(t, int(y)) for t, y in fl]})
        # 样例正典的弧带(films实体层就位前的临时表,仅服务于邀请)
        _CB = {"让娜·迪尔曼": "de", "迷魂记": "us", "公民凯恩": "us", "东京物语": "jp",
               "花样年华": "hk", "2001太空漫游": "us", "军中禁恋": "fr", "穆赫兰道": "us",
               "持摄影机的人": "su", "雨中曲": "us", "贪婪": "us", "游戏规则": "fr",
               "七武士": "jp", "浮云": "jp", "小城之春": "cn", "肖申克的救赎": "us",
               "霸王别姬": "cn", "教父": "us", "低俗小说": "us", "寄生虫": "kr",
               "盗梦空间": "us"}
        self.canon_band = {t: IDX[b] for t, b in _CB.items()}
        con.close()

    def magnitude(self, t, y):
        hits = {lid for (yy, lid) in self.canon.get(norm(t), ()) if abs(yy - y) <= 1}
        return len(hits)

# ---------- 天空构建 ----------
def build_sky(films, ed: Editorial):
    by_norm = defaultdict(list)
    for f in films:
        f["m"] = ed.magnitude(f["t"], f["y"])
        by_norm[norm(f["t"])].append(f)

    def find_user(t, y):
        for f in by_norm.get(norm(t), ()):
            if abs(f["y"] - y) <= 1:
                return f
        return None

    # 星座:运动中被用户点亮≥2颗 → 成线;按成员数取前8
    lines, gaps = [], []
    for mv in ed.movements:
        hit, miss = [], []
        for (t, y) in mv["films"]:
            u = find_user(t, y)
            (hit if u else miss).append(u or (t, y))
        if len(hit) >= 2:
            hit.sort(key=lambda f: (f["y"], f["t"]))
            ln = {"g": [f["t"] for f in hit], "n": mv["n"]}
            if mv["open"]:
                ln["open"] = 1
            lines.append((len(hit), ln))
            if miss:
                gaps.append({"mv": mv, "hit": hit, "miss": miss})
    lines.sort(key=lambda x: (-x[0], x[1]["n"]))
    lines = [ln for _, ln in lines[:8]]

    # 邀请(≤3,全部由数据推得,文案保持白话)
    watched_norms = {norm(f["t"]) for f in films}
    unseen = []
    for title, sec in ed.canon_band.items():
        if norm(title) in watched_norms:
            continue
        pairs = ed.canon.get(norm(title), set())
        if not pairs:
            continue
        y = min(yy for yy, _ in pairs)
        unseen.append({"t": title, "y": y, "s": sec, "m": len({l for _, l in pairs})})

    invs, dots, used = [], [], set()
    sec_count = [0] * len(REGIONS)
    for f in films:
        sec_count[f["s"]] += 1
    # ①最空扇区里星等最高的未见之作
    for sec in sorted(range(len(REGIONS)), key=lambda k: sec_count[k]):
        cand = sorted((u for u in unseen if u["s"] == sec), key=lambda u: (-u["m"], u["y"]))
        if cand:
            c = dict(cand[0]); c["note"] = "你最空的星区:" + REGION_NAMES[sec]
            invs.append(c); used.add(c["t"]); break
    # ②成员最多却有缺口的星座 → 缺口成星,虚线连最近一颗
    for g in sorted(gaps, key=lambda g: -len(g["hit"])):
        sec = g["mv"]["band"]
        if sec is None:
            continue
        mt, my = min(g["miss"], key=lambda p: p[1])
        if mt in used:
            continue
        near = min(g["hit"], key=lambda f: abs(f["y"] - my))
        invs.append({"t": mt, "y": my, "s": sec, "m": 0,
                     "note": "「" + g["mv"]["n"] + "」的缺口"})
        dots.append([near["t"], mt]); used.add(mt); break
    # ③全局星等最高的未见之作
    for u in sorted(unseen, key=lambda u: (-u["m"], u["y"])):
        if u["t"] in used:
            continue
        c = dict(u); c["note"] = "入选 %d 张清单" % u["m"]
        invs.append(c); break

    nm = [{"t": f["t"], "y": f["y"], "s": f["s"], "m": f["m"],
           **({"rt": f["rt"]} if f.get("rt") else {}),
           **({"g": f["g"]} if f.get("g") else {})} for f in films]
    for c in invs:
        nm.append({"t": c["t"], "y": c["y"], "s": c["s"], "m": 0, "inv": 1, "note": c["note"]})
    stats = {
        "films": len(films),
        "runtime_minutes": sum(int(f.get("rt") or 0) for f in films),
        "runtime_known": sum(1 for f in films if f.get("rt")),
        "watch_years": len({f["y"] for f in films}),
        "region_count": len({f["s"] for f in films}),
        "bands": {REGION_NAMES[i]: sec_count[i] for i in range(len(REGIONS)) if sec_count[i]},
        "continents": {CONTINENTS[ci]: sum(sec_count[i] for i, r in enumerate(REGIONS) if r["c"] == ci) for ci in range(5)},
        "lit": sum(1 for f in films if f["m"] > 0),
        "constellations": [l["n"] for l in lines],
        "invitations": [c["t"] for c in invs],
        "year_min": min(f["y"] for f in films), "year_max": max(f["y"] for f in films),
    }
    return {"nm": nm, "lines": lines, "dots": dots}, stats

# ---------- 海报SVG(与前端同一套几何) ----------
Y0, Y1, RW, RIN = 1895, 2026, 150, 9
EDGE_SOFT, EDGE_INNER, EDGE_DRIFT = .012, .976, .035

def _pos(entries):
    for d in entries:
        d["aa"] = EDGE_SOFT + hash_a(d["t"]) * EDGE_INNER
        d["rf"] = .12 + .76 * hash_a("\u0001" + d["t"])
        d["jj"] = hash_a("\u0002" + d["t"]) - .5
        d["ed"] = hash_a("\u0003" + d["t"]) - .5
    grp = defaultdict(list)
    for d in entries:
        grp[(d["s"], d["y"])].append(d)
    for g in grp.values():
        if len(g) < 2:
            continue
        g.sort(key=lambda d: (d["aa"], d["t"]))
        n = len(g)
        gap = min(.062, EDGE_INNER / (n - 1))
        for i in range(1, n):
            if g[i]["aa"] < g[i - 1]["aa"] + gap:
                g[i]["aa"] = g[i - 1]["aa"] + gap
        if g[0]["aa"] < EDGE_SOFT:
            sh = EDGE_SOFT - g[0]["aa"]
            for d in g:
                d["aa"] += sh
        if g[-1]["aa"] > 1 - EDGE_SOFT:
            a0 = g[0]["aa"]
            sc = EDGE_INNER / (g[-1]["aa"] - a0) if g[-1]["aa"] > a0 else 0
            for d in g:
                d["aa"] = EDGE_SOFT + (d["aa"] - a0) * sc
        for d in g:
            d["aa"] = max(-EDGE_SOFT, min(1 + EDGE_SOFT, d["aa"] + d["jj"] * gap * .55 + d["ed"] * EDGE_DRIFT))
    for d in entries:
        ye = min(d["y"] + d["rf"], Y1 - .05)
        r = RIN + (ye - Y0) / (Y1 - Y0) * (RW - RIN)
        rg = REGIONS[d["s"]]
        a = math.radians(rg["a0"] + d["aa"] * (rg["a1"] - rg["a0"]) - 90)
        d["x"], d["yy"] = r * math.cos(a), r * math.sin(a)

def _star(x, y, R):
    w = R * .27
    p = [(x, y - R), (x + w, y - w), (x + R, y), (x + w, y + w),
         (x, y + R), (x - w, y + w), (x - R, y), (x - w, y - w)]
    return " ".join("%.1f,%.1f" % q for q in p)

def poster_svg(sky, title, count):
    W, H, cx, cy, S = 800, 1000, 400, 470, 2.2
    nm = [dict(d) for d in sky["nm"]]
    _pos(nm)
    by_t = {d["t"]: d for d in nm}
    def T(d): return cx + d["x"] * S, cy + d["yy"] * S
    o = ['<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d">' % (W, H, W, H),
         '<rect width="%d" height="%d" fill="#060F22"/>' % (W, H)]
    for y in range(1905, 2026, 10):
        r = (RIN + (y - Y0) / (Y1 - Y0) * (RW - RIN)) * S
        o.append('<circle cx="%d" cy="%d" r="%.1f" fill="none" stroke="rgba(201,168,106,.08)" stroke-width="1"/>' % (cx, cy, r))
    o.append('<circle cx="%d" cy="%d" r="%.1f" fill="none" stroke="rgba(201,168,106,.25)" stroke-width="1.2"/>' % (cx, cy, RW * S))
    # 年份刻度(与前端一致:1895/1930/1960/2000,字号14)
    for yr in (1930, 1960, 2000):
        yr_r = (RIN + (yr - Y0) / (Y1 - Y0) * (RW - RIN)) * S
        o.append('<text x="%.1f" y="%.1f" font-size="14" fill="rgba(150,160,170,.30)" font-family="Songti SC,Noto Serif SC,serif">%d</text>' % (cx + 10, cy - yr_r + 5, yr))
    o.append('<text x="%d" y="%d" font-size="14" fill="rgba(150,160,170,.30)" font-family="Songti SC,Noto Serif SC,serif">1895</text>' % (cx + 8, cy - 6))
    spans = []
    for ci in range(5):
        bs = [r for r in REGIONS if r["c"] == ci]
        spans.append((min(r["a0"] for r in bs), max(r["a1"] for r in bs)))
    # 标签放置:大洲+国家/地区, 处理底部非洲/大洋洲重叠与左侧欧洲拥挤
    bc = [0] * len(REGIONS)
    for d in nm:
        if not d.get("inv"):
            bc[d["s"]] += 1

    # 欧洲区域索引 (fr,it,de,su,uk,nord,ee,ib)
    EUROPE = {11, 12, 13, 14, 15, 16, 17, 18}
    # 非洲、大洋洲 (底部)
    BOTTOM = {9, 10}

    def _place(text, angle, fs, base_r, x_offset=0, y_offset=0, alpha=.55):
        r = base_r
        a = math.radians(angle)
        x = cx + r * S * math.cos(a) + x_offset
        y = cy + r * S * math.sin(a) + y_offset
        if abs(x - cx) < 30:
            anchor = "middle"
        elif x > cx:
            anchor = "start"
        else:
            anchor = "end"
        letter = 3 if fs >= 15 else 1
        return '<text x="%.1f" y="%.1f" text-anchor="%s" font-size="%d" letter-spacing="%d" fill="rgba(201,168,106,%.2f)" font-family="Songti SC,Noto Serif SC,serif">%s</text>' % (x, y, anchor, fs, letter, alpha, text)

    # 大洲标签
    for ci, (a0, a1) in enumerate(spans):
        text = CONTINENTS[ci]
        angle = (a0 + a1) / 2 - 90
        x_offset = 0
        y_offset = 0
        base_r = RW + 8
        if text == "非洲":
            x_offset = 18
            base_r = RW + 12
        elif text == "大洋洲":
            x_offset = -12
            y_offset = 28
            base_r = RW + 13
        elif text == "欧洲":
            x_offset = -18
            y_offset = 18
            base_r = RW + 14
        o.append(_place(text, angle, 15, base_r, x_offset, y_offset, .42))

    # 国家/地区标签(只显示有星的), 欧洲外扩、底部外扩并水平错开
    for kb in sorted(range(len(REGIONS)), key=lambda k: (-bc[k], k)):
        if bc[kb] == 0:
            continue
        rg = REGIONS[kb]
        text = rg["n"]
        if text in CONTINENTS:
            continue
        angle = (rg["a0"] + rg["a1"]) / 2 - 90
        if kb in EUROPE:
            r = RW + 5  # 欧洲外扩,缓解左侧拥挤
        elif kb in BOTTOM:
            r = RW + 2  # 非洲/大洋洲外扩
        else:
            r = RW + 2
        x_offset = 0
        if text == "非洲":
            x_offset = 12
        elif text == "大洋洲":
            x_offset = -12
        elif text == "意大利":
            x_offset = -18
        elif text == "西亚":
            x_offset = 12
        o.append(_place(text, angle, 11, r, x_offset))
    for ln in sky["lines"]:
        pts = [T(by_t[t]) for t in ln["g"] if t in by_t]
        if len(pts) >= 2:
            o.append('<path d="M' + " L".join("%.1f %.1f" % p for p in pts) +
                     '" fill="none" stroke="rgba(242,216,143,.35)" stroke-width="1"/>')
        if ln.get("open") and pts:
            last = by_t[ln["g"][-1]]
            r0 = math.hypot(last["x"], last["yy"])
            ux, uy = last["x"] / r0, last["yy"] / r0
            o.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="rgba(242,216,143,.25)" stroke-width="1" stroke-dasharray="3 5"/>'
                     % (cx + last["x"] * S, cy + last["yy"] * S,
                        cx + ux * (RW + 12) * S, cy + uy * (RW + 12) * S))
    for pr in sky["dots"]:
        if pr[0] in by_t and pr[1] in by_t:
            p, q = T(by_t[pr[0]]), T(by_t[pr[1]])
            o.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="rgba(242,216,143,.3)" stroke-width="1" stroke-dasharray="3 4"/>' % (p + q))
    for d in nm:
        x, y = T(d)
        if d.get("inv"):
            o.append('<polygon points="%s" fill="none" stroke="rgba(242,216,143,.3)" stroke-width="1"/>' % _star(x, y, 7))
            continue
        m = d["m"]
        if m >= 4:
            R = 13 if m >= 6 else (11.5 if m == 5 else 9.5)
            o.append('<circle cx="%.1f" cy="%.1f" r="%.1f" fill="rgba(242,216,143,.12)"/>' % (x, y, R + 6))
            o.append('<polygon points="%s" fill="#F2D88F"/>' % _star(x, y, R))
        elif m == 3:
            o.append('<polygon points="%s" fill="rgba(242,216,143,.95)"/>' % _star(x, y, 7))
        elif m == 2:
            o.append('<polygon points="%s" fill="rgba(217,194,138,.9)"/>' % _star(x, y, 5.2))
        elif m == 1:
            o.append('<polygon points="%s" fill="rgba(217,194,138,.85)"/>' % _star(x, y, 4))
        else:
            o.append('<polygon points="%s" fill="rgba(217,194,138,.8)"/>' % _star(x, y, 3.2))
    o.append('<circle cx="%d" cy="%d" r="2.5" fill="#F2D88F"/>' % (cx, cy))
    o.append("</svg>")
    return "\n".join(o)
