import os, csv, requests
import time

from dotenv import load_dotenv
load_dotenv()#加载同目录下.env文件

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
    for p in r.get("pois", []):
        lng, lat = (p.get("location") or ",").split(",")
        rows.append({"name": p.get("name"), "address": p.get("address"),
                    "lng": lng, "lat": lat, "tel": p.get("tel"), "type": p.get("type")})
    print(f"已检索{page}页")
    page += 1
    time.sleep(1)

if rows:
    with open("banks.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=rows[0].keys())
        w.writeheader(); w.writerows(rows)
    print(f"已保存 {len(rows)} 条到 banks.csv")