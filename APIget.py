import os, csv, requests
import time
import sqlite3
con = sqlite3.connect("banks.db")
cursor = con.cursor()

from dotenv import load_dotenv
load_dotenv()#加载同目录下.env文件
cursor.execute('''
    CREATE TABLE IF NOT EXISTS banks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        address TEXT,
        lng REAL,
        lat REAL,
        tel TEXT,
        type TEXT
    )
''')

KEY = os.getenv("AMAP_KEY"," ")
URL = "https://restapi.amap.com/v5/place/text"
KEY = KEY.strip()

params = {
    "key": KEY,
    "keywords": "银行",
    "types": "160100",        # 160100=银行  120300=住宅区
    "region": "深圳",
    "citylimit": "true",
    "page_size": 25,
    "page_num": 1,
    "extensions": "all",
    "output": "JSON",
}
page = 1
rows = []
while True:
    params["page_num"] = page
    r = requests.get(URL, params=params, timeout=10).json()
    pois = r.get("pois",[])

    if not pois:
        break
    for p in pois:
        lng, lat = (p.get("location") or ",").split(",")
        rows.append((p.get("name"), p.get("address"),
                    lng, lat,p.get("tel"), p.get("type")))
    print(f"已检索{page}页")
    page += 1
    time.sleep(1)

rows = set(rows)
rows = list(rows)#抓取顺序不重要，直接set去重
if rows:
    cursor.executemany("INSERT INTO banks (name, address, lng, lat,tel,type) VALUES (?, ?, ?, ?, ?,?)", rows)
    con.commit()
    con.close()
    print(f"已保存 {len(rows)} 条到 banks.db")