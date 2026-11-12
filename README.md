# 泵站启泵试验 · 水锤压力峰谷分析服务

面向泵站调试员的启泵试验复盘工具：把记录仪导出的**单次试验 CSV** 导入
FastAPI 服务，采样与多级摘要落盘到 **SQLite**；复盘时给定起止时刻和桶宽，
按桶返回首尾点与压力最高 / 最低点，快速定位水锤造成的峰谷，空桶自动标记缺测。

临时调试机撤场后，泵站档案员可用 `POST /tests/migrate` 从留存的数据库备份里
挑一场启泵试验**迁入中心库**，验收组按中心库新编号继续查峰谷。

## 功能与约定

- **CSV 导入**（`POST /tests/import`，Swagger 可直接上传）
  - 首行表头，自动识别时间戳列与压力列，支持中英文表头
    （`timestamp_ms / ts / 时间戳 …`、`pressure_kpa / pressure / 压力(kPa) …`），
    UTF-8 与 GBK 编码均可
  - 毫秒时间戳必须为整数且**严格递增**，重复或倒序整次拒绝（HTTP 400）
  - 压力为有限数值，单位 kPa；单事务写入，失败不留半成品
- **备份迁入**（`POST /tests/migrate`，Swagger 可直接上传）
  - 输入：同版服务的 **SQLite 单文件备份** + **源试验编号**（备份库 `tests.id`）
  - 只复制这一场试验的元信息、`samples` 原始采样与各级 `blocks` 摘要，
    **摘要原样搬运、不重算**；中心库为它重新分配试验编号
  - 源备份里的试验编号、采样 rowid 与中心库现有数据**重叠也安全**：
    摘要块引用的首尾 rowid 会重映射到该场试验在中心库中的新采样行，
    因此按新编号查询的 `buckets`、`blocks_by_level`、`raw_rows_read`
    与源库逐字段一致，包括桶边界切开 level 1 最细摘要块的情形
  - 拒绝条件：备份不可读 / 缺表 → 400；源试验不存在 → 404；
    摘要引用的首尾采样对不上（rowid、时间戳或压力与摘要不符）→ 422；
    中心库已有同名试验 → 409
  - 写入在单个事务内完成，任何失败都回滚，**中心库保持原样**
- **备份导出**（`POST /tests/backup`）
  - 用 SQLite 在线备份 API 生成自包含的单文件 `.db`（含 WAL 中未落盘内容），
    可直接作为迁入接口的 `file`，不会出现 `-wal/-shm` 伴生文件
- **分桶查询**（`GET /tests/{id}/query`）
  - 从 `start_ts` 起按 `bucket_width_ms` 分桶，**含左端、不含右端**，
    末桶截到 `end_ts`
  - 每桶按时序返回首点、尾点、压力最高点、最低点；**同一点只列一次**，
    角色合并到 `roles`；极值并列时取时间戳最早的点
  - 桶内无采样时 `missing=true`、`points=[]`
- **多级摘要加速**（核心）
  - level 1 为最细索引块（默认每 128 个采样一块），其上逐级合并，
    每块保存首尾点、最高 / 最低点（含时间戳）、采样数与原始 rowid 区间
  - 查询时从最粗层级开始：**完整落在桶内的块直接采用摘要**；
    只有桶边界切开 level 1 最细块时，才按 rowid 区间回读原始采样
  - 响应中的 `metrics` 如实回显：`blocks_by_level`（各级摘要命中数）
    与 `raw_rows_read`（回读原始行数，边界不切块时为 0）

## 目录结构

```
app/
  config.py      环境变量配置（库路径、块大小、上传上限、备份目录、桶数上限）
  db.py          SQLite 连接、建表（tests / samples / blocks）、单文件备份
  ingest.py      CSV 解析、严格校验、导入
  summaries.py   多级摘要构建与区间递归查询
  service.py     分桶编排、点去重与角色合并
  migration.py   备份迁入：校验、rowid 重映射、单事务复制
  schemas.py     Pydantic 请求 / 响应模型
  main.py        FastAPI 路由
sample_data/
  pump_start_demo.csv      2 万点演示 CSV
  small_demo.csv           60 点小样本
  acceptance_baseline.csv  迁入演示：先导入中心库的基线 CSV（120 点）
  legacy_backup.db         迁入演示：源服务单文件备份（含试验 legacy-pump-001）
scripts/         样例 / 备份生成脚本
tests/           pytest 测试
Dockerfile, docker-compose.yml
```

`sample_data` 里两份迁入演示材料由 `scripts/generate_backup.py` 可复现生成。

## Docker Compose 启动

```bash
docker compose up --build
```

- 服务：<http://localhost:8000>
- Swagger UI：<http://localhost:8000/docs>
- 健康检查：`GET /health`
- SQLite 数据保存在命名卷 `pump-data`（容器内 `/data/pump_tests.db`），
  服务端导出的备份写在卷内 `/data/backups`

## 复现样例试验

样例 `sample_data/pump_start_demo.csv` 为 2 万点、10 ms 间隔的合成启泵曲线
（静压 → 启泵爬升 → 衰减振荡，含一次低压深谷），可重新生成：

```bash
python3 scripts/generate_sample.py
```

在 Swagger（`/docs`）中的操作步骤：

1. `POST /tests/import`：`name` 填 `demo-001`，`file` 选择
   `sample_data/pump_start_demo.csv`，执行。响应返回试验 id、时间范围与各级块数。
2. `GET /tests`：确认试验已入库。
3. `GET /tests/{id}/query`：
   - `start_ts=1731300000000`、`end_ts=1731300019999`、`bucket_width_ms=1000`
     → 200 个 1 秒桶，逐桶查看峰谷；
   - 改成 `bucket_width_ms=1280`（与块边界对齐）再试，
     可见 `metrics.raw_rows_read = 0`，全程只走摘要；
   - 换 `start_ts=1731299990000&end_ts=1731299995000` 可看到全部桶 `missing=true`。

小样本 `sample_data/small_demo.csv`（60 点）适合在 Swagger 里肉眼逐桶核对。

## Swagger 演示：编号 / 行号重叠时的迁入与峰谷查询

`sample_data/legacy_backup.db` 由同版服务生成，**整个库里只有一场试验**：
`tests.id = 1`、`name = legacy-pump-001`、300 个采样、采样 rowid 为 1..300；
`sample_data/acceptance_baseline.csv` 是 120 点的平稳基线，先导入中心库后
同样占用 `tests.id = 1`、采样 rowid 1..120 —— 编号与行号全部刻意重叠。

按顺序在 Swagger 操作：

1. **导入基线**：`POST /tests/import`
   - `name` = `acceptance-baseline`
   - `file` = `sample_data/acceptance_baseline.csv`
   - 响应 `id = 1`（中心库里的第一场试验）。
2. **迁入源试验**：`POST /tests/migrate`
   - `source_test_id` = `1`（指备份内的源编号）
   - `file` = `sample_data/legacy_backup.db`
   - 响应 `status 201`，`id = 2`、`source_test_id = 1`：
     中心库**重新分配**了新编号；`block_counts = {"1": 3, "2": 1}` 与源库相同，
     摘要没有被重算。
3. **按新编号查峰谷**：`GET /tests/2/query`（中心库编号 2 = 源试验 1）
   - `start_ts=1731300000000&end_ts=1731300002999&bucket_width_ms=1280`
     → 3 个桶、末桶 `end_ts` 截到 1731300002999；
     `metrics = {"blocks_used": 3, "blocks_by_level": {"1": 3}, "raw_rows_read": 0}`
     （与源库用源编号 1 查询逐字段一致）。
   - `start_ts=1731300000000&end_ts=1731300002990&bucket_width_ms=1000`
     → 3 个桶（末桶 99 点）；`raw_rows_read = 299`，
     桶边界切开了 level 1 最细块，回读原始采样。
   - `start_ts=1731300000000&end_ts=1731300002990&bucket_width_ms=640`
     → 5 个桶；第 2 桶（1731300000640..1731300001280）能看到启泵瞬间的
     最高点 368.039 kPa 与回落后的最低点 283.601 kPa。
   - `start_ts=1731300004000&end_ts=1731300005000&bucket_width_ms=250`
     → 4 个桶全部 `missing=true`（超出数据时间范围）。
4. **错误演示**（都在同一 Swagger 页面）：
   - 再次迁入同名试验 → `409`，提示中心库已有同名试验；
   - `source_test_id=2` → `404`，提示备份中找不到源试验；
   - 上传非 SQLite 文件 → `400`，提示备份不是 SQLite 数据库；
   - 用 `POST /tests/backup` 可导出一份单文件备份，其中的
     `/data/backups/...db` 可直接作为迁入的 `file` 再导入另一个中心库。

### curl 示例

```bash
# 1) 先导入基线 CSV（中心库编号 1）
curl -X POST http://localhost:8000/tests/import \
  -F "name=acceptance-baseline" \
  -F "file=@sample_data/acceptance_baseline.csv;type=text/csv"

# 2) 从备份迁入源试验（备份内编号 1，中心库重新分配为 2）
curl -X POST http://localhost:8000/tests/migrate \
  -F "source_test_id=1" \
  -F "file=@sample_data/legacy_backup.db;type=application/x-sqlite3"

# 3) 按新编号查峰谷：桶宽与 level 1 块对齐，全程走摘要
curl "http://localhost:8000/tests/2/query?start_ts=1731300000000&end_ts=1731300002999&bucket_width_ms=1280"

# 4) 桶宽 640ms：边界切开最细摘要块，回读原始采样
curl "http://localhost:8000/tests/2/query?start_ts=1731300000000&end_ts=1731300002990&bucket_width_ms=640"

# 5) 导出可再次迁入的单文件备份
curl -X POST http://localhost:8000/tests/backup
```

重新生成演示材料（内容可复现）：

```bash
python3 scripts/generate_backup.py
```

## 本地开发（无 Docker）

```bash
python3 -m pip install -r requirements.txt
python3 -m pytest tests/ -q
PUMP_DB_PATH=./data/dev.db python3 -m uvicorn app.main:app --reload --port 8000
```

## 关键环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `PUMP_DB_PATH` | `/data/pump_tests.db` | SQLite 文件路径 |
| `PUMP_BLOCK_SIZE` | `128` | 每级摘要块大小（点数 / 子块数） |
| `PUMP_MAX_UPLOAD_BYTES` | `209715200` | 单次上传 CSV 字节上限（200 MB） |
| `PUMP_MAX_BACKUP_BYTES` | `209715200` | 单次上传备份字节上限（200 MB） |
| `PUMP_BACKUP_DIR` | `<主库目录>/backups` | 服务端导出备份的落盘目录 |
| `PUMP_MAX_BUCKETS` | `100000` | 单次查询桶数上限，防止桶宽过小 |

## 迁入接口的错误码

| HTTP | 场景 |
| --- | --- |
| 400 | 备份为空 / 非 SQLite 文件 / 不是同版服务库（缺表缺列）/ 无法读取 |
| 404 | 备份里不存在指定源试验编号 |
| 409 | 中心库已有同名试验（重新分配编号，但名称保持唯一） |
| 413 | 备份超过 `PUMP_MAX_BACKUP_BYTES` |
| 422 | 摘要引用的首尾采样对不上，或摘要层级 / 采样数与元信息不一致 |
