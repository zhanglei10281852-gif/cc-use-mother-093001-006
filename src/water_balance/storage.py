"""JSON 文件仓储：原子写入 + 跨进程文件锁。

整库单文件快照，所有写操作在持锁期间读改写，保证 CLI 与 API
多进程同时访问时不会互相覆盖。导入批次按内容哈希去重（幂等）。
"""
from __future__ import annotations

import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterator, Optional


def _default(obj: Any) -> Any:
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    raise TypeError(f"不可序列化类型 {type(obj)!r}")


class Repository:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")

    # ------------------------------------------------------------ 底层

    def _load(self) -> dict[str, Any]:
        if not self.path.exists() or self.path.stat().st_size == 0:
            return self._empty()
        with self.path.open("r", encoding="utf-8") as fh:
            return json.load(fh)

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {
            "schema": 1,
            "zones": {},
            "users": {},
            "service_points": {},
            "meters": {},
            "readings": {},              # key: (meter_id, read_on)
            "readings_index": {},        # meter_id -> [key...]
            "rules": {},
            "usages": {},
            "transfers": {},
            "batches": {},
            "adjustments": {},
            "versions": {},              # key: f"{zone}|{month}|{rev}"
            "version_index": {},         # "zone|month" -> [key...]
            "counters": {},
        }

    def _save(self, db: dict[str, Any]) -> None:
        fd, tmp = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=".db-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(db, fh, ensure_ascii=False, indent=1,
                          sort_keys=True, default=_default)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    @contextmanager
    def transaction(self) -> Iterator[dict[str, Any]]:
        with self.lock_path.open("a+") as lock_fh:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
            db = self._load()
            try:
                yield db
            except BaseException:
                raise
            else:
                self._save(db)

    # ------------------------------------------------------------ 便捷只读

    def snapshot(self) -> dict[str, Any]:
        with self.lock_path.open("a+") as lock_fh:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_SH)
            return self._load()

    def next_seq(self, db: dict[str, Any], name: str) -> int:
        n = int(db["counters"].get(name, 0)) + 1
        db["counters"][name] = n
        return n

    # ------------------------------------------------------------ 批次幂等

    def find_batch(self, content_hash: str) -> Optional[dict[str, Any]]:
        db = self.snapshot()
        for batch in db["batches"].values():
            if batch["content_hash"] == content_hash and \
                    batch["status"] != "failed":
                return batch
        return None

    def get_batch(self, batch_id: str) -> Optional[dict[str, Any]]:
        return self.snapshot()["batches"].get(batch_id)
