"""从同版服务的 SQLite 单文件备份迁入单场启泵试验。

迁入规则：
- 只按源试验编号复制 tests 元信息、samples 原始采样和各级 blocks 摘要，
  摘要数值原样搬运，绝不重新构建 / 重算。
- 中心库重新分配 tests.id；原始采样的 rowid 与源库无关（两库行号可以重叠），
  blocks 中的 first_rowid / last_rowid 会按“源采样在该试验内的序号”重映射，
  从而保证按新编号查询时的分桶结果、blocks_by_level 与 raw_rows_read
  和源库完全一致，包括桶边界切开 level 1 最细摘要块的情形。
- 源试验不存在、摘要首尾采样引用对不上、中心库已有同名试验都会拒绝。
- 迁入写入在一个事务内完成，任何失败都回滚，中心库保持原样。
"""

from __future__ import annotations

import math
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Optional

from . import config

_SQLITE_MAGIC = b"SQLite format 3\x00"
_SAMPLE_CHUNK = 1000
_IN_QUERY_CHUNK = 500
# 同版服务每级至少 2 个点 / 子块合并，64 级远超任何真实试验；
# 用于挡住恶意备份里 level=10^9 之类会让层级集合展开失控的值。
_MAX_SUMMARY_LEVEL = 64

_REQUIRED_COLUMNS = {
    "tests": (
        "id",
        "name",
        "source_filename",
        "sample_count",
        "start_ts",
        "end_ts",
        "max_level",
        "imported_at",
    ),
    "samples": ("test_id", "ts", "pressure"),
    "blocks": (
        "test_id",
        "level",
        "block_index",
        "start_ts",
        "end_ts",
        "sample_count",
        "first_ts",
        "last_ts",
        "first_pressure",
        "last_pressure",
        "max_pressure",
        "max_pressure_ts",
        "min_pressure",
        "min_pressure_ts",
        "first_rowid",
        "last_rowid",
    ),
}

_BLOCK_COPY_FIELDS = (
    "level",
    "block_index",
    "start_ts",
    "end_ts",
    "sample_count",
    "first_ts",
    "last_ts",
    "first_pressure",
    "last_pressure",
    "max_pressure",
    "max_pressure_ts",
    "min_pressure",
    "min_pressure_ts",
    "first_rowid",
    "last_rowid",
)


class MigrationError(Exception):
    """迁入失败；status_code 供 HTTP 层直接使用。"""

    status_code = 400


class BackupInvalidError(MigrationError):
    """上传内容不是可读的同版 SQLite 备份。"""

    status_code = 400


class SourceTestNotFoundError(MigrationError):
    """备份里找不到指定编号的源试验。"""

    status_code = 404


class SummaryMismatchError(MigrationError):
    """摘要引用的首尾采样对不上，或试验元信息与采样不一致。"""

    status_code = 422


class DuplicateTestNameError(MigrationError):
    """中心库已有同名试验（tests.name 唯一）。"""

    status_code = 409


def _open_backup(path: Path) -> sqlite3.Connection:
    """打开落盘后的备份临时副本。

    上传字节只写入本服务创建的临时目录再打开，绝不直接打开上传路径；
    副本用完即弃，源备份不会被修改。
    """
    conn = sqlite3.connect(
        f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=30
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    conn.execute("PRAGMA foreign_keys = OFF")
    return conn


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _validate_schema(conn: sqlite3.Connection) -> None:
    """确认备份具备同版服务所需的表与列。"""
    for table, required in _REQUIRED_COLUMNS.items():
        columns = _table_columns(conn, table)
        if not columns:
            raise BackupInvalidError(
                f"备份不是同版服务的 SQLite 数据库：缺少 {table} 表"
            )
        missing = sorted(set(required) - columns)
        if missing:
            raise BackupInvalidError(
                f"备份不是同版服务的 SQLite 数据库：{table} 表缺少列 {', '.join(missing)}"
            )


def _fetch_sample_rows(
    conn: sqlite3.Connection, rowids: list[int]
) -> dict[int, sqlite3.Row]:
    """批量读取摘要边界 rowid 对应的采样行。"""
    rows: dict[int, sqlite3.Row] = {}
    for start in range(0, len(rowids), _IN_QUERY_CHUNK):
        chunk = rowids[start : start + _IN_QUERY_CHUNK]
        placeholders = ",".join("?" for _ in chunk)
        for row in conn.execute(
            f"SELECT rowid AS rid, test_id, ts, pressure FROM samples "
            f"WHERE rowid IN ({placeholders})",
            chunk,
        ):
            rows[row["rid"]] = row
    return rows


def _validate_source_test(
    src: sqlite3.Connection, test: sqlite3.Row
) -> list[sqlite3.Row]:
    """校验源试验元信息与摘要首尾采样引用，返回全部摘要块。"""
    source_id = test["id"]
    first = src.execute(
        "SELECT rowid AS rid, ts, pressure FROM samples "
        "WHERE test_id = ? ORDER BY rowid LIMIT 1",
        (source_id,),
    ).fetchone()
    last = src.execute(
        "SELECT rowid AS rid, ts, pressure FROM samples "
        "WHERE test_id = ? ORDER BY rowid DESC LIMIT 1",
        (source_id,),
    ).fetchone()
    count = src.execute(
        "SELECT COUNT(*) FROM samples WHERE test_id = ?", (source_id,)
    ).fetchone()[0]

    if first is None or last is None:
        raise SummaryMismatchError(f"源试验 {source_id} 没有原始采样，无法迁入")
    if count != test["sample_count"]:
        raise SummaryMismatchError(
            f"源试验 {source_id} 的采样数 {count} 与元信息 "
            f"{test['sample_count']} 对不上"
        )
    if first["ts"] != test["start_ts"] or last["ts"] != test["end_ts"]:
        raise SummaryMismatchError(
            f"源试验 {source_id} 的首尾采样时间与元信息对不上"
        )

    blocks = src.execute(
        "SELECT * FROM blocks WHERE test_id = ? ORDER BY level, block_index",
        (source_id,),
    ).fetchall()
    if not blocks:
        raise SummaryMismatchError(f"源试验 {source_id} 没有摘要块，无法迁入")

    levels = {row["level"] for row in blocks}
    max_level = max(levels)
    if min(levels) < 1 or max_level > _MAX_SUMMARY_LEVEL:
        raise SummaryMismatchError(
            f"源试验 {source_id} 的摘要层级超出合理范围：{sorted(levels)[:5]}"
        )
    if max_level != test["max_level"]:
        raise SummaryMismatchError(
            f"源试验 {source_id} 的摘要最高层级 {max_level} 与元信息 "
            f"{test['max_level']} 对不上"
        )
    if levels != set(range(1, max_level + 1)):
        raise SummaryMismatchError(
            f"源试验 {source_id} 的摘要层级不连续：{sorted(levels)}"
        )
    for level in levels:
        level_count = sum(
            row["sample_count"] for row in blocks if row["level"] == level
        )
        if level_count != count:
            raise SummaryMismatchError(
                f"源试验 {source_id} 的 level {level} 摘要采样数与原始采样对不上"
            )

    boundary_rowids = sorted(
        {row["first_rowid"] for row in blocks}
        | {row["last_rowid"] for row in blocks}
    )
    boundary_rows = _fetch_sample_rows(src, boundary_rowids)

    for block in blocks:
        where = f"level {block['level']} 第 {block['block_index']} 块"
        if block["sample_count"] <= 0:
            raise SummaryMismatchError(f"源试验 {source_id} 的 {where} 没有采样")
        if block["first_rowid"] > block["last_rowid"]:
            raise SummaryMismatchError(
                f"源试验 {source_id} 的 {where} 摘要引用的首尾采样对不上："
                "首尾 rowid 顺序颠倒"
            )
        first_row = boundary_rows.get(block["first_rowid"])
        last_row = boundary_rows.get(block["last_rowid"])
        if first_row is None or last_row is None:
            raise SummaryMismatchError(
                f"源试验 {source_id} 的 {where} 摘要引用的首尾采样对不上："
                "rowid 在采样表中不存在"
            )
        if first_row["test_id"] != source_id or last_row["test_id"] != source_id:
            raise SummaryMismatchError(
                f"源试验 {source_id} 的 {where} 摘要引用的首尾采样对不上："
                "rowid 不属于该试验"
            )
        if (
            first_row["ts"] != block["first_ts"]
            or not _same_float(first_row["pressure"], block["first_pressure"])
        ):
            raise SummaryMismatchError(
                f"源试验 {source_id} 的 {where} 摘要引用的首尾采样对不上："
                f"首点实际为 ts={first_row['ts']} pressure={first_row['pressure']!r}，"
                f"摘要为 ts={block['first_ts']} pressure={block['first_pressure']!r}"
            )
        if (
            last_row["ts"] != block["last_ts"]
            or not _same_float(last_row["pressure"], block["last_pressure"])
        ):
            raise SummaryMismatchError(
                f"源试验 {source_id} 的 {where} 摘要引用的首尾采样对不上："
                f"尾点实际为 ts={last_row['ts']} pressure={last_row['pressure']!r}，"
                f"摘要为 ts={block['last_ts']} pressure={block['last_pressure']!r}"
            )
        if block["start_ts"] != block["first_ts"] or block["end_ts"] != block["last_ts"]:
            raise SummaryMismatchError(
                f"源试验 {source_id} 的 {where} 摘要时间范围与首尾点对不上"
            )
    return blocks


def _same_float(a: float, b: float) -> bool:
    """摘要里的压力应当由同一 REAL 值原样写入；用精确相等比较。"""
    return a == b or (math.isnan(a) and math.isnan(b))


def _duplicate_name_id(conn: sqlite3.Connection, name: str) -> Optional[int]:
    row = conn.execute("SELECT id FROM tests WHERE name = ?", (name,)).fetchone()
    return None if row is None else row["id"]


def _copy_samples(
    dest: sqlite3.Connection,
    src: sqlite3.Connection,
    source_id: int,
    new_id: int,
    dest_base_rowid: int,
    boundary_rowids: set[int],
) -> dict[int, int]:
    """按源 rowid 顺序连续写入采样，返回边界 rowid -> 中心库 rowid 的映射。

    中心库 rowid 与源库 rowid 完全解耦：第 i 个采样固定写入
    dest_base_rowid + i，因此即使两库行号重叠也不会串数据。
    """
    rid_map: dict[int, int] = {}
    insert_sql = (
        "INSERT INTO samples (rowid, test_id, ts, pressure) VALUES (?, ?, ?, ?)"
    )
    batch: list[tuple[int, int, int, float]] = []
    index = 0
    for row in src.execute(
        "SELECT rowid AS rid, ts, pressure FROM samples "
        "WHERE test_id = ? ORDER BY rowid",
        (source_id,),
    ):
        dest_rid = dest_base_rowid + index
        if row["rid"] in boundary_rowids:
            rid_map[row["rid"]] = dest_rid
        batch.append((dest_rid, new_id, row["ts"], row["pressure"]))
        index += 1
        if len(batch) >= _SAMPLE_CHUNK:
            dest.executemany(insert_sql, batch)
            batch.clear()
    if batch:
        dest.executemany(insert_sql, batch)
    return rid_map


def _copy_test(
    dest: sqlite3.Connection,
    src: sqlite3.Connection,
    test: sqlite3.Row,
    blocks: list[sqlite3.Row],
) -> dict:
    source_id = test["id"]
    name = test["name"]

    # BEGIN IMMEDIATE 先拿写锁，保证“分配 rowid 区间 + 写入”期间没有并发写。
    dest.execute("BEGIN IMMEDIATE")
    try:
        existing = _duplicate_name_id(dest, name)
        if existing is not None:
            raise DuplicateTestNameError(
                f"中心库已存在同名试验 {name!r}（id={existing}），拒绝迁入"
            )

        cur = dest.execute(
            "INSERT INTO tests "
            "(name, source_filename, sample_count, start_ts, end_ts, max_level, imported_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                name,
                test["source_filename"],
                test["sample_count"],
                test["start_ts"],
                test["end_ts"],
                test["max_level"],
                test["imported_at"],
            ),
        )
        new_id = cur.lastrowid

        dest_base_rowid = (
            dest.execute("SELECT COALESCE(MAX(rowid), 0) FROM samples").fetchone()[0]
            + 1
        )

        boundary_rowids = {
            row["first_rowid"] for row in blocks
        } | {row["last_rowid"] for row in blocks}
        rid_map = _copy_samples(
            dest, src, source_id, new_id, dest_base_rowid, boundary_rowids
        )

        if len(rid_map) != len(boundary_rowids):
            missing = sorted(boundary_rowids - set(rid_map))
            raise SummaryMismatchError(
                f"源试验 {source_id} 的摘要引用的首尾采样对不上："
                f"rowid {missing[:5]} 不在该试验的采样序列中"
            )

        for block in blocks:
            values = []
            for field in _BLOCK_COPY_FIELDS:
                if field == "first_rowid":
                    values.append(rid_map[block[field]])
                elif field == "last_rowid":
                    values.append(rid_map[block[field]])
                else:
                    values.append(block[field])
            dest.execute(
                "INSERT INTO blocks (test_id, "
                + ", ".join(_BLOCK_COPY_FIELDS)
                + ") VALUES (?, "
                + ", ".join("?" for _ in _BLOCK_COPY_FIELDS)
                + ")",
                (new_id, *values),
            )

        block_counts = dict(
            dest.execute(
                "SELECT level, COUNT(*) FROM blocks WHERE test_id = ? GROUP BY level",
                (new_id,),
            ).fetchall()
        )
        imported_at = dest.execute(
            "SELECT imported_at FROM tests WHERE id = ?", (new_id,)
        ).fetchone()[0]
    except Exception:
        dest.rollback()
        raise
    else:
        dest.commit()

    return {
        "id": new_id,
        "name": name,
        "source_filename": test["source_filename"],
        "sample_count": test["sample_count"],
        "start_ts": test["start_ts"],
        "end_ts": test["end_ts"],
        "max_level": test["max_level"],
        "imported_at": imported_at,
        "block_counts": {str(k): v for k, v in sorted(block_counts.items())},
        "source_test_id": source_id,
    }


def migrate_from_backup(
    dest: sqlite3.Connection,
    backup_content: bytes,
    source_test_id: int,
) -> dict:
    """把备份中的单场试验完整迁入 dest，返回中心库分配的编号及元信息。"""
    if not backup_content:
        raise BackupInvalidError("备份文件为空")
    if not backup_content.startswith(_SQLITE_MAGIC):
        raise BackupInvalidError("备份不是 SQLite 数据库文件")
    if len(backup_content) > config.MAX_BACKUP_BYTES:
        raise BackupInvalidError(
            f"备份超过 {config.MAX_BACKUP_BYTES} 字节上限"
        )
    if source_test_id < 1:
        raise SourceTestNotFoundError(f"备份中找不到源试验 {source_test_id}")

    with TemporaryDirectory(prefix="pump-migrate-") as tmpdir:
        backup_path = Path(tmpdir) / "backup.db"
        backup_path.write_bytes(backup_content)
        try:
            src = _open_backup(backup_path)
        except sqlite3.DatabaseError as exc:
            raise BackupInvalidError(f"备份数据库无法打开：{exc}") from None
        try:
            try:
                _validate_schema(src)
                test = src.execute(
                    "SELECT id, name, source_filename, sample_count, start_ts, end_ts, "
                    "max_level, imported_at FROM tests WHERE id = ?",
                    (source_test_id,),
                ).fetchone()
            except sqlite3.DatabaseError as exc:
                raise BackupInvalidError(f"备份数据库无法读取：{exc}") from None
            if test is None:
                raise SourceTestNotFoundError(
                    f"备份中找不到源试验 {source_test_id}"
                )

            existing = _duplicate_name_id(dest, test["name"])
            if existing is not None:
                # 写入事务里还会再查一次，覆盖并发导入的竞态。
                raise DuplicateTestNameError(
                    f"中心库已存在同名试验 {test['name']!r}（id={existing}），拒绝迁入"
                )

            try:
                blocks = _validate_source_test(src, test)
            except sqlite3.DatabaseError as exc:
                raise BackupInvalidError(f"备份数据库无法读取：{exc}") from None
            try:
                return _copy_test(dest, src, test, blocks)
            except DuplicateTestNameError:
                raise
            except sqlite3.IntegrityError as exc:
                raise DuplicateTestNameError(
                    f"中心库已存在同名试验 {test['name']!r}，拒绝迁入"
                ) from exc
            except sqlite3.DatabaseError as exc:
                raise BackupInvalidError(f"备份数据库无法读取：{exc}") from None
        finally:
            src.close()
