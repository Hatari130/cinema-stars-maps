# 每个人的电影史 · 后端 v0.1

匿名星空服务:输入豆瓣ID→后台抓取公开观影页→生成可分享的电影史星空。
CSV上传仍作为豆瓣反爬或私密主页时的备用导入方式。

## 运行
```
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8000
```
打开 http://localhost:8000 即是豆瓣ID输入页。

## 架构铁律
天空 = f(片单, 编辑库),纯函数:同一份CSV + 同一版编辑库 → 永远同一片天空。
星等、星座、邀请均为查询结果,不落库;数据库只存片单与编辑库。

## 文件
- app.py            FastAPI:落地页 / 豆瓣后台任务 / 生成进度页 / CSV备用导入 / 渲染 / 海报 / 覆盖式重导入 / 真删除
- scrape_douban_collect.py 豆瓣公开观影页抓取器(电影名/年份/国别)
- templates/build.html 生成进度页(轮询任务进度,完成后跳转星空)
- pipeline.py       片单解析 → 扇区映射 → 编辑库匹配 → 天空JSON → 海报SVG
- regions.py        定址制地理:21条弧带(文化相邻排序)+五大洲;国别→弧带映射
                    (规则不变:取国别字段第一个国家)。弧带宽度=权重表 _BANDS,
                    微调权重后重启服务即全站生效(模板渲染时注入,无需重建前端)
- editorial.db      编辑库编译产物(由 editorial-library.xlsx 经 build_db.py 生成)
- templates/sky.html 前端模板(7个注入点:__NM__/__LINES__/__DOTS__/__TITLE__/__COUNT__)
- app.db            运行时生成(skies表)

## API
- POST /api/jobs/douban           form: douban_id, title? → 创建后台任务 → 303跳转 /build/{job_id}
- POST /api/skies/douban          兼容旧路径,行为同 /api/jobs/douban
- GET  /build/{job_id}            生成进度页
- GET  /api/jobs/{job_id}.json    任务进度与最终星空URL
- POST /api/jobs/{job_id}/resume  对 partial/failed 豆瓣任务继续补齐
- POST /api/skies                 multipart: file=CSV, title? → CSV备用导入 → 303跳转 /sky/{slug}?token=...
- GET  /sky/{slug}                星空页(公开)
- GET  /sky/{slug}/poster.svg     分享海报(服务端渲染,与页面同一套几何与哈希)
- GET  /api/skies/{slug}.json     数据与统计
- POST /api/skies/{slug}/reimport multipart: file, token → 覆盖(同一slug,天空生长)
- DELETE /api/skies/{slug}?token= 真删除

## 身份模型
无账号。创建返回一次性展示的 edit_token,凭token可重导入/删除;丢失即放弃编辑权。

## 正典清单导入(点亮星等的唯一开关)
任何清单整理成三列CSV(rank,title_zh,year),一条命令入库,星等全量自动重算:
```
python3 import_canon.py --version douban-snapshot --csv douban250.csv --dated 2026-06-12
```
六个版本ID已注册:ss-2022-critics / ss-2022-directors / rosenbaum-2004 /
kinejun-alltime-2009 / hkfa-2005 / douban-snapshot / letterboxd-snapshot。
豆瓣250与Letterboxd250用你的爬虫抓快照;视与听/旬报/金像/罗森鲍姆从官方出版物手工整理。
同一版本重复导入即覆盖(正典是连载,版本带日期)。

## 已知边界(按优先级)
1. 星等覆盖率受限:editorial.db 中正典条目目前仅22条样例,故 m>0 的星很少。
   六张清单全量导入后(canon_entries同结构),星等自动恢复满血,代码零改动。
2. 匹配键是「中文名+年份±1」:爬虫若能加一列豆瓣条目ID,匹配即升级为主键连接。
   同片异译靠待建的别名表兜底(编辑库待决问题⑨)。
3. 豆瓣公开页可能触发反爬或被用户设为不可见。后台任务会缓存已读分页,中途触发反爬时先生成 partial 星空,之后可继续补齐;若要全量且稳定,用CSV备用导入。
4. 当前后台任务使用 FastAPI BackgroundTasks + SQLite 起步;公开部署时建议迁移到独立队列(worker)与Postgres。
5. SQLite单文件起步;迁移Postgres时仅 app.py 的连接层需改。
6. 海报为SVG;微信内分享若需PNG,后续加 resvg/Playwright 转栅格。
