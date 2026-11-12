"""测试环境：在导入 app 之前固定数据库路径与摘要块大小。

test_api.py 模块自身也会设置这些变量（它先被导入时以它为准）；
本文件保证单独运行 test_migration.py 时同样不落到 /data。
"""

import os
import tempfile

_tmpdir = tempfile.TemporaryDirectory(prefix="pump-tests-")
os.environ.setdefault("PUMP_DB_PATH", os.path.join(_tmpdir.name, "test.db"))
os.environ.setdefault("PUMP_BLOCK_SIZE", "4")
os.environ.setdefault("PUMP_MAX_BUCKETS", "1000")
