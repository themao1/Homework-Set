# -*- coding: utf-8 -*-
"""
folium_exam.py — folium 打点学习/验证脚本（非正式功能代码）

用途：早期验证「高德瓦片 + GCJ-02 坐标」组合能否正常打点的小实验，
取库中前 10 条数据导出 test_map.html，用于排查坐标偏移问题。
正式的静态地图导出在 folium_map.py，交互查询在 bank_map_app.py。
"""

import folium
import sqlite3
import pandas as pd

conn = sqlite3.connect("banks.db")
df = pd.read_sql("SELECT * FROM banks LIMIT 10", conn)
conn.close()

m = folium.Map(
    location=[22.54, 114.05],
    zoom_start=12,
    tiles='http://webrd02.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=7&x={x}&y={y}&z={z}',
    attr='高德地图'
)

# 3. 暴力打点测试（不用自定义图标，就用默认的红色大头针）
for index, row in df.iterrows():
    try:
        lat = float(row['lat'])
        lng = float(row['lng'])
        folium.Marker(
            location=[lat, lng],
            popup=row['name']
        ).add_to(m)
    except Exception as e:
        print(f"第 {index} 行数据出错: {e}, lat={row['lat']}, lng={row['lng']}")

# 4. 保存
m.save("test_map.html")
print("✅ 测试地图已保存！")