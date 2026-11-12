"""试验迁入测试：编号 / 行号重叠、摘要边界切块、失败原子性与各类校验错误。"""

from __future__ import annotations

import io
import os
import sqlite3
import tempfile
from pathlib import Path

import pytest

from fastapi.testclient import TestClient

from app.db import connect, init_db
from app.ingest import import_test
from app.main import app

init_db()
client = TestClient(app)

BLOCK_SIZE = int(os.environ.get("PUMP_BLOCK_SIZE", "4"))


def _csv(rows, header=("timestamp_ms", "pressure_kpa")) -> bytes:
    buf = io.StringIO()
    buf.write(",".join(header) + "\n")
    for ts, p in rows:
        buf.write(f"{int(ts)},{p}\n")
    return buf.getvalue().encode("utf-8")


def _build_backup(path: Path, name: str, rows, block_size: int = BLOCK_SIZE) -> int:
    """用同版导入逻辑生成一份独立源库备份，返回源试验编号。"""
    init_db(path)
    conn = connect(path)
    try:
        result = import_test(
            conn,
            name=name,
            filename=f"{name}.csv",
            content=_csv(rows),
            block_size=block_size,
        )
        conn.commit()
        return result["id"]
    finally:
        conn.close()


_LAST_BACKUP: dict[str, tuple[int, bytes]] = {}


def _backup_bytes(source_id: int, name: str = "migrate-src") -> bytes:
    """30 点、压力有并列极值；block_size=4 -> level1 8 块，level2 2 块，level3 1 块。

    每场试验都用同一份固定内容，因此不同名字的备份除了 tests.name 外完全一致。
    """
    pressures = [
        100, 105, 95, 105, 90, 110, 90, 120,
        100, 104, 96, 104, 91, 109, 92, 121,
        101, 106, 94, 106, 89, 111, 93, 122,
        102, 103, 97, 103, 88, 112,
    ]
    rows = [(5000 + i * 10, float(p)) for i, p in enumerate(pressures)]
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "src.db"
        sid = _build_backup(path, name, rows)
        # 每次新建的备份库里只有这一场试验，源编号固定为 1
        assert sid == 1
        data = path.read_bytes()
    _LAST_BACKUP[name] = (sid, data)
    return data


def _source_rowids(name: str = "migrate-src") -> tuple[int, int]:
    """备份中源试验采样 rowid 的起止；用作中心库里的重叠参照。"""
    _, data = _LAST_BACKUP[name]
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "src.db"
        path.write_bytes(data)
        conn = sqlite3.connect(path)
        lo, hi = conn.execute(
            "SELECT MIN(rowid), MAX(rowid) FROM samples WHERE test_id = 1"
        ).fetchone()
        conn.close()
    return lo, hi


def _migrate(source_id: int, name: str = "migrate-src", data: bytes | None = None):
    if data is None:
        data = _backup_bytes(source_id, name)
    return client.post(
        "/tests/migrate",
        data={"source_test_id": str(source_id)},
        files={"file": ("backup.db", data, "application/x-sqlite3")},
    )


def _query(test_id: int, **params):
    resp = client.get(f"/tests/{test_id}/query", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _subset(body: dict) -> dict:
    return {
        "bucket_count": body["bucket_count"],
        "missing_bucket_count": body["missing_bucket_count"],
        "metrics": body["metrics"],
        "buckets": body["buckets"],
    }


@pytest.fixture(scope="module")
def migrated():
    data = _backup_bytes(1)
    resp = _migrate(1, data=data)
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------- 迁入成功路径

def test_migrate_returns_new_central_id(migrated):
    assert migrated["source_test_id"] == 1
    # 单元测试库里已有 unit-test-1 等试验，新编号不可能沿用源编号的语义
    assert migrated["id"] >= 1
    assert migrated["sample_count"] == 30
    assert migrated["block_counts"] == {"1": 8, "2": 2, "3": 1}
    central = client.get(f"/tests/{migrated['id']}").json()
    assert central["name"] == migrated["name"]
    assert central["start_ts"] == 5000 and central["end_ts"] == 5290


def test_migrated_queries_match_source_central_and_source(migrated):
    """按新编号查询的结果、blocks_by_level 与 raw_rows_read 必须与源库一致。"""
    central_id = migrated["id"]

    # 覆盖多种桶宽：与 level1 块对齐、切开最细块、末桶截断、纯缺测区间
    cases = [
        {"start_ts": 5000, "end_ts": 5290, "bucket_width_ms": 40},
        {"start_ts": 5000, "end_ts": 5320, "bucket_width_ms": 40},
        {"start_ts": 5030, "end_ts": 5270, "bucket_width_ms": 10},
        {"start_ts": 5005, "end_ts": 5285, "bucket_width_ms": 7},
        {"start_ts": 4990, "end_ts": 5300, "bucket_width_ms": 13},
        {"start_ts": 5000, "end_ts": 5120, "bucket_width_ms": 120},
        {"start_ts": 4800, "end_ts": 4900, "bucket_width_ms": 20},
    ]
    # 源库：直接打开备份副本，按源试验编号查询
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "src.db"
        path.write_bytes(_LAST_BACKUP["migrate-src"][1])
        source_conn = connect(path)
        try:
            for params in cases:
                central_body = _query(central_id, **params)
                assert central_body["test_name"] == migrated["name"]

                from app.service import bucket_query

                src_body = bucket_query(
                    source_conn, 1,
                    params["start_ts"], params["end_ts"], params["bucket_width_ms"],
                ).model_dump()
                assert _subset(central_body) == _subset(src_body), params
        finally:
            source_conn.close()


def test_new_id_with_overlapping_sample_rowids(migrated):
    """中心库与源库采样 rowid 完全重叠，但摘要边界只指向本试验自己的新采样行。"""
    conn = connect()
    try:
        src_lo, src_hi = _source_rowids()
        rows = conn.execute(
            "SELECT MIN(rowid), MAX(rowid) FROM samples WHERE test_id = ?",
            (migrated["id"],),
        ).fetchone()
        assert rows[0] is not None and rows[1] is not None
        # 行号重叠：中心库这场试验的采样 rowid 区间与源库区间有交集
        assert max(rows[0], src_lo) <= min(rows[1], src_hi)

        bad = conn.execute(
            """SELECT COUNT(*) FROM blocks b
               JOIN samples s1 ON s1.rowid = b.first_rowid
               JOIN samples s2 ON s2.rowid = b.last_rowid
               WHERE b.test_id = ? AND (s1.test_id <> ? OR s2.test_id <> ?)""",
            (migrated["id"], migrated["id"], migrated["id"]),
        ).fetchone()[0]
        assert bad == 0
    finally:
        conn.close()


def test_boundary_cut_matches_source_raw_read_count(migrated):
    """桶宽 15ms 会切开最细 level1 块：raw_rows_read / blocks_by_level 必须与源一致。"""
    params = {"start_ts": 5015, "end_ts": 5245, "bucket_width_ms": 15}
    central_body = _query(migrated["id"], **params)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "src.db"
        path.write_bytes(_LAST_BACKUP["migrate-src"][1])
        conn = connect(path)
        try:
            from app.service import bucket_query

            src_body = bucket_query(
                conn, 1, params["start_ts"], params["end_ts"], params["bucket_width_ms"]
            ).model_dump()
        finally:
            conn.close()
    assert _subset(central_body) == _subset(src_body)
    assert central_body["metrics"]["raw_rows_read"] > 0
    assert sum(b["sample_count"] for b in central_body["buckets"]) == 23


def test_randomized_bucket_parity_with_source(migrated):
    """固定种子随机查询：迁入后按新编号的结果与源库逐字段一致。"""
    import random

    from app.service import bucket_query

    data = _LAST_BACKUP["migrate-src"][1]
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "src.db"
        path.write_bytes(data)
        conn = connect(path)
        try:
            rng = random.Random(20261112)
            for _ in range(60):
                start = rng.randint(4800, 5400)
                width = rng.choice([1, 2, 5, 7, 10, 13, 40, 80, 128, 500])
                end = start + width * rng.randint(1, 12)
                central_body = _query(
                    migrated["id"],
                    start_ts=start,
                    end_ts=end,
                    bucket_width_ms=width,
                )
                src_body = bucket_query(conn, 1, start, end, width).model_dump()
                assert _subset(central_body) == _subset(src_body), (
                    start, end, width
                )
        finally:
            conn.close()


# ---------------------------------------------------------------- 失败路径

def test_missing_source_test_rejected(migrated):
    data = _backup_bytes(1, name="another-src")
    resp = client.post(
        "/tests/migrate",
        data={"source_test_id": "999"},
        files={"file": ("backup.db", data, "application/x-sqlite3")},
    )
    assert resp.status_code == 404
    assert "999" in resp.json()["detail"]


def test_duplicate_name_rejected(migrated):
    data = _backup_bytes(1, name=migrated["name"])
    resp = client.post(
        "/tests/migrate",
        data={"source_test_id": "1"},
        files={"file": ("backup.db", data, "application/x-sqlite3")},
    )
    assert resp.status_code == 409
    assert "同名" in resp.json()["detail"]


def test_corrupt_summary_first_last_reference_rejected(migrated):
    data = _backup_bytes(1, name="corrupt-src")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "corrupt.db"
        path.write_bytes(data)
        conn = sqlite3.connect(path)
        # 把首尾 rowid 改成“别人的”合法 rowid（源库里另一场试验的行）
        conn.execute(
            "INSERT INTO tests (id, name, source_filename, sample_count, start_ts, end_ts, max_level) "
            "VALUES (2, 'other-in-backup', 'x.csv', 1, 9999, 9999, 1)"
        )
        other_rid = conn.execute(
            "INSERT INTO samples (test_id, ts, pressure) VALUES (2, 9999, 1.0) "
            "RETURNING rowid"
        ).fetchone()[0]
        conn.execute(
            "UPDATE blocks SET first_rowid = ? "
            "WHERE test_id = 1 AND level = 1 AND block_index = 1",
            (other_rid,),
        )
        conn.commit()
        conn.close()
        broken = path.read_bytes()
    resp = client.post(
        "/tests/migrate",
        data={"source_test_id": "1"},
        files={"file": ("corrupt.db", broken, "application/x-sqlite3")},
    )
    assert resp.status_code == 422
    assert "首尾采样对不上" in resp.json()["detail"]


def test_summary_rowid_out_of_range_rejected(migrated):
    data = _backup_bytes(1, name="rowid-out-src")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "out.db"
        path.write_bytes(data)
        conn = sqlite3.connect(path)
        conn.execute(
            "UPDATE blocks SET first_rowid = last_rowid + 1000 "
            "WHERE test_id = 1 AND level = 1 AND block_index = 1"
        )
        conn.commit()
        conn.close()
        broken = path.read_bytes()
    resp = client.post(
        "/tests/migrate",
        data={"source_test_id": "1"},
        files={"file": ("out.db", broken, "application/x-sqlite3")},
    )
    assert resp.status_code == 422
    assert "首尾采样对不上" in resp.json()["detail"]


def test_summary_pressure_mismatch_rejected(migrated):
    data = _backup_bytes(1, name="bad-pressure-src")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "bad.db"
        path.write_bytes(data)
        conn = sqlite3.connect(path)
        conn.execute(
            "UPDATE blocks SET first_pressure = first_pressure + 1 "
            "WHERE test_id = 1 AND level = 2 AND block_index = 0"
        )
        conn.commit()
        conn.close()
        broken = path.read_bytes()
    resp = client.post(
        "/tests/migrate",
        data={"source_test_id": "1"},
        files={"file": ("bad.db", broken, "application/x-sqlite3")},
    )
    assert resp.status_code == 422


def test_not_a_sqlite_file_rejected():
    resp = client.post(
        "/tests/migrate",
        data={"source_test_id": "1"},
        files={"file": ("x.db", b"not a database", "application/octet-stream")},
    )
    assert resp.status_code == 400


def test_failed_migration_leaves_central_db_unchanged(migrated):
    before_tests = client.get("/tests").json()
    before_counts = {
        t["id"]: _query(t["id"], start_ts=0, end_ts=10_000, bucket_width_ms=1000)["buckets"]
        for t in before_tests
    }

    data = _backup_bytes(1, name="will-fail-src")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "fail.db"
        path.write_bytes(data)
        conn = sqlite3.connect(path)
        # 破坏 level 1 最细块的尾点压力 -> 校验失败
        conn.execute(
            "UPDATE blocks SET last_pressure = 999 WHERE test_id = 1 "
            "AND level = 1 AND block_index = 1"
        )
        conn.commit()
        conn.close()
        broken = path.read_bytes()

    resp = client.post(
        "/tests/migrate",
        data={"source_test_id": "1"},
        files={"file": ("fail.db", broken, "application/x-sqlite3")},
    )
    assert resp.status_code == 422

    after_tests = client.get("/tests").json()
    assert after_tests == before_tests
    conn = connect()
    try:
        leftover = conn.execute(
            "SELECT COUNT(*) FROM samples WHERE test_id IN "
            "(SELECT id FROM tests WHERE name = 'will-fail-src')"
        ).fetchone()[0]
        assert leftover == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM tests WHERE name = 'will-fail-src'"
        ).fetchone()[0] == 0
    finally:
        conn.close()
    assert {
        t["id"]: _query(t["id"], start_ts=0, end_ts=10_000, bucket_width_ms=1000)["buckets"]
        for t in after_tests
    } == before_counts


def test_missing_schema_rejected():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "empty.db"
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE unrelated (x INTEGER)")
        conn.commit()
        conn.close()
        data = path.read_bytes()
    resp = client.post(
        "/tests/migrate",
        data={"source_test_id": "1"},
        files={"file": ("empty.db", data, "application/x-sqlite3")},
    )
    assert resp.status_code == 400
    assert "同版" in resp.json()["detail"]


def test_migrate_requires_source_id():
    data = _backup_bytes(1, name="no-form-src")
    resp = client.post(
        "/tests/migrate", files={"file": ("b.db", data, "application/x-sqlite3")}
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------- 服务端备份导出

def test_exported_backup_is_single_file_and_migratable(migrated):
    resp = client.post("/tests/backup")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    path = Path(body["path"])
    assert path.exists() and body["size_bytes"] == path.stat().st_size
    assert body["test_count"] == len(client.get("/tests").json())
    # 单文件：不应有 -wal / -shm 伴生文件
    assert not Path(str(path) + "-wal").exists()
    assert not Path(str(path) + "-shm").exists()

    # 导出的备份可直接迁入另一个中心库，且与源库查询结果一致
    with tempfile.TemporaryDirectory() as tmp:
        other_path = Path(tmp) / "other.db"
        init_db(other_path)
        other = connect(other_path)
        try:
            result = migrate_backup_bytes(other, path.read_bytes(), migrated["id"])
            assert result["source_test_id"] == migrated["id"]
            assert result["name"] == migrated["name"]
            assert result["block_counts"] == migrated["block_counts"]
            for params in (
                {"start_ts": 5000, "end_ts": 5290, "bucket_width_ms": 40},
                {"start_ts": 5015, "end_ts": 5245, "bucket_width_ms": 15},
            ):
                a = _query(migrated["id"], **params)
                from app.service import bucket_query

                b = bucket_query(
                    other, result["id"],
                    params["start_ts"], params["end_ts"], params["bucket_width_ms"],
                ).model_dump()
                assert _subset(a) == _subset(b)
        finally:
            other.close()


def migrate_backup_bytes(conn, data: bytes, source_id: int):
    from app.migration import migrate_from_backup

    return migrate_from_backup(conn, data, source_id)
