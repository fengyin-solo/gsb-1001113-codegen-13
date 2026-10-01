"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。
跨模块写入统一走 transaction：先快照、后落账、出错整体回滚，版本号由 next_version 单调分配。
"""
from __future__ import annotations

import copy
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from app.seed import SEED_ROWS


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        self._lock = threading.RLock()
        self._versions: dict[str, int] = {}

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    @contextmanager
    def transaction(self, *modules: str) -> Iterator[None]:
        """把多个模块的写入包进一次事务：任一环节抛错就整体回滚到进入前的状态。"""
        with self._lock:
            snapshot = {name: copy.deepcopy(self.rows(name)) for name in modules}
            try:
                yield
            except Exception:
                for name, rows in snapshot.items():
                    self._tables[name] = rows
                raise

    def next_version(self, key: str) -> int:
        """按键分配单调递增的版本号，跨模块写入与命令序号都从这里取。"""
        with self._lock:
            self._versions[key] = self._versions.get(key, 0) + 1
            return self._versions[key]

    def current_version(self, key: str) -> int:
        return self._versions.get(key, 0)

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()
