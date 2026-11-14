import folium
import sqlite3
import pandas as pd

conn = sqlite3.connect("banks.db")
df = pd.read_sql("SELECT * FROM banks", conn)
conn.close()
print(df)

m = folium.Map(
    location=[22.54, 114.05],
    zoom_start=12,
    tiles='http://webrd02.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=7&x={x}&y={y}&z={z}',
    attr='高德地图'
)

for _,row in df.iterrows():
    folium.Marker(
        location=[row["lat"], row["lng"]],
        popup=f"{row['name']}<br>{row['address']}",   # 点击标记后弹出的内容，支持 HTML
    ).add_to(m)

m.save("banks_map.html")