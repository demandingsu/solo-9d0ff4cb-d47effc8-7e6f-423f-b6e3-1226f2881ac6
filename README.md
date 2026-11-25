# 冷库超温占比查询 API

冷库质检员在明细读取配额内查询任意整点区间的超温（>8℃）占比。
FastAPI + SQLite，查询时从同一已提交状态的有效摘要与小时明细中，
选出恰好覆盖全段、读取明细行数最少的组合。

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

时间一律为带时区的 UTC 整点（分秒为 0），区间含起点、不含终点。超温判定为严格大于 8℃。

## 实现

**数据模型**（`app/db.py`）

- `hours(hour_ts, version, row_count)`：每个已录入小时一行，`version` 每次更正 +1，`row_count` 为当前明细行数（规划阶段的成本元数据）。
- `readings(hour_ts, temperature)`：温度明细行。
- `summaries(start_ts, end_ts, total_count, over_count)` + `summary_hours(summary_id, hour_ts, version)`：摘要生成时快照每个小时的版本号。

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

**健壮性**：自定义 422 处理器只回显 `type/loc/msg`，避免含 NaN 的非法输入在序列化错误体时变成 500。

## 实测

**单元/端到端测试**（`tests/test_api.py`，TestClient + 临时库）：`9 passed`

- 录入校验：重复录入 409、空明细 / 非整点 / 缺时区 / NaN → 422
- 摘要有重叠（S1=[00,04)、S2=[02,06)）时查询 [00,06) 选最优组合
- 预算不足、缺档均在读明细前返回原因及所需行数/缺档小时
- 更正 h1 → S1 失效、S2 照用；更正后查询改用 S2+明细且反映新数据；
  重新生成 S1′ 后配额 0 可查；更正两个摘要共有的小时 → 全部失效退回全明细

**真实服务冒烟**（uvicorn + curl，端口 8734，临时库）：

```
GET /docs -> 200
S1=[00,04) -> {"total_count":14,"over_count":4}   S2=[02,06) -> {"total_count":6,"over_count":3}

Q1 查[00,06) 配额100 -> detail_rows_read=2, 超温占比 5/16=0.3125
   段: summary#1 [00,04) + details [04,06)        # 重叠摘要中选了更优的 S1 方案
Q2 同段配额1 -> 409 budget_exceeded required_detail_rows=2
Q3 查[00,08) -> 409 missing_data missing_hours=[06:00, 07:00]

更正 h1=[2,3] -> version=2；GET /summaries: S1 valid=False, S2 valid=True
Q4 查[00,06) 配额10 -> detail_rows_read=7, 段: details [00,02) + summary#2 [02,06)
   总 13 笔超温 4 笔（更正后 h1 超温 0 已生效）
Q5 查[00,04) 配额0 -> 409 budget_exceeded required=11（S1 已失效）
重新生成 S1'=[00,04) -> {total:11, over:3}；配额0 查询命中 S1'，占比 3/11
```

## 余项

- 本环境无 Docker 守护进程，镜像构建未实测；`Dockerfile`/`docker-compose.yml` 为标准配置，应用本身经 uvicorn 实测。
- 无鉴权与多租户；单 worker 跑 SQLite（写串行），如需并发写可换 PostgreSQL，模型与 SQL 基本可平移。
- 摘要只增不删，长期运行需归档/清理策略；失效摘要目前保留供审计。
- 更正是整小时整体替换，不支持单条修改；`PUT` 对不存在的小时按建档处理（version=1）。
- 查询 DP 复杂度 O(小时数 + 摘要数)，超长区间（如数年）未做分片；缺档小时列表最多回显 24 条。
- 未做并发更正与查询交错的压测（一致性依赖单读事务快照，SQLite WAL 下读写不互堵）。
