# 线上展馆 API：观众参观路径 + 借展器物点交

两块能力：

1. 从浏览器上报的 `open` / `close` 事件还原匿名观众的实际参观路径，避免把未关闭页面
   计作完整参观；支持乱序补报、重传去重与数据同意撤回。
2. 借展器物到馆点交：先录发运箱单，再按箱提交整份实点清单（可更正/撤销），
   系统维护一张**物化差异表**，区分相符、冲突、错箱、多出、待查与短缺。

技术栈：Python 3.11 + FastAPI + SQLite（WAL 模式），Docker Compose 一键启动。

## 快速启动

```bash
docker compose up --build
# 打开交互文档：
#   Swagger UI  http://localhost:8000/docs
#   ReDoc       http://localhost:8000/redoc
# 健康检查     http://localhost:8000/health
```

SQLite 数据文件存放在命名卷 `exhibition-data`（容器内 `/data/exhibition.db`），
容器重建不丢数据。

本地（无 Docker）运行：

```bash
pip install -r requirements.txt
EXHIBITION_DB=./exhibition.db uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/events` | 上报单个事件 |
| POST | `/events/batch` | 批量上报，单事务原子提交 |
| GET | `/visitors/{visitor_id}/path` | 查询观众参观路径 |
| POST | `/visitors/{visitor_id}/withdraw` | 撤回数据使用同意（body 可省略，或传 `{"visitor_id": "..."}`） |
| POST | `/loan/batches/{batch_id}/manifest` | 录入一批借展的发运箱单 |
| PUT | `/loan/batches/{batch_id}/boxes/{box_id}/count` | 按箱提交整份实点清单（首次点交/更正，空清单算已点） |
| DELETE | `/loan/batches/{batch_id}/boxes/{box_id}/count` | 撤销一箱点交（幂等） |
| GET | `/loan/batches/{batch_id}/discrepancies` | 查询点交差异表（直接读已存表） |
| GET | `/health` | 健康检查 |

## 借展器物点交

### 操作流程

1. **先录发运箱单**：`POST /loan/batches/{batch_id}/manifest`，body 列出每个箱及其
   器物号。每个器物号全批只能归一个箱，跨箱重复返回 400 且整批回滚；同一批次
   重复录入返回 409。录入后每个箱单器物先在差异表落「待查」。
2. **按箱提交实点清单**：`PUT .../boxes/{box_id}/count`，body 为
   `{"artifact_ids": [...]}`，是**整份替换**语义——重复提交即更正；箱号必须来自
   发运箱单（否则 400）；箱内重复器物号自动去重；**空清单 `[]` 也算该箱已点**。
3. **可撤销**：`DELETE .../boxes/{box_id}/count` 删除该箱现行清单，撤销后该箱视为
   未点；撤销未点过的箱不报错（幂等，`was_checked=false`）。
4. **随时查询**：`GET .../discrepancies` 直接读已物化的差异表，按器物号返回
   应在箱、实见箱列表、结论与中文标签，并附各箱点交状态和结论计数。

### 差异结论（按器物号）

| conclusion | 中文 | 判定规则 |
| --- | --- | --- |
| `ok` | 相符 | 箱单所列，且只在应在箱实见 |
| `conflict` | 冲突 | 在**多个箱**实见（即使其中一个是应在箱，仍优先报冲突） |
| `misboxed` | 错箱 | 只在**别的箱**找到（应在箱没出现） |
| `extra` | 多出 | 箱单**没列**该器物，且只在一个箱实见 |
| `pending` | 待查 | 箱单列了但没找到，且**尚有箱未点完** |
| `shortage` | 短缺 | **全部箱点完**仍没找到 |

### 设计要点（如何满足需求）

1. **物化差异表，查询不重算**：`loan_discrepancies` 持久化每个器物的应在箱、
   实见箱 JSON 数组与结论。查询端点只做 `SELECT`，不现场推导。
2. **实点记录与差异表同一事务**：每次点交/撤销在一个 `BEGIN IMMEDIATE` 事务内
   先写 `loan_box_counts` 再更新差异表，任一步失败整体回滚，**保留原结果**。
3. **增量重核，不全表重算**：提交一箱时，重核范围 = 该箱旧清单 ∪ 新清单涉及的
   器物（撤销时仅旧清单），其余器物的差异行原样保留；响应体回传
   `reevaluated_artifacts` 便于核对。
4. **待查/短缺随点齐状态翻转**：当「全部箱点完」状态发生变化时，批量更新所有
   仍未找到的箱单器物——点齐时 `pending → shortage`，撤销导致不再点齐时
   `shortage → pending`；已找到器物（相符/错箱/冲突）不受影响。
5. **多出器物的生命周期**：箱单外器物首次实见即落 `extra` 行；若唯一见到它的
   箱被撤销，该行删除（箱单外且无实见即无差异）。
6. **并发安全**：沿用 WAL + `BEGIN IMMEDIATE` + `busy_timeout`，多 worker 并发
   点交不同箱由 SQLite 写锁串行化，查询在只读快照事务里读差异表，不会看到
   半份更新。

### 点交示例（curl）

```bash
# 1) 发运箱单：C1 装 A1/A2，C2 装 A3
curl -X POST localhost:8000/loan/batches/LOT-1/manifest \
  -H 'Content-Type: application/json' \
  -d '{"batch_id":"LOT-1","boxes":[
        {"box_id":"C1","artifact_ids":["A1","A2"]},
        {"box_id":"C2","artifact_ids":["A3"]}]}'

# 2) C1 实点：A1 相符；A3 错箱（应在 C2）；X9 多出
curl -X PUT localhost:8000/loan/batches/LOT-1/boxes/C1/count \
  -H 'Content-Type: application/json' \
  -d '{"artifact_ids":["A1","A3","X9"]}'

# 3) C2 空清单算已点 -> A2 报短缺
curl -X PUT localhost:8000/loan/batches/LOT-1/boxes/C2/count \
  -H 'Content-Type: application/json' -d '{"artifact_ids":[]}'

# 4) 查询差异表
curl localhost:8000/loan/batches/LOT-1/discrepancies
```

## 观众路径 API

事件字段：`event_id`（全局唯一，重传复用同一号）、`visitor_id`（匿名观众号）、
`page_view_id`（页面浏览号，同一次浏览的开关共享）、`exhibit_id`（展品号）、
`event_type`（`open` / `close`）、`event_time`（**带时区的 UTC ISO-8601**，
如 `2026-11-18T08:00:00Z`；无时区时间戳返回 400）。

### 上报响应语义

- `stored`：首次入库
- `duplicate`：`event_id` 已存在，幂等忽略（即使重传内容被篡改，也以首次内容为准）
- `rejected_withdrawn`：该观众已撤回同意，事件不入库，路径不会因此恢复

HTTP 状态码：成功受理固定 `201`，具体结果看响应体 `status`；参数/时间非法为
`422` / `400`。

### 路径响应

按 `page_view_id` 配对开关，同一观众按 **open 时间** 排序（`sequence` 从 1 连续编号）：

- 有关闭：`dwell_known=true`，`dwell_seconds` 为停留秒数
- 缺关闭：仍计为一次参观，但 `closed_at=null`、`dwell_known=false`、`dwell_seconds=null`（停留未知）
- 只有 close 没有 open 的孤儿事件不产生参观项
- close 早于 open 的异常数据：保留关闭时间，但停留时长标记未知

## 设计要点（如何满足需求）

1. **重传不重复计数**：`events.event_id` 为主键；插入前先查存在性，存在即返回
   `duplicate`，整段逻辑在同一事务内。
2. **乱序补报照常处理**：路径不做物化存储，每次查询从已提交事件实时配对、排序。
   后补的较早 open 会在下次查询时自然改变路径顺序；后补的 close 会把“停留未知”
   变成已知时长。
3. **撤回后不可恢复**：`withdrawals` 表持久化撤回标记；撤回在一个 `BEGIN IMMEDIATE`
   事务内完成「删除该观众全部事件 + 写撤回标记」。此后任何上传（包括旧事件重放）
   在同一事务内先查撤回标记，一律 `rejected_withdrawn`。
4. **交错操作基于同一已提交状态、不露半份路径**：
   - SQLite 开 WAL，写请求统一 `BEGIN IMMEDIATE` 立即拿写锁，配合 `busy_timeout`
     串行化所有写事务；
   - 读请求在单个 `BEGIN DEFERRED` 快照事务里完成「撤回检查 + 事件扫描」，
     要么看到撤回前的完整状态，要么看到撤回后的空路径；
   - 路径是查询时推导的，不存在“路径表更新一半”的中间态；
   - 批量上报为单事务，任一事件非法则整批回滚。
5. **线程/进程并发**：每请求一个独立 SQLite 连接（`check_same_thread=False`
   以适配 uvicorn 线程池，连接不在请求间共享）；多个 uvicorn worker 共享同一
   数据卷时由 SQLite 文件锁保证安全。

## 目录结构

```
app/
  config.py       环境变量配置（DB 路径、busy timeout）
  db.py           连接、schema、WAL/pragma、事务管理
  models.py       观众路径的 Pydantic 请求/响应模型
  crud.py         观众路径领域逻辑：入库去重、开关配对、路径推导、撤回
  loan_models.py  点交请求/响应模型与结论中文标签
  loan_crud.py    点交领域逻辑：箱单录入、整份点交/撤销、差异表增量重核
  main.py         FastAPI 路由与生命周期
tests/
  conftest.py
  test_api.py      15 项观众路径功能/并发测试
  test_loan.py     16 项点交功能/原子性/并发测试
  stress_http.py   针对运行中服务的真实 HTTP 并发压测（手动运行）
Dockerfile
docker-compose.yml
```

## 自测

```bash
pip install -r requirements-dev.txt
pytest tests/ -v
```

观众路径覆盖：正常配对与停留时长、乱序（close 先到）与更早事件补报导致的重排、
缺关闭标记未知及补关后转已知、孤儿 close、`event_id` 重传（含篡改内容）、
批量去重与整批原子回滚、撤回后路径消失/重复撤回幂等/旧事件重放与新事件均被拒、
多线程上传-查询-撤回交错（进程内 TestClient 与真实 HTTP 两种）、
无时区时间拒绝、时区偏移归一化、负停留异常、WAL 与主键的存储层校验。

点交覆盖：箱单录入与待查初值、器物跨箱归箱拒绝且整批回滚、批次重复录入 409、
相符/空清单算已点、整份更正且只重核受影响器物、箱内重复号去重、
**跨箱重复实见报冲突**、只在别箱找到报错箱、箱单外单箱实见报多出、
待查在全部点完后转短缺、**撤销一箱点交后短缺回退待查/多出行删除/幂等撤销**、
差异表写入失败时实点记录一并回滚保留原结果、未知箱 400/未知批次 404、
多线程点交-查询交错一致性。

## 已知限制 / 未完成项

- 本开发沙箱没有 Docker 守护进程，`Dockerfile` 与 `docker-compose.yml` 已编写并做
  YAML/结构校验，但**未在真实 Docker 中构建运行**；容器外已用 uvicorn 单进程与
  `--workers 2` 两种方式完成功能与并发验证（含真实 HTTP 全流程点交演示）。
- 同一 `page_view_id` 若出现多个 open，取最早 open；多个 close 取最早 close，
  异常上报不报错但只保留一组配对。
- 没有鉴权/限流与分页；观众号、事件号、批次号、箱号、器物号均按字符串处理，
  未做格式白名单。
- 撤回是硬删除事件数据（仅保留撤回标记本身），不做审计留档。
- 发运箱单录入后不支持修改/追加箱（设计为一批一单）；如需改箱单只能启用新批次。
- 差异表的「实见箱」来自现行各箱清单，撤销点交会使历史实见痕迹一并消失，
  不另存点交历史版本。
