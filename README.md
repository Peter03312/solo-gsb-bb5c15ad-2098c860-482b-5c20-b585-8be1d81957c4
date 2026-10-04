# 安全作业规程历史回溯服务

基于 **Python 3.11 + FastAPI + SQLite** 的双时间轴（bitemporal）服务：
安全作业规程（SOP）首次发布后只追加、不修改；更正只覆盖指定子区间，
并可按「作业时刻 + 知悉时刻」精确回溯任意时点看到的正文与版本。

## 一条命令启动（依赖安装 + 数据库初始化 + 服务启动）

```bash
docker compose up --build
```

该命令完成：

1. 构建镜像并安装 `requirements.txt` 中的全部依赖；
2. 容器入口自动执行数据库初始化（`python -m app.init_db`，幂等，已有库不受影响）；
3. 启动 HTTP 服务。

服务地址：<http://localhost:8080> ，交互式 API 文档：<http://localhost:8080/docs>

停止 / 清空重来：

```bash
docker compose down            # 停止并删除容器（保留数据库卷）
docker compose down -v         # 连同数据库卷一起删除
```

> 不用 Docker 的本地方式：
> `pip install -r requirements.txt && python -m app.init_db && uvicorn app.main:app --host 0.0.0.0 --port 8080`

## 配置项（均通过环境变量，带默认值）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `DB_PATH` | `/data/sop.db` | SQLite 数据库文件路径；容器内挂载在持久化卷 `/data` |
| `APP_HOST` | `0.0.0.0` | HTTP 监听地址 |
| `APP_PORT` | `8080` | HTTP 监听端口（改容器端口需同时调整 compose 端口映射） |
| `DB_TIMEOUT` | `10` | 写事务等待 SQLite 写锁的秒数（并发提交时串行化） |

宿主机端口在 `docker-compose.yml` 的 `ports: "8080:8080"` 调整。

## 核心语义

- **适用时间轴（valid time，作业时刻）**：规程正文在现实中生效的半开区间 `[起, 止)`。
- **事务时间轴（transaction time，知悉时刻）**：版本进入系统、再不可更改的时刻。
- **发布时间由服务端记录**，请求体里的 `published_at` 一律忽略，不可回填、不可篡改。
- **更正只挖洞**：新正文仅在「被替代版本当前生效区间内的指定子区间」生效；
  基版其余部分原样保留，更正发布之前的历史查询结果也永久保持原样（append-only）。
- **查询**：输入 `work_at`（作业时刻）与 `known_at`（知悉时刻，默认服务端当前时间），
  返回截至知悉时刻**已发布**、且在作业时刻**有效**的正文与版本；无匹配返回空。
- 支持对更正再更正（形成更正链）；查询取作业时刻命中的最深版本。

### 整体拒绝（事务回滚，绝不改动历史）

| 场景 | HTTP | error.code |
|---|---|---|
| 首次发布编号重复 | 409 | `DUPLICATE_PROCEDURE` |
| 适用区间非法（起 ≥ 止、缺时区） | 400/422 | `INVALID_REQUEST` / `ILLEGAL_INTERVAL` |
| 更正子区间超出被替代版本的生效区间 | 422 | `ILLEGAL_INTERVAL` |
| 被替代版本不存在 / 规程不存在 | 422/404 | `BASE_VERSION_NOT_FOUND` / `PROCEDURE_NOT_FOUND` |
| 基版在目标子区间已失效（被既有更正耗尽） | 409 | `BASE_EXPIRED` |
| 与已发布的同基版更正子区间重叠（并发更正冲突） | 409 | `CONCURRENT_CORRECTION` |
| 同批数组更正之间相互重叠 / 任一项不合法 | 409/4xx | 整批回滚，无一落库 |

并发安全：所有写入在单事务内 `BEGIN IMMEDIATE` 立即取写锁并做完整校验，
两个并发请求只会有一个成功，另一个整体收到 `CONCURRENT_CORRECTION`（或锁超时 503）。

## HTTP API

基址：`http://localhost:8080` ；所有时间为**带时区的 ISO 8601**（推荐结尾 `Z`，如 `2026-03-01T00:00:00Z`；也接受 `+09:00` 等，内部统一为 UTC）。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` | 健康检查，返回服务端当前 UTC 时间 |
| POST | `/api/v1/procedures` | 首次发布：编号、正文、UTC 适用区间 `[起,止)` |
| POST | `/api/v1/corrections` | 更正；请求体为单个对象（单条）或数组（并发更正，原子成败） |
| GET | `/api/v1/procedures` | 列出全部规程 |
| GET | `/api/v1/procedures/{no}/versions` | 查看某规程全部已发布版本（含发布时间、替代关系） |
| GET | `/api/v1/procedures/{no}/effective?work_at=&known_at=` | 单规程回溯；无匹配 `effective: null` |
| GET | `/api/v1/effective?work_at=&known_at=` | 跨规程回溯；无匹配 `results: []` |

### 首次发布

```bash
curl -X POST http://localhost:8080/api/v1/procedures \
  -H 'Content-Type: application/json' \
  -d '{"procedure_no":"SOP-001",
       "valid_start":"2026-01-01T00:00:00Z",
       "valid_end":"2026-12-31T23:59:59Z",
       "body":"进入受限空间作业规程（首版正文）"}'
```

### 提交更正（被替代版本 + 该版本当前生效区间内的子区间 + 新正文）

```bash
curl -X POST http://localhost:8080/api/v1/corrections \
  -H 'Content-Type: application/json' \
  -d '{"procedure_no":"SOP-001",
       "supersedes_version":1,
       "valid_start":"2026-03-01T00:00:00Z",
       "valid_end":"2026-04-01T00:00:00Z",
       "body":"进入受限空间作业规程（三月专项修订正文）"}'
```

并发更正（数组，全部成功或全部拒绝）：

```bash
curl -X POST http://localhost:8080/api/v1/corrections \
  -H 'Content-Type: application/json' \
  -d '[{"procedure_no":"SOP-001","supersedes_version":1,
        "valid_start":"2026-06-01T00:00:00Z","valid_end":"2026-06-15T00:00:00Z","body":"六月上"},
       {"procedure_no":"SOP-001","supersedes_version":1,
        "valid_start":"2026-06-15T00:00:00Z","valid_end":"2026-07-01T00:00:00Z","body":"六月下"}]'
```

### 回溯查询

```bash
# 现在（知悉时刻默认）查询 2026-03-15 的作业应执行哪个版本 → 更正版
curl "http://localhost:8080/api/v1/procedures/SOP-001/effective?work_at=2026-03-15T00:00:00Z"

# 回到更正发布之前的知悉时刻（这里取首版的发布时刻）→ 同一作业时刻查到的仍是首版
V1=$(curl -s http://localhost:8080/api/v1/procedures/SOP-001/versions \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["data"]["versions"][0]["published_at"])')
curl -s -G "http://localhost:8080/api/v1/procedures/SOP-001/effective" \
  --data-urlencode "work_at=2026-03-15T00:00:00Z" \
  --data-urlencode "known_at=$V1"

# 无匹配：effective 为 null
curl "http://localhost:8080/api/v1/procedures/SOP-001/effective?work_at=2030-01-01T00:00:00Z"
```

更多可直接运行的示例：

- `examples/curl_examples.sh` —— 全流程 curl（`bash examples/curl_examples.sh`）
- `examples/python_client.py` —— 零三方依赖的 Python 客户端（`python3 examples/python_client.py`）

## 数据模型（SQLite，只追加）

- `procedures(procedure_no PRIMARY KEY, created_publish)`
- `versions(procedure_no, version_no, valid_start, valid_end, body, published_at, supersedes_version,
  PRIMARY KEY(procedure_no, version_no))`

`versions` 表**只 INSERT，从不 UPDATE/DELETE**；更正通过 `supersedes_version`
与严格内嵌的子区间形成更正树，"当前生效状态"在查询时推导，因此任意历史时点都可复现。

## 测试

```bash
python -m unittest discover -s tests -v
```

覆盖：区间半开边界、双时间轴回溯、更正挖洞与嵌套更正、重复编号、非法区间、
过期基版、平级/批量/真实多线程并发冲突的整体回滚、无匹配返回空。
