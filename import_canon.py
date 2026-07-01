# -*- coding: utf-8 -*-
"""正典清单导入器。
任何一张清单整理成CSV(rank,title_zh,year;无排名清单rank留空)即可入库:
  python3 import_canon.py --db editorial.db --version douban-snapshot --csv douban250.csv --dated 2026-06-12
导入后星等自动随之重算(天空=纯函数),后端代码零改动。
version 必须已在 canon_versions 注册(编辑库xlsx里维护),本工具只灌条目。"""
import argparse, csv, sqlite3, sys, io

p = argparse.ArgumentParser()
p.add_argument("--db", default="editorial.db")
p.add_argument("--version", required=True, help="canon_versions.id,如 douban-snapshot / ss-2022-critics")
p.add_argument("--csv", required=True, help="三列CSV: rank,title_zh,year(首行为表头)")
p.add_argument("--dated", default="", help="快照/版本日期,写回canon_versions.dated")
a = p.parse_args()

con = sqlite3.connect(a.db)
cur = con.cursor()
row = cur.execute("SELECT list_id,label FROM canon_versions WHERE id=?", (a.version,)).fetchone()
if not row:
    sys.exit("版本未注册: %s(先在编辑库xlsx的canon_versions里登记,再build_db.py)" % a.version)

rows = list(csv.reader(io.open(a.csv, encoding="utf-8-sig")))
entries, seen, bad = [], set(), []
for r in rows[1:]:
    if len(r) < 3 or not r[1].strip():
        continue
    rank = int(r[0]) if r[0].strip().isdigit() else None
    title, ystr = r[1].strip(), r[2].strip()
    if not ystr.isdigit():
        bad.append(r); continue
    key = (title, int(ystr))
    if key in seen:
        bad.append(["重复"] + r); continue
    seen.add(key)
    entries.append((a.version, rank, title, int(ystr), 0, ""))
if not entries:
    sys.exit("CSV无有效条目")

cur.execute("DELETE FROM canon_entries WHERE version_id=?", (a.version,))
cur.executemany("INSERT INTO canon_entries VALUES(?,?,?,?,?,?)", entries)
if a.dated:
    cur.execute("UPDATE canon_versions SET dated=? WHERE id=?", (a.dated, a.version))
con.commit()

ys = [e[3] for e in entries]
print("版本 %s(清单:%s)导入 %d 条,年份 %d–%d,覆盖旧条目" % (a.version, row[0], len(entries), min(ys), max(ys)))
if bad:
    print("跳过 %d 条问题行:" % len(bad))
    for b in bad[:8]:
        print("   ", b)
total = cur.execute("SELECT COUNT(*) FROM canon_entries").fetchone()[0]
print("库内正典条目总数: %d" % total)
con.close()
