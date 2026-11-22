# 小区 / 银行信息采集与查询系统

一个演示版的数据采集与查询系统：用高德开放平台 API 抓取**银行网点**和**小区**数据，
存入 SQLite，提供**地图式**与**列表式**两种查询方式，并支持用户在前端**反馈信息错误**、
由后台人工审核后回写修正。

本版是初版 demo，重点是把「采集 → 存储 → 查询 → 反馈 → 审核修正」这条业务闭环跑通，
并在结构上为「以后不只在深圳」「以后数据量更大」「以后接入新的数据类别」留好接口。

---

## 1. 快速开始

```bash
# 1) 安装依赖（建议用虚拟环境）
python -m venv .venv
.venv\Scripts\activate            # Windows
pip install -r requirements.txt

# 2) 配置高德 Key
copy .env.example .env            # 然后把 AMAP_KEY 改成你自己的 Key

# 3) 建表 / 升级库结构（幂等，重复执行安全）
python main.py db migrate

# 4) 抓数据（银行 160100、小区 120300；--pages 用于控制配额消耗）
python main.py scrape --target all --city 深圳 --pages 5

# 5) 启动前端
python main.py app                # 浏览器访问 http://localhost:8501
```

> 库文件默认是同目录的 `banks.db`，可用环境变量 `BANKS_DB` 指到别处（部署/测试用）。

---

## 2. 目录结构

| 文件 | 职责 | 关键约束 |
|---|---|---|
| `main.py` | **程序统一入口**：启动前端、抓取、导出地图、看库、审核反馈 | 只做参数解析与调度 |
| `config.py` | 全局配置 + **数据图层注册表**（`LAYERS`） | 所有可变值集中在此，支持环境变量覆盖 |
| `SQLOperation.py` | **唯一的数据库访问层**：建表、迁移、UPSERT、查询、反馈、审核回写 | **只有这个文件 `import sqlite3`、只有这里写 SQL** |
| `DataGet.py` | 高德 POI 抓取 → 清洗 → 入库 | 不写 SQL，全部委托 `SQLOperation` |
| `bank_map_app.py` | Streamlit 前端：地图查询 / 列表查询 / 信息反馈 | 不写 SQL，全部调用 `SQLOperation` |
| `folium_map.py` | 导出静态地图 HTML（可离线打开、可放进报告） | 不写 SQL |
| `map_assets.py` | 地图前端资源本地化：leaflet 走本地 `libs/`，标记点用内联 SVG 图钉 | 不依赖公网 CDN |
| `banks.db` | SQLite 数据库（表结构 v3） | 由 `SQLOperation` 建表与迁移 |

---

## 3. 功能说明

### 3.1 地图查询
- 侧边栏切换**数据类型**（银行网点 / 小区）、关键词、类型、城市、周边半径；
- 点击标记看详情（名称、地址、坐标、电话、类型、城市/区县、来源与更新时间）；
- 点击地图空白处，按半径查周边同类数据（距离由近到远排列）。

### 3.2 列表查询
- 与地图共用筛选条件，避免同一套筛选填两遍；
- 支持排序（录入顺序 / 名称 / 最近更新）、升降序、每页条数、翻页；
- 一键导出当前页 CSV（带 BOM，Excel 打开中文不乱码）。

### 3.3 信息反馈（前端 → `feed_back` → 人工审核）
1. 用户在地图详情卡片里点「这条信息有错？点这里反馈」，或在「信息反馈」标签页
   选择记录后反馈；
2. 填写出错字段（名称/地址/坐标/电话/类型/其他）、错误描述、可选建议值与联系方式；
3. 提交后写入 `feed_back` 表，状态为 `pending`（待审核）；
4. 后台用命令行审核，审核通过时**可以**把建议值回写到业务记录。

```bash
python main.py review list                                  # 看待审核列表
python main.py review show 3                                # 看详情
python main.py review approve 3 --apply --reviewer 张三 --note 电话核实
python main.py review reject 3 --note 证据不足
```

回写规则：
- 只改用户指明的那个字段（名称/地址/电话/类型/坐标），其余字段不动；
- 坐标必须写成 `经度,纬度`，且必须是数字、在合法范围内；
- 「其他」类反馈只改状态，不改数据（避免不可控的批量改写）；
- 回写业务记录与改反馈状态在**同一个事务**里，不会出现「状态已通过但数据没改」；
- 已审核过的反馈不能再审；`--apply` 在非交互环境必须加 `--yes` 明确确认。

### 3.5 地图资源本地化（离线 / 内网可用）

folium 默认从公网 CDN 加载 10 个资源。实测只要其中两个挂了，地图就不能正常显示：

- `leaflet.js` 加载不到 → 地图整块白屏；
- `leaflet.awesome-markers.js` 加载不到 → `L.AwesomeMarkers.icon(...)` 报错，
  用 `folium.Icon` 画的标记点全部消失。

项目里已经把 Leaflet 1.9.4 放在 `libs/`（`leaflet.js` / `leaflet.css` / `images/`），
`map_assets.py` 负责按场景切换引用方式：

| 场景 | 引用方式 | 效果 |
|---|---|---|
| 静态导出 HTML | `<script src="libs/leaflet.js">`、`<link href="libs/leaflet.css">` | 双击打开即用，无外网请求 |
| 前端内嵌地图 | 把 leaflet 内容编码成 `data:` URL 注入组件 iframe | 组件里拿不到项目目录，这样也零请求 |

同时去掉了 jquery / bootstrap / glyphicons / fontawesome / awesome-markers 这 8 个用不到的
CDN 依赖，标记点改为**内联 SVG 图钉**（`folium.DivIcon`），不依赖任何图标字体。

> ⚠️ 一个踩过的坑：`folium.Popup` 的模板里用 jQuery 的 `$(...)` 把 HTML 转成 DOM 节点，
> 一旦页面没有 jQuery（离线/CDN 不通）就会抛 `ReferenceError: $ is not defined`，
> **整段地图脚本直接中断**，表现就是「地图只出来一个点 / 完全不加载」。
> 所以本项目的标记点改用 Leaflet 原生 **tooltip**（模板字符串，零依赖）：
> 鼠标悬停显示名称、地址、电话、类型，点击标记则由下方详情卡片展示完整信息。

- 导出到其它目录时，`folium_map.export` 会自动把 `libs/` 复制到 HTML 旁边；
- 本地资源缺失时自动回退到 folium 默认 CDN，并打印一次警告；
- 应急开关：环境变量 `MAP_ASSET_MODE=cdn` 可强制走原来的 CDN 方式。

自查：导出的 HTML 里搜不到 `http` 外链、搜不到 `AwesomeMarkers`、搜不到 `$(`。

> 注意：底图瓦片仍然来自高德服务器。完全断网时能正常看到标记点、控件和交互，
> 但底图会是灰底——瓦片要离线只能自己搭缓存或改用本地瓦片目录。

### 3.6 统一入口

```bash
python main.py app [--port 8501] [--no-browser]   # 启动前端
python main.py scrape --target bank|community|all --city 深圳 --pages N
python main.py export-map --layer bank|community|all [--out 路径]
python main.py db status|migrate                  # 库结构/数据量/待审核数量
python main.py review list|show|approve|reject
```

---

## 4. 数据库表结构（v3）

`banks` 与 `communities` 共用同一套列（新增一类数据只需在 `config.LAYERS` 注册，
表会被自动创建）：

| 列 | 说明 |
|---|---|
| `id` | 主键 |
| `name` / `address` / `tel` / `type` | 高德 POI 字段（`type` 保留原始分类串） |
| `lng` / `lat` | GCJ-02 坐标（高德「火星坐标」） |
| `city` / `district` / `adcode` | 城市 / 区县 / 行政区划编码（为「不只在深圳」预留） |
| `source` | 数据来源标识 |
| `extra` | **预留拓展槽**：JSON 文本，放未来才有的业务字段（如小区物业费、户数） |
| `created_at` / `updated_at` | 首次入库 / 最近更新时间 |

唯一索引 `(name, lng, lat)`：重复抓取不会堆积数据，靠数据库去重而不是靠某次运行的内存。

`feed_back`（用户反馈）：

| 列 | 说明 |
|---|---|
| `id` | 反馈编号（界面会回执给用户） |
| `target_type` | 反馈对象类型：`bank` / `community`（对应 `config.LAYERS` 的 key） |
| `target_id` / `target_name` | 被反馈记录的 id 与名称快照（记录被改后仍能对照） |
| `field_name` | 出错字段：`name` / `address` / `lnglat` / `tel` / `type` / `other` |
| `error_desc` | 用户描述的错误（必填） |
| `suggested_value` | 建议的正确值（审核通过时可回写） |
| `contact` | 可选联系方式 |
| `status` | `pending` / `approved` / `rejected` |
| `reviewer` / `review_note` / `reviewed_at` | 审核人、审核备注、审核时间 |

---

## 5. 拓展指南（这些地方都留了接口）

- **新增一类数据**（学校、医院、加油站……）：在 `config.LAYERS` 加一条
  （表名、图标、高德类型编码、默认关键词），前端图层、抓取命令、列表、反馈就都支持了，
  业务代码一行不用改。
- **换城市 / 多城市共存**：`--city 广州` 或环境变量 `DEFAULT_CITY`；数据按 `city` 列区分，
  城市下拉框自动出现新城市，地图初始视野用「数据自身中心」自动定位。
- **数据量变大**：查询默认分页；周边查询把经纬度矩形范围下推到 SQL；地图最多渲染
  `MAP_MARKER_LIMIT` 个点（超出提示用列表查询）；`city`、`(name,lng,lat)` 都有索引。
- **换数据库**（PostgreSQL 等）：只需要改 `SQLOperation.py`，其它文件不用动——
  这正是「数据访问收敛到一个文件」的意义；连接从 `sqlite3` 换成连接池也只改这一处。
- **小区业务字段**：短期写进 `extra`（JSON），长期在 `POI_COLUMNS` 与建表语句里加列，
  并补一条 `ALTER TABLE` 迁移即可，旧库会自动升级。
- **反馈来源**：目前是前端表单；以后接小程序/客服系统，只要调用
  `SQLOperation.submit_feedback` 即可，表结构不用变。

---

## 6. 职责边界（为什么说「权责分明」）

| 层 | 文件 | 能做 | 不能做 |
|---|---|---|---|
| 界面层 | `bank_map_app.py`、`folium_map.py` | 展示、收集输入、调用 DAL | 不 `import sqlite3`、不写 SQL |
| 采集层 | `DataGet.py` | 调 API、清洗、调 DAL 入库 | 不直接碰数据库 |
| 入口层 | `main.py` | 参数解析、调度 | 不写 SQL |
| 数据访问层 | `SQLOperation.py` | **唯一**的建表/迁移/查询/写入/审核回写 | 不 import streamlit、不发网络请求 |
| 配置 | `config.py` | 常量、图层注册、环境变量 | 不连库、不发请求 |

自查命令（应只输出 `SQLOperation.py:...` 这些行）：

```bash
findstr /n "sqlite3" *.py     # Windows
grep -rn "import sqlite3" *.py  # Git Bash / Linux
```

---

## 7. 本次改造对照（原代码里的「可改进」批注 → 现在怎么处理）

| 原批注 | 处理 |
|---|---|
| `bank_map_app.py` 越过 SQLOperation 直接操作数据库 | 前端不再 `import sqlite3`，全部走 `SQLOperation` |
| `load_banks` 没有像 DAL 那样转义输入 | 统一用 `_escape_like` + `ESCAPE`，参数一律 `?` 占位 |
| `d_lng` 在大范围下会「爆炸」 | 粗筛下推到 SQL 的经纬度矩形（`BETWEEN`），再按 Haversine 精算 |
| `pd.isna` 挡不住空字符串 | 展示层统一 `fmt_value`（`None`/空串/`[]` 都当缺失） |
| 外部数据拼 HTML 未实际转义 | 统一 `html.escape`，防 XSS |
| 数据量大要考虑 `st.cache_data` 缓存 | 类型/城市下拉候选值已加 2 分钟缓存 |
| `upsert_banks` 空 rows 仍开连接计数 | 空列表直接返回 `(0, 0)` |
| 线性退避、未区分永久错误 | 指数退避 + `FATAL_INFOCODES` 永久错误不重试 |
| 没有处理 JSON 解析失败 | 捕获 `ValueError`（含 JSON 解码错误）并可重试 |
| `--target` 只抓银行、参数写死 | `config.LAYERS` + 命令行参数（城市/关键词/类型/页数） |
| 迁移/建表重复代码、索引无参数化 | 建表与索引统一由「图层表名」推导，迁移按 v1/v2/v3 分情况 |
| `status` 未设 `row_factory` | 统一 `sqlite3.Row`，按列名取值 |
| `clean_row` 认为 `lng=0/lat=0` 合法 | 保留这个判断，另加经纬度**合法范围**校验 |
| 抓取边抓边写可以省内存 | 当前仍是分批入内存后入库，注释标明了改造点 |
| folium 样式/脚本从公网 CDN 获取，离线打不开地图 | 改用本地 `libs/` + 内联 SVG 图钉，去掉 8 个用不到的 CDN 请求（见 3.5） |
| （实测新发现）`folium.Popup` 内部依赖 jQuery，删掉 jQuery 后脚本报 `$ is not defined` 中断 | 标记点改用 Leaflet 原生 tooltip，彻底不依赖 jQuery |
| （实测新发现）`st_folium` 回传的 `center` 是 `{"lat","lng"}` 字典，直接喂给 `folium.Map` 会报 TypeError | 新增 `as_center()` 归一化，存进 session_state 前就转成 `[lat, lng]` |
| （实测新发现）标记点 id 不存在 `last_object_clicked.tooltip` 里，取错了回传字段 | 回传字段补上 `last_object_clicked_tooltip`，点击标记才能打开详情 |

---

## 8. 坐标系说明（务必先读）

`banks.db` 里的 `lng/lat` 是 **GCJ-02**（高德「火星坐标」），底图必须用高德瓦片。
若换成 OpenStreetMap 等 WGS84 底图，必须先做 GCJ-02 → WGS84 转换，否则点位整体偏移约 500 米。
「底图坐标系 == 数据坐标系」是任何底图/数据源变更的必过检查项。

---

## 9. 已知限制（demo 定位）

- 单机 SQLite，未做多用户并发写入的优化（演示够用）；
- 反馈审核是命令行工具，没有网页后台与账号体系；
- 抓取受高德接口页码上限（100 页/关键词）限制，城市数据量大时要按区县拆分检索；
- 地图不做点位聚合，数据量大时靠列表查询分页；
- 底图瓦片依赖高德服务器，完全离线时只有标记点和控件，没有底图。
