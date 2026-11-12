"""FastAPI 入口：试验导入、备份迁入、列表、分桶查询。

启动后访问 /docs 即可在 Swagger UI 上传样例 CSV 或 SQLite 备份并调整桶宽查询。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, UploadFile
from sqlite3 import Connection

from . import __version__, config
from .db import backup_to, init_db, session
from .ingest import CsvValidationError, import_test
from .migration import MigrationError, migrate_from_backup
from .schemas import (
    BackupResponse,
    ImportResponse,
    MigrateResponse,
    QueryResponse,
    TestOut,
)
from .service import bucket_query


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="泵站启泵试验水锤压力分析",
    description=(
        "导入记录仪单次试验 CSV（毫秒时间戳、kPa 压力），"
        "在 SQLite 中保存原始采样与多级摘要；按起止时刻与桶宽分桶，"
        "返回每桶首/尾/最高/最低点，空桶标记为缺测。"
        "另支持从同版服务的 SQLite 单文件备份迁入单场试验（编号重新分配）。"
    ),
    version=__version__,
    lifespan=lifespan,
)


def get_conn() -> Connection:
    with session() as conn:
        yield conn


@app.get("/health", tags=["系统"])
def health():
    return {"status": "ok"}


@app.post(
    "/tests/import",
    response_model=ImportResponse,
    status_code=201,
    tags=["试验导入"],
    summary="上传单次试验 CSV",
)
def upload_test(
    file: UploadFile = File(..., description="记录仪导出的 CSV，首行表头"),
    name: str = Form(..., description="试验名称，库内唯一"),
    conn: Connection = Depends(get_conn),
):
    content = file.file.read(config.MAX_UPLOAD_BYTES + 1)
    if len(content) > config.MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"文件超过 {config.MAX_UPLOAD_BYTES} 字节上限",
        )
    if not content:
        raise HTTPException(status_code=400, detail="上传文件为空")
    try:
        result = import_test(conn, name=name, filename=file.filename or "", content=content)
    except CsvValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return result


@app.post(
    "/tests/backup",
    response_model=BackupResponse,
    status_code=201,
    tags=["试验迁入"],
    summary="导出一份可迁入 / 归档的 SQLite 单文件备份",
)
def create_backup(conn: Connection = Depends(get_conn)):
    """用 SQLite 在线备份 API 生成一致的单个 .db 文件（不含 -wal/-shm）。

    该文件可直接作为 `POST /tests/migrate` 的 `file` 使用；
    回包中的 `path` 为容器内路径，位于 `PUMP_BACKUP_DIR`（默认 `/data/backups`）。
    """
    config.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = config.BACKUP_DIR / f"pump_backup_{stamp}.db"
    if dest.exists():
        raise HTTPException(status_code=409, detail="同名备份已存在，请稍后重试")
    try:
        backup_to(config.DB_PATH, dest)
    except Exception as exc:  # 磁盘满、目录不可写等
        dest.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"生成备份失败：{exc}") from None
    test_count = conn.execute("SELECT COUNT(*) FROM tests").fetchone()[0]
    return {
        "path": str(dest),
        "test_count": test_count,
        "size_bytes": Path(dest).stat().st_size,
    }


@app.post(
    "/tests/migrate",
    response_model=MigrateResponse,
    status_code=201,
    tags=["试验迁入"],
    summary="从同版服务的 SQLite 备份迁入单场试验",
    response_description="迁入成功，返回中心库新分配的试验编号",
)
def migrate_test(
    file: UploadFile = File(
        ...,
        description="源服务导出的 SQLite 单文件备份（.db）",
    ),
    source_test_id: int = Form(
        ...,
        ge=1,
        description="备份中的源试验编号（对应备份库 tests.id）",
    ),
    conn: Connection = Depends(get_conn),
):
    """只复制该场试验的元信息、原始采样与各级摘要，不重算摘要。

    - 中心库重新分配试验编号；即使两库编号 / 采样行号重叠，
      按新编号查询的分桶结果、`blocks_by_level`、`raw_rows_read` 与源库一致。
    - 源试验不存在 → 404；摘要引用的首尾采样对不上 → 422；
      中心库已有同名试验 → 409；备份不可读 → 400。
    - 迁入在单事务内完成，失败时中心库保持原样。
    """
    content = file.file.read(config.MAX_BACKUP_BYTES + 1)
    if len(content) > config.MAX_BACKUP_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"备份超过 {config.MAX_BACKUP_BYTES} 字节上限",
        )
    try:
        return migrate_from_backup(conn, content, source_test_id)
    except MigrationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from None


@app.get("/tests", response_model=list[TestOut], tags=["试验查询"], summary="列出全部试验")
def list_tests(conn: Connection = Depends(get_conn)):
    rows = conn.execute(
        "SELECT id, name, source_filename, sample_count, start_ts, end_ts, "
        "max_level, imported_at FROM tests ORDER BY id"
    ).fetchall()
    return [dict(r) for r in rows]


@app.get(
    "/tests/{test_id}",
    response_model=TestOut,
    tags=["试验查询"],
    summary="查看单个试验元信息",
)
def get_test(test_id: int, conn: Connection = Depends(get_conn)):
    row = conn.execute(
        "SELECT id, name, source_filename, sample_count, start_ts, end_ts, "
        "max_level, imported_at FROM tests WHERE id = ?",
        (test_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"试验 {test_id} 不存在")
    return dict(row)


@app.get(
    "/tests/{test_id}/query",
    response_model=QueryResponse,
    tags=["试验查询"],
    summary="按起止时刻与桶宽分桶，定位每桶水锤峰谷",
)
def query_buckets(
    test_id: int,
    start_ts: int = Query(..., description="查询起点毫秒时间戳（含），桶从该时刻起划分"),
    end_ts: int = Query(..., description="查询终点毫秒时间戳（不含），末桶截到该时刻"),
    bucket_width_ms: int = Query(..., gt=0, description="桶宽，毫秒，必须为正整数"),
    conn: Connection = Depends(get_conn),
):
    if end_ts <= start_ts:
        raise HTTPException(
            status_code=400, detail="end_ts 必须大于 start_ts（半开区间 [start, end)）"
        )
    try:
        return bucket_query(conn, test_id, start_ts, end_ts, bucket_width_ms)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
