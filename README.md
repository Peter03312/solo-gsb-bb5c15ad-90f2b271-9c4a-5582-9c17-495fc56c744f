# 工地作业车危险区穿越审计服务

用 Python (FastAPI + Shapely + SQLite) 构建的 HTTP 服务，用于：

1. **登记危险区**：平面米制多边形（支持洞）+ UTC 左闭右开 `[start, end)` 生效时段；每次登记或同 ID 覆盖更新**原子地产生一个全局递增修订号**，并保存该区域的**不可变历史版本**；
2. **批量提交轨迹点**：车次、点 ID、UTC 时刻、平面坐标；同点 ID 同内容重传幂等，异内容返回冲突；
3. **穿越审计**：相邻点间按时间与坐标线性插值，仅在“轨迹覆盖且区域生效”的时间内计算穿越，按危险区返回起止区间与累计秒数；查询可指定 `revision`，按**各区截至该修订号的最后版本**复现当时核算。
4. **修订间差异核对**：给定较小/较大两个全局修订号，以首次请求时已存轨迹为两版共同输入，分别按各区截至两修订号的最后配置核算，只列出**穿越区间或累计秒数有变化**的车次×危险区，支持稳定分页与快照化翻页（后续页不受新上传轨迹/新登记区域影响）。

## 语义约定

- 坐标为**平面米制坐标**（单位统一为米），服务内部不做投影换算。
- 危险区 = 多边形内部 + **外边界**；**洞的内部与其边界均不计入**。
- 生效时段为 UTC、**左闭右开** `[start, end)`，仅当穿越时间落在生效时段内才统计。
- 相邻轨迹点按时刻排序后在相邻点间做**时间-空间线性插值**；乱序上传会自动按时刻重建。
- **零时长接触不算穿越**（轨迹仅擦过角点/边于一瞬不计）；静止点按点归属处理。
- 同一危险区的相接（端点重合）穿越区间自动合并；中间隔着未覆盖时间的区间不合并。
- **全局修订号**：每次区域登记或同 ID 覆盖更新都在单个立即事务内占用下一个全局递增整数（`1,2,3,…`），登记响应返回 `revision`；同一修订号下写入一份区域配置（几何 + 生效时段）的**不可变快照**，事后不会改变。校验失败的登记整体回滚，**不消耗修订号**。
- **按修订号复现**：穿越查询带 `revision=N` 时，取每个区域**截至 N 的最后版本**（last-version-as-of）核算；N 时**尚未登记的区域不参与**。沿用现有带洞边界、UTC 半开窗口与相邻点插值规则，响应额外返回所用 `revision`。不带 `revision` 的查询路径、请求字段与响应语义完全不变（走最新配置，响应不含 `revision` 字段）。
- 修订号 `N` 不存在（尚无任何登记）或 `N` 大于当前最大修订号时返回 `404 not_found`；`revision` 非正整数由参数校验返回 `422`。
- **两版差异核对**（`GET /api/v1/crossing-diffs`）：
  - 入参为较小的 `old_revision`、较大的 `new_revision`（必须 `old < new`）、可选 UTC 半开核算时段 `since`/`until`、`page_size`（1..1000，默认 100）与翻页游标 `cursor`；
  - **首次请求**以当时全部已存车次的轨迹为两版共同输入（冻结入库），区域分别取**截至 `old_revision` / `new_revision` 的最后版本**；旧版尚未登记的区域按**空结果**（未穿越、0 秒、无区间）比较；
  - 仅当某车次×某危险区的**穿越区间或累计秒数**两版不一致时才输出，条目给出 `old`/`new` 两版完整结论（`crossed`/`segments`/`total_duration_sec`）与 `duration_delta_sec`（新版累计秒 − 旧版累计秒，可正可负可为 0）；无任何变化返回空 `changes`；
  - 结果按 `(trip_id, zone_id)` 字典序**稳定分页**；首页在服务端生成不可变快照（`diff_snapshots` 表），`next_cursor` 为 HMAC 签名的不透明令牌；**后续页必须原样沿用首次请求的 `old_revision`/`new_revision`/`since`/`until`/`page_size` 并携带游标**，核算只从快照读取——翻页期间新上传的轨迹、新登记的区域不会改变本次结果（重新发起不带游标的首请求才会采用新数据）；游标与签名密钥均落库，**进程重启后翻页仍有效**；
  - 错误：`old_revision >= new_revision`、`since >= until`、时刻缺时区等返回 `400 validation_error`；任一修订号不存在/超出当前最大修订号返回 `404 not_found`；游标损坏/签名无效/对应快照缺失返回 `400`（detail 以“游标失效”开头），翻页参数与首请求不一致返回 `400`（detail 含“游标参数不匹配”）；修订号或 `page_size` 非正整数/非整数返回 `422`。
  - 沿用现有带洞边界（外边界计入、洞及其边界剔除）、相邻点时间-空间线性插值、零时长接触剔除、UTC 半开窗口与区间合并规则，逐车次核算与单车穿越查询 `GET /api/v1/trips/{trip_id}/crossings` 完全同构。
- **旧库迁移**：升级前已存在的区域在首次启动时一次性迁移为**基线版本**（修订号从 1 起），不推断迁移前历史；迁移为单事务、幂等，可安全重启。
- 并发登记由数据库立即事务串行化，修订号全局唯一连续；并发查询在快照事务内取齐轨迹与区域配置，**不会读到半次提交**。
- 非法输入明确拒绝（HTTP 400）：自交多边形、洞越出外环、洞互相重叠、非有限坐标（NaN/Infinity）、不带时区的时刻、非正长度时段等。
- 同车次同一 UTC 时刻只能属于一个点 ID，否则视为冲突（HTTP 409）。
- 批量提交为**单事务原子写入**：任何一点校验/冲突失败，整批不写入任何数据。
- SQLite 持久化到挂载卷，**进程/容器重启后记录仍可查询**。

## 一键启动（Docker Compose）

```bash
docker compose up -d --build
```

启动后：

- 服务地址：`http://localhost:8080`
- 健康检查：`GET http://localhost:8080/health`
- 交互式 API 文档（Swagger UI）：`http://localhost:8080/docs`
- OpenAPI 描述：`http://localhost:8080/openapi.json`

数据保存在 Docker 命名卷 `audit-data`（容器内 `/data/dangerzones.db`），重启不丢。

停止：

```bash
docker compose down          # 保留数据
docker compose down -v       # 同时删除数据卷
```

仅构建镜像：`docker build -t danger-zone-audit:1.0.0 .`，容器入口脚本会先执行数据库初始化（建表）再启动 uvicorn。

## 本地直接运行（无 Docker）

需要 Python 3.11+：

```bash
pip install -r requirements.txt
export DZ_DB_PATH=./data/dangerzones.db
python -m app.init_db                     # 可选，首次启动会自动建表
uvicorn app.main:app --host 0.0.0.0 --port 8080
# 或：python -m app.main
```

## 配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DZ_HOST` | `0.0.0.0` | 监听地址 |
| `DZ_PORT` | `8080` | 监听端口 |
| `DZ_DB_PATH` | `./data/dangerzones.db`（Compose 内为 `/data/dangerzones.db`） | SQLite 数据文件路径 |
| `DZ_MIN_DURATION_SEC` | `1e-9` | 小于该秒数的覆盖片段视为零时长接触，不计入穿越 |

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/v1/zones` | 登记（或同 ID 覆盖更新）危险区，返回全局修订号 |
| GET | `/api/v1/zones` | 列出全部危险区（含各自当前修订号） |
| GET | `/api/v1/zones/{zone_id}` | 查询单个危险区（含当前修订号） |
| GET | `/api/v1/zones/{zone_id}/history` | 查询单个危险区的全部不可变历史版本 |
| POST | `/api/v1/trips/{trip_id}/points` | 批量提交/重传某车次轨迹点（原子、幂等） |
| GET | `/api/v1/trips/{trip_id}` | 查询车次全部轨迹点（按时刻排序） |
| GET | `/api/v1/trips/{trip_id}/crossings` | 穿越审计结果（可选 `revision` 复现历史配置） |
| GET | `/api/v1/crossing-diffs` | 两个修订号之间全部已存车次×危险区的穿越结论差异（分页、快照游标） |

穿越查询参数（均可选）：`zone_id`（只看某危险区）、`since` / `until`（UTC ISO 8601，半开 `[since, until)` 时间过滤）、`revision`（正整数；取各区截至该号的最后版本，不填则使用最新配置）。

时间字符串使用 ISO 8601 且**必须带时区**，推荐以 `Z` 结尾表示 UTC，例如 `2026-09-30T12:00:00Z`。

## 调用示例

### 1) 登记带洞危险区

外边界 100m×100m，中央 20m×20m 为洞（洞及其边界不计入），2026-09-30 全天生效：

```bash
curl -s -X POST http://localhost:8080/api/v1/zones \
  -H 'Content-Type: application/json' \
  -d '{
    "zone_id": "Z-A",
    "exterior": [[0,0],[100,0],[100,100],[0,100]],
    "holes": [[[40,40],[60,40],[60,60],[40,60]]],
    "windows": [
      {"start": "2026-09-30T00:00:00Z", "end": "2026-10-01T00:00:00Z"}
    ]
  }'
# {"zone_id":"Z-A","status":"created","revision":1,"num_holes":1,"num_windows":1}
```

响应中的 `revision` 即本次登记的**全局递增修订号**（同 ID 覆盖更新时 `status` 为 `updated` 且修订号继续递增）。`GET /api/v1/zones/{zone_id}` 与 `GET /api/v1/zones` 的每个区域也带 `revision`（当前版本号）。

环的首尾点无需重复给出；可登记多个生效时段（重叠/相接时段会合并处理）。

### 2) 批量提交轨迹点（允许乱序）

```bash
curl -s -X POST http://localhost:8080/api/v1/trips/truck-7/points \
  -H 'Content-Type: application/json' \
  -d '{
    "points": [
      {"point_id":"a", "ts":"2026-09-30T12:00:00Z", "x":-20, "y":50},
      {"point_id":"c", "ts":"2026-09-30T12:02:00Z", "x":120, "y":50},
      {"point_id":"b", "ts":"2026-09-30T12:01:00Z", "x":50,  "y":50}
    ]
  }'
# {"trip_id":"truck-7","received":3,"accepted_new":3,"idempotent_retransmit":0}
```

同点 ID 同内容重传返回 `accepted_new=0, idempotent_retransmit=N`（HTTP 200）；
异内容返回 HTTP 409，例如：

```json
{"error":"conflict","detail":"point_id='a' 已存在且内容不同：已存 (ts_epoch=..., x=-20.0, y=50.0)，新传 (...)"}
```

整批中任何一点失败都会回滚，不写入部分数据。

### 3) 查询穿越审计

```bash
curl -s "http://localhost:8080/api/v1/trips/truck-7/crossings"
```

```json
{
  "trip_id": "truck-7",
  "point_count": 3,
  "zones": [
    {
      "zone_id": "Z-A",
      "crossed": true,
      "total_duration_sec": 68.571429,
      "segments": [
        {"start": "2026-09-30T12:00:17.142857Z", "end": "2026-09-30T12:00:51.428571Z", "duration_sec": 34.285714},
        {"start": "2026-09-30T12:01:08.571429Z", "end": "2026-09-30T12:01:42.857143Z", "duration_sec": 34.285714}
      ]
    }
  ]
}
```

车辆以 140m/120s 匀速横穿，进入外边界到进入洞前沿途计入；穿越洞（含其边界）的
约 17.14 秒被剔除，出洞后再次计入，因此得到两段、累计 68.571429 秒。两段之间被
洞隔开，不合并；若轨迹在区域边界处首尾相接，则自动合并为一段。

带时间过滤的查询：

```bash
curl -s "http://localhost:8080/api/v1/trips/truck-7/crossings?zone_id=Z-A&since=2026-09-30T12:00:30Z&until=2026-09-30T12:01:30Z"
```

### 4) 修订与按修订号复现历史核算

现场会修订危险区的几何或生效时段，已出具的穿越核算需要按当时的区域配置复现：

```bash
# 同 ID 覆盖更新（例如外边界扩到 200m×100m）：原子产生新修订号
curl -s -X POST http://localhost:8080/api/v1/zones \
  -H 'Content-Type: application/json' \
  -d '{
    "zone_id": "Z-A",
    "exterior": [[0,0],[200,0],[200,100],[0,100]],
    "holes": [],
    "windows": [
      {"start": "2026-09-30T00:00:00Z", "end": "2026-10-01T00:00:00Z"}
    ]
  }'
# {"zone_id":"Z-A","status":"updated","revision":2,"num_holes":0,"num_windows":1}
```

按**修订号 1**（旧的带洞 100m 配置）复现核算——取各区截至该号的最后版本：

```bash
curl -s "http://localhost:8080/api/v1/trips/truck-7/crossings?revision=1"
```

```json
{
  "trip_id": "truck-7",
  "point_count": 3,
  "zones": [
    {
      "zone_id": "Z-A",
      "crossed": true,
      "total_duration_sec": 68.571429,
      "segments": [
        {"start": "2026-09-30T12:00:17.142857Z", "end": "2026-09-30T12:00:51.428571Z", "duration_sec": 34.285714},
        {"start": "2026-09-30T12:01:08.571429Z", "end": "2026-09-30T12:01:42.857143Z", "duration_sec": 34.285714}
      ]
    }
  ],
  "revision": 1
}
```

要点：

- 响应回显所用 `revision`；指定版本中**尚未登记的区域不参与**（如 `revision=1` 时 2 号修订才登记的区域不会出现，`zone_id` 过滤一个该版本不存在的区域返回空 `zones`，不报错）。
- `revision` 超出当前最大修订号：`404 {"error":"not_found","detail":"修订号超出当前范围: revision=…，当前最大修订号=…"}`；非正整数：`422`。
- 可与 `zone_id` / `since` / `until` 组合。
- 不带 `revision` 时维持原路径与语义（最新配置，响应不含 `revision` 字段）。

查看某区域的全部不可变历史版本：

```bash
curl -s http://localhost:8080/api/v1/zones/Z-A/history
# {"zone_id":"Z-A","count":2,"versions":[{"zone_id":"Z-A","revision":1,"registered_at":"...","exterior":[...],"holes":[...],"windows":[...]}, ...]}
```

### 5) 两版修订号之间的穿越差异核对（分页 + 快照游标）

危险区配置经过多轮修订后，审计员核对**修订号 1 到修订号 3** 之间哪些已存车次、对哪些危险区的穿越结论发生了变化：

```bash
curl -s "http://localhost:8080/api/v1/crossing-diffs?old_revision=1&new_revision=3"
```

```json
{
  "old_revision": 1,
  "new_revision": 3,
  "since": null,
  "until": null,
  "page_size": 100,
  "changes": [
    {
      "trip_id": "truck-7",
      "zone_id": "Z-A",
      "old": {
        "crossed": true,
        "total_duration_sec": 40.0,
        "segments": [
          {"start": "2026-09-30T10:00:20Z", "end": "2026-09-30T10:01:00Z", "duration_sec": 40.0}
        ]
      },
      "new": {
        "crossed": true,
        "total_duration_sec": 60.0,
        "segments": [
          {"start": "2026-09-30T10:00:20Z", "end": "2026-09-30T10:01:20Z", "duration_sec": 60.0}
        ]
      },
      "duration_delta_sec": 20.0
    }
  ],
  "next_cursor": null
}
```

要点：

- 只列出**穿越区间或累计秒数有变化**的车次×危险区；两版完全一致的组合不出现，全部一致时 `changes` 为空数组。
- `old`/`new` 与单车穿越查询的单区结论结构完全相同（`crossed`/`segments`/`total_duration_sec`）；旧修订号下**尚未登记**的区域按空结果比较（`{"crossed": false, "total_duration_sec": 0, "segments": []}`）。
- 可选 UTC 半开核算时段与分页大小：

```bash
curl -s "http://localhost:8080/api/v1/crossing-diffs?old_revision=1&new_revision=3&since=2026-09-30T10:00:30Z&until=2026-09-30T10:01:00Z&page_size=50"
```

- **翻页**：首页响应中的 `next_cursor` 原样回传，其余参数必须与首次请求一致（含 `page_size`）：

```bash
curl -s "http://localhost:8080/api/v1/crossing-diffs?old_revision=1&new_revision=3&page_size=50&cursor=<上一页 next_cursor>"
```

  首页返回时全部已存车次轨迹与两版区域配置已冻结为服务端快照；翻页期间新上传的轨迹、新登记/修订的区域**不会改变这次结果**（游标继续指向旧快照）。需要基于最新数据重新核对时，去掉 `cursor` 发起一次新的首请求即可。游标经 HMAC 签名且密钥落库，进程重启后仍可继续翻页。
- 报错：顺序错误（`old_revision >= new_revision`）、核算时段非法为 `400 validation_error`；修订号不存在或超过当前最大修订号为 `404 not_found`；游标损坏/过期/签名无效为 `400`（detail 以“游标失效”开头），翻页参数与首请求不一致为 `400`（detail 含“游标参数不匹配”）；修订号、`page_size` 类型/范围错误为 `422`。

## 错误响应

- `400 validation_error`：几何/时间/坐标语义非法（自交多边形、洞越界、非有限坐标、时区缺失等）；差异核对的修订号顺序错误、核算时段非法、游标失效或翻页参数不匹配。
- `409 conflict`：同点 ID 异内容，或同车次同刻关联了不同点 ID。
- `404 not_found`：危险区或车次不存在；或穿越查询指定的 `revision` 不存在 / 超出当前最大修订号；差异核对的任一修订号不存在 / 超出当前最大修订号。
- `422`：请求体不符合 JSON 模式（类型错误、多余字段、缺字段），或 `revision` / `old_revision` / `new_revision` / `page_size` 不是合法正整数。

## 运行测试

```bash
pip install pytest httpx
python -m pytest -q
```

测试覆盖：带洞多边形裁剪、外边界计入、洞与洞边界剔除、零时长接触、自交/洞越界拒绝、
幂等与冲突、批量原子性、乱序重建、生效时段半开裁剪、区间合并、重启持久化；
修订号全局递增与原子占用、按修订号复现几何/窗口、截至版本取最后版本、未登记区域排除、
修订号越界/非法报错、失败登记不占号、并发登记唯一连续、历史版本不可变、旧库基线迁移等；
两版差异核对：几何/窗口/区间变化检出、旧版未登记按空比较、无变化空列表、半开核算时段过滤、
(trip,zone) 稳定分页、快照冻结（翻页期间新轨迹/新登记不影响结果）、顺序/非法时段/修订号
越界/非法参数报错、游标篡改与参数不匹配报错、游标跨重启有效等。

## 目录结构

```
app/
  config.py       # 环境变量配置
  geometry.py     # 带洞多边形校验与线段裁剪（Shapely）
  timeutils.py    # UTC 解析与半开时间窗运算
  cursor.py       # 差异核对分页游标（base64url + HMAC 签名）
  db.py           # SQLite 持久层（事务、唯一约束、索引、差异快照）
  service.py      # 业务逻辑：登记/幂等批量/穿越计算/两版差异核对
  schemas.py      # Pydantic 请求模型
  main.py         # FastAPI 入口
  init_db.py      # 数据库初始化（建表）
tests/            # pytest 测试套件
Dockerfile
docker-compose.yml
docker-entrypoint.sh
requirements.txt
```
