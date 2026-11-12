#!/usr/bin/env python3
"""生成“试验迁入”演示用的两份材料（可重复运行）。

输出：
    sample_data/acceptance_baseline.csv
        先导入中心库的 CSV：120 点、10 ms 间隔、压力平稳在 100 kPa 附近。
        导入后中心库试验编号为 1、采样 rowid 为 1..120。
    sample_data/legacy_backup.db
        同版服务生成的 SQLite 单文件备份：仅含一场试验 “legacy-pump-001”，
        源试验编号为 1、采样 rowid 为 1..300，压力为启泵水锤曲线。
        与中心库基线试验的试验编号、采样行号刻意重叠，用于演示迁入时重新分配。

用法（仓库根目录）：
    python3 scripts/generate_backup.py
"""

from __future__ import annotations

import csv
import math
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app import config  # noqa: E402
from app.db import backup_to, connect, init_db  # noqa: E402
from app.ingest import import_test  # noqa: E402

BASE_TS = 1_731_300_000_000
OUT_DIR = REPO_ROOT / "sample_data"


def baseline_rows(count: int = 120, interval_ms: int = 10):
    """验收基线：平稳压力，与源试验的峰谷明显不同。"""
    return [
        (BASE_TS + i * interval_ms, round(100.0 + 0.5 * (i % 7) - 1.5, 3))
        for i in range(count)
    ]


def source_rows(count: int = 300, interval_ms: int = 10):
    """待迁入试验：静压 -> 爬升 -> 水锤衰减振荡，含一次深谷与一次尖峰。"""
    rows = []
    for i in range(count):
        t = i * interval_ms
        if t < 200:
            p = 100.0
        elif t < 500:
            p = 100.0 + 220.0 * (t - 200) / 300.0
        else:
            dt = t - 500
            steady = 320.0
            oscillation = 55.0 * math.exp(-dt / 1200.0) * math.sin(
                2 * math.pi * 1.5 * dt / 1000.0
            )
            dip = -60.0 * math.exp(-((dt - 60) / 30.0) ** 2)
            p = steady + oscillation + dip
        rows.append((BASE_TS + t, round(p, 3)))
    return rows


def write_csv(path: Path, rows) -> bytes:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp_ms", "pressure_kpa"])
        for ts, p in rows:
            writer.writerow([ts, p])
    return path.read_bytes()


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    csv_path = OUT_DIR / "acceptance_baseline.csv"
    write_csv(csv_path, baseline_rows())
    print(f"wrote {csv_path} ({len(baseline_rows())} rows)")

    db_path = OUT_DIR / "legacy_backup.db"
    work_path = OUT_DIR / ".legacy_work.db"
    for suffix in ("", "-wal", "-shm"):
        Path(str(db_path) + suffix).unlink(missing_ok=True)
        Path(str(work_path) + suffix).unlink(missing_ok=True)

    init_db(work_path)
    conn = connect(work_path)
    try:
        result = import_test(
            conn,
            name="legacy-pump-001",
            filename="legacy-pump-001.csv",
            content=write_csv(OUT_DIR / ".legacy_source.csv", source_rows()),
            block_size=config.BLOCK_SIZE,
        )
        conn.commit()
        print(
            f"wrote {db_path}: source test id={result['id']} "
            f"samples={result['sample_count']} levels={result['block_counts']}"
        )
    finally:
        conn.close()
    (OUT_DIR / ".legacy_source.csv").unlink(missing_ok=True)

    # 用服务自己的备份 API 导出单文件，确保没有 -wal/-shm 伴生文件
    backup_to(work_path, db_path)
    for suffix in ("", "-wal", "-shm"):
        Path(str(work_path) + suffix).unlink(missing_ok=True)
    for suffix in ("-wal", "-shm"):
        Path(str(db_path) + suffix).unlink(missing_ok=True)


if __name__ == "__main__":
    main()
