import os, csv, requests
from dotenv import load_dotenv
load_dotenv()#加载同目录下.env文件

KEY = os.getenv("AMAP_KEY"," ")
URL = "https://restapi.amap.com/v5/place/text"
print(repr(KEY))

params = {
    "key": KEY,
    "keywords": "银行",
    "types": "160100",        # 160100=银行  120300=住宅区
    "city": "深圳",
    "citylimit": "true",
    "offset": 25,
    "page": 1,
    "extensions": "all",
    "output": "JSON",
}

r = requests.get(URL, params=params, timeout=10).json()
print("status:", r.get("status"), "| infocode:", r.get("infocode"), "| count:", r.get("count"))

rows = []
for p in r.get("pois", []):
    lng, lat = (p.get("location") or ",").split(",")
    rows.append({"name": p.get("name"), "address": p.get("address"),
                 "lng": lng, "lat": lat, "tel": p.get("tel"), "type": p.get("type")})

for x in rows[:5]:
    print(x)

if rows:
    with open("banks.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=rows[0].keys())
        w.writeheader(); w.writerows(rows)
    print(f"已保存 {len(rows)} 条到 banks.csv")