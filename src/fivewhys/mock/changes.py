"""变更历史 —— 配置和发布。

## 这是 agent 要够到的那个「啊哈」

日志能告诉 agent「错误从 14:30 开始」「connection wait time 飙升」，
但**说不出为什么**。真正的答案是：

    14:29:58  order-service 的 db.pool_size 从 50 改成了 5

这条信息既不在日志里（日志只说「config reloaded」，不说改了什么），
也不在指标里。它只能通过**查配置历史**拿到。

于是排障路径完整了::

    指标：错误率飙升、P95 冲到 3s
      -> 日志：14:30 开始报 deadline exceeded，时间点紧跟一次配置变更
        -> 配置：pool_size 50 -> 5          <- 根因在这里
          -> 结论：连接池被改小，导致连接耗尽

## 配置里出现答案词是**故意的**

需求 FR-2 要求「日志不得泄漏答案」。但配置里出现 ``pool_size`` 完全正确 ——
它是证据，不是泄题。区别在于：

- 日志是**现象**：agent 一搜就全看见了，不需要推理
- 配置是**证据**：agent 必须先怀疑到「配置变过」，才会去查它

这也是为什么要给配置单独做一个工具（M4 的 ``get_config``），
而不是把配置内容混进日志里。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ConfigSnapshot:
    """某个服务在某个时刻的完整配置。"""

    ts: datetime
    service: str
    values: dict[str, Any]
    note: str = ""


@dataclass(frozen=True)
class ConfigChange:
    """一次配置项的变化。"""

    ts: datetime
    service: str
    key: str
    old: Any
    new: Any

    @property
    def description(self) -> str:
        return f"{self.key}: {self.old} -> {self.new}"


@dataclass(frozen=True)
class DeployRecord:
    """一次发布记录。"""

    ts: datetime
    service: str
    version: str
    operator: str = "unknown"
    note: str = ""


@dataclass
class ConfigStore:
    """服务配置的历史快照。"""

    _snapshots: list[ConfigSnapshot] = field(default_factory=list)

    # ---- 写入 ----

    def record(self, snapshot: ConfigSnapshot) -> ConfigSnapshot:
        self._snapshots.append(snapshot)
        return snapshot

    def record_values(
        self,
        ts: datetime,
        service: str,
        values: dict[str, Any],
        note: str = "",
    ) -> ConfigSnapshot:
        return self.record(ConfigSnapshot(ts=ts, service=service, values=dict(values), note=note))

    def clear(self) -> None:
        self._snapshots.clear()

    # ---- 读取 ----

    def all(self) -> list[ConfigSnapshot]:
        return sorted(self._snapshots, key=lambda snap: snap.ts)

    def __len__(self) -> int:
        return len(self._snapshots)

    def services(self) -> list[str]:
        return sorted({snap.service for snap in self._snapshots})

    def history(
        self,
        service: str,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[ConfigSnapshot]:
        return [
            snap
            for snap in self.all()
            if snap.service == service
            and (start is None or snap.ts >= start)
            and (end is None or snap.ts <= end)
        ]

    def latest(self, service: str, at: datetime | None = None) -> ConfigSnapshot | None:
        """``at`` 时刻生效的配置（含边界）。不传 ``at`` 就取最新的一条。"""
        candidates = [
            snap for snap in self.all() if snap.service == service and (at is None or snap.ts <= at)
        ]
        return candidates[-1] if candidates else None

    def changes(
        self,
        service: str,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[ConfigChange]:
        """把快照序列 diff 成「哪一项在什么时候从什么变成了什么」。

        这是 agent 真正需要的形态 —— 它不该自己去比对两份配置，
        那是工具该干的活。
        """
        snapshots = [snap for snap in self.all() if snap.service == service]
        out: list[ConfigChange] = []

        # strict=False 是**故意的**：两个序列长度天然差一（自己 和 自己错开一位），
        # 这正是「相邻快照两两配对」的写法。strict=True 会直接报错。
        for previous, current in zip(snapshots, snapshots[1:], strict=False):
            if start is not None and current.ts < start:
                continue
            if end is not None and current.ts > end:
                continue
            for key in sorted(set(previous.values) | set(current.values)):
                old = previous.values.get(key)
                new = current.values.get(key)
                if old != new:
                    out.append(
                        ConfigChange(
                            ts=current.ts,
                            service=service,
                            key=key,
                            old=old,
                            new=new,
                        )
                    )
        return out

    # ---- 持久化 ----

    def to_jsonl(self) -> str:
        """整个仓库序列化成 JSONL 文本。落盘与快照指纹共用这一份（见 FIV-12）。"""
        return "".join(
            json.dumps(
                {
                    "ts": snap.ts.isoformat(),
                    "service": snap.service,
                    "values": snap.values,
                    "note": snap.note,
                },
                ensure_ascii=False,
            )
            + "\n"
            for snap in self.all()
        )

    def dump_jsonl(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_jsonl(), encoding="utf-8", newline="\n")
        return path

    @classmethod
    def load_jsonl(cls, path: Path) -> ConfigStore:
        store = cls()
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                data = json.loads(line)
                store.record(
                    ConfigSnapshot(
                        ts=datetime.fromisoformat(data["ts"]),
                        service=data["service"],
                        values=data["values"],
                        note=data.get("note", ""),
                    )
                )
        return store


@dataclass
class DeployStore:
    """发布历史。"""

    _records: list[DeployRecord] = field(default_factory=list)

    def record(self, record: DeployRecord) -> DeployRecord:
        self._records.append(record)
        return record

    def add(
        self,
        ts: datetime,
        service: str,
        version: str,
        operator: str = "unknown",
        note: str = "",
    ) -> DeployRecord:
        return self.record(
            DeployRecord(
                ts=ts,
                service=service,
                version=version,
                operator=operator,
                note=note,
            )
        )

    def clear(self) -> None:
        self._records.clear()

    def all(self) -> list[DeployRecord]:
        return sorted(self._records, key=lambda rec: rec.ts)

    def __len__(self) -> int:
        return len(self._records)

    def history(
        self,
        service: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[DeployRecord]:
        return [
            rec
            for rec in self.all()
            if (service is None or rec.service == service)
            and (start is None or rec.ts >= start)
            and (end is None or rec.ts <= end)
        ]

    def to_jsonl(self) -> str:
        """整个仓库序列化成 JSONL 文本。落盘与快照指纹共用这一份（见 FIV-12）。"""
        return "".join(
            json.dumps(
                {
                    "ts": rec.ts.isoformat(),
                    "service": rec.service,
                    "version": rec.version,
                    "operator": rec.operator,
                    "note": rec.note,
                },
                ensure_ascii=False,
            )
            + "\n"
            for rec in self.all()
        )

    def dump_jsonl(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_jsonl(), encoding="utf-8", newline="\n")
        return path

    @classmethod
    def load_jsonl(cls, path: Path) -> DeployStore:
        store = cls()
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                data = json.loads(line)
                store.record(
                    DeployRecord(
                        ts=datetime.fromisoformat(data["ts"]),
                        service=data["service"],
                        version=data["version"],
                        operator=data.get("operator", "unknown"),
                        note=data.get("note", ""),
                    )
                )
        return store


__all__ = [
    "ConfigChange",
    "ConfigSnapshot",
    "ConfigStore",
    "DeployRecord",
    "DeployStore",
]
