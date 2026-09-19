"""极简日志存储。

M1 用纯内存 + JSONL 持久化，不引入数据库。
评测要能复现，所以场景一旦生成就落盘成 JSONL。
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from fivewhys.models import LogEntry


class LogStore:
    """一个场景内的所有日志。"""

    def __init__(self) -> None:
        self._entries: list[LogEntry] = []

    # ---- 写入 ----

    def append(self, entry: LogEntry) -> None:
        self._entries.append(entry)

    def extend(self, entries: Iterable[LogEntry]) -> None:
        self._entries.extend(entries)

    # ---- 读取 ----

    def all(self) -> list[LogEntry]:
        """返回全部日志（副本，按写入顺序）。

        注意：这里**故意不排序**。写入顺序就是时间顺序，因为生成器是按时间推进的。
        query_logs 工具应该自己做过滤和排序 —— 那是 M1 的活。
        """
        return list(self._entries)

    def clear(self) -> None:
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)

    # ---- 持久化 ----

    def dump_jsonl(self, path: Path) -> Path:
        """把日志落盘。评测复现靠它。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as fh:
            for entry in self._entries:
                fh.write(entry.model_dump_json() + "\n")
        return path

    @classmethod
    def load_jsonl(cls, path: Path) -> LogStore:
        """从 JSONL 读回来。"""
        store = cls()
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    store.append(LogEntry.model_validate_json(line))
        return store

    def stats(self) -> dict[str, int]:
        """按级别统计，方便调试场景是否生成合理。"""
        counts: dict[str, int] = {}
        for entry in self._entries:
            counts[str(entry.level)] = counts.get(str(entry.level), 0) + 1
        return counts


__all__ = ["LogStore"]
