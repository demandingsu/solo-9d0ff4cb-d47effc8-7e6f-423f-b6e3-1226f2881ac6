# 冷库超温占比 / 纸带核对 API

冷库质检员在明细读取配额内查询任意整点区间的超温（>8℃）占比，并按纸带标记
补回可证实的采样分钟。FastAPI + SQLite：

- 查询时从同一已提交状态的有效摘要与小时明细中，选出恰好覆盖全段、读取明细行数最少的组合；
- 纸带核对按小时和明细版号提交纸带上标出的分钟（0～59）与温度，
  按录入顺序把整小时读数排入各分钟，求每笔读数的所有可行分钟，
  唯一可行才补入分钟索引；版号不符或无可行排法整组退回。

## 启动

```bash
docker compose up --build
# 打开 http://localhost:8000/docs （Swagger UI，可直接操作全部接口）
```

本地开发（Python 3.11+）：

```bash
pip install -r requirements-dev.txt
uvicorn app.main:app --reload
pytest tests/ -v
```

数据库文件路径由环境变量 `COLDCHAIN_DB` 指定（compose 中为 `/data/coldchain.db`，挂在命名卷上）。

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/hours/{hour}/readings` | 录入某 UTC 整点的非空温度明细；已有数据返回 409 |
| PUT | `/hours/{hour}/readings` | 更正该小时全部明细（整小时替换，版本 +1） |
| GET | `/hours` | 已录入小时一览（版本号、行数） |
| POST | `/summaries` | 为齐备的整点区间 `[start, end)` 生成摘要；缺小时返回 409 |
| GET | `/summaries` | 摘要一览，含当前有效性 |
| POST | `/query` | 在 `max_detail_rows` 配额内查询 `[start, end)` 超温占比 |
| POST | `/hours/{hour}/reconciliations` | 按版号提交纸带标记试算，逐笔返回可行分钟；版号不符/无可行排法 409 |
| GET | `/hours/{hour}/reconciliations` | 该小时全部试算单（含 `feasible=false` 的退回留痕） |
| POST | `/hours/{hour}/reconciliations/{id}/approval` | 把试算单登记为最近一次核定，登记前再次核对版号 |
| GET | `/hours/{hour}/approval` | 最近一次核定；核定后明细已换版则 `stale=true`，从未核定 404 |

时间一律为带时区的 UTC 整点（分秒为 0），区间含起点、不含终点。超温判定为严格大于 8℃。

## 实现

**数据模型**（`app/db.py`）

- `hours(hour_ts, version, row_count)`：每个已录入小时一行，`version` 每次更正 +1，`row_count` 为当前明细行数（规划阶段的成本元数据）。
- `readings(hour_ts, temperature)`：温度明细行，自增 `id` 即录入顺序。
- `summaries(start_ts, end_ts, total_count, over_count)` + `summary_hours(summary_id, hour_ts, version)`：摘要生成时快照每个小时的版本号。
- `reconciliations(hour_ts, version, feasible, reading_count)` + `tape_marks(recon_id, minute, temperature)` +
  `reconciliation_readings(recon_id, entry_order, feasible_minutes, minute_index)`：试算单、纸带标记与逐笔可行分钟。
- `approvals(hour_ts PRIMARY KEY, recon_id, version)`：每小时最近一次核定；登记失败不写入，上次核定原样保留。

**摘要失效**：更正某小时 → `hours.version` +1 → 查询时用 `NOT EXISTS(summary_hours 快照版本 ≠ hours 当前版本)` 判定有效性。
按该小时旧明细生成的摘要自动失效，不相关摘要不受影响（`app/logic.py` 的 `_VALID_SUMMARIES_SQL`）。

**查询规划**（`app/logic.py: query_range`）

1. 整条查询在**一个读事务**内完成：有效摘要集合与小时行数来自同一已提交快照。
2. 规划只读元数据：以小时为格点做 DP，`dp[i]` = 覆盖前 i 小时所需最少明细行数
   （明细段成本 = 行数，摘要段成本 = 0；并列取段数更少者），回溯后相邻明细小时合并为一段。
3. **缺档 / 超预算在读取明细前返回**：`dp` 不可达 → 409 `missing_data`（列出缺档小时）；
   最优成本 > 配额 → 409 `budget_exceeded`（附所需行数）。
4. 通过检查后才 `SELECT temperature` 读取明细行，分段汇总总笔数、超温笔数与占比，
   响应标注每段来源（`summary` + `summary_id` 或 `details`）。

**纸带核对**（`app/reconcile.py`）

- 规则：整小时读数按录入顺序排入 0～59 各分钟，每分钟最多一笔；
  纸带标出的每个分钟都必须被一笔**同温**读数占据（强制锚点），未标分钟可空。
- 可行分钟用前后两次布尔 DP 求交集：`forward[i][m]` = 读数 `0..i-1` 能否排入 `[0,m)`，
  `backward[i][m]` = 读数 `i..n-1` 能否排入 `[m,60)`；读数 `i` 可行分钟 `m` 当且仅当
  `forward[i][m] ∧ backward[i+1][m+1]` 且标记位同温。空读数区间不能跨过强制标记（基例由首/末标记限定）。
  `forward[n][60]` 不成立即整组无可行排法。
- 复杂度 O(读数数 × 60)，与标记数量无关；60 笔满员时每笔恰好唯一。
- **同一已提交状态**：试算在一条只读事务里同时取版号与有序明细；版号先核对，不符直接 409 不落单。
  计算期间整小时更正仍可提交（WAL 读不阻塞写），试算只基于快照时的版号。
- **登记复核**：`approval` 在写事务内重新比对 `hours.version` 与试算单版号，
  不符或试算单不可行则回滚、保留上次核定；查询最近核定时当前版号 ≠ 核定版号即标 `stale`。
- 唯一可行才写 `minute_index`（含纸带未直接标出、但被锚点间接锁定的读数）；多解留空。

**健壮性**：自定义 422 处理器只回显 `type/loc/msg`，避免含 NaN 的非法输入在序列化错误体时变成 500。

## 实测

**单元/端到端测试**（`tests/`，TestClient + 临时库）：`26 passed`

- 录入校验：重复录入 409、空明细 / 非整点 / 缺时区 / NaN → 422
- 摘要有重叠（S1=[00,04)、S2=[02,06)）时查询 [00,06) 选最优组合
- 预算不足、缺档均在读明细前返回原因及所需行数/缺档小时
- 更正 h1 → S1 失效、S2 照用；更正后查询改用 S2+明细且反映新数据；
  重新生成 S1′ 后配额 0 可查；更正两个摘要共有的小时 → 全部失效退回全明细
- 纸带核对 DP：无标记候选区间、60 笔满员逐笔唯一、同温多笔、首末锚点锁定、
  未标出读数被间接锁定、错误温度/超额读数无可行排法、同温锚点顺序约束
- API：试算与留痕、版号不符 409 不落单、无可行排法 409 留痕、核定后更正 `stale=true`、
  失败登记保留上次核定、未知试算单 404、重复分钟/越界/空标记/NaN → 422
- **核定中途更正**：读事务快照建立后挂起，期间另一请求整小时更正成功（WAL 不互堵）；
  试算按旧版快照完成，登记时版号复核 409、无核定写入，按新版重做后登记成功

**DP 穷举对照**（独立脚本，小分钟槽全实例）：5 分钟槽 × 2 种温度的 **15,066 个全部实例**、
以及 8 分钟槽 3,000 个随机实例，DP 输出与暴力枚举所有合法排列的可行分钟集合逐一相等。

**真实服务冒烟**（uvicorn + curl，端口 8734，临时库）：

```
GET /docs -> 200，OpenAPI 含全部 10 条路径

同温多笔 [5,5,7,5]，标记 30=5℃：三笔 5℃ 都含分钟 30，均无 minute_index（多解不补）
强锚点 0=5℃、59=5℃：读数0 -> [0]、读数3 -> [59] 唯一补入，中间两笔多解
标记 30=9℃（无 9℃ 读数）-> 409 infeasible_arrangement，留痕单 feasible=false 可列出
version=99 -> 409 version_mismatch（submitted=99, current=1），不落试算单
登记强锚点单 -> 200 stale=false；PUT 更正到 5 行 version=2
再查核定 -> stale=true current=2；旧单登记 -> 409，recon_id 仍是上次核定

并发：试算(v1) 与整小时更正 同时发出 -> 试算 201（v1 快照）、更正 200(version=2)
      旧单登记 -> 409 version_mismatch，/approval 404（从未成功登记）
      按 v2 重新试算登记 -> 200 stale=false
```

## 余项

- 本环境无 Docker 守护进程，镜像构建未实测；`Dockerfile`/`docker-compose.yml` 为标准配置，应用本身经 uvicorn 实测。
- 无鉴权与多租户；单 worker 跑 SQLite（写串行），如需并发写可换 PostgreSQL，模型与 SQL 基本可平移。
- 摘要只增不删，长期运行需归档/清理策略；失效摘要目前保留供审计。
- 更正是整小时整体替换，不支持单条修改；`PUT` 对不存在的小时按建档处理（version=1）。
- 查询 DP 复杂度 O(小时数 + 摘要数)，超长区间（如数年）未做分片；缺档小时列表最多回显 24 条。
- 纸带试算单只增不删（含退回留痕），未做归档；每小时 `approvals` 仅保留最近一次核定。
- 温度相等按浮点 `==` 判定（与入库同一 REAL 值），若上游存在量纲/精度差异需先归一化再提交。
- 并发正确性依赖单读事务快照与 WAL（读写不互堵）；极端并发写仍由 SQLite 写锁串行，未做高并发压测。
