"""``get_config`` —— 答案最常藏在这里的那件工具。

## 为什么单独给它一个工具

需求里有一条设计纪律：**日志是现象，配置是证据。**

日志能告诉 agent「错误从 14:30 开始」「connection wait time 飙升」，
但说不出**为什么**。真正的答案长这样::

    14:29:58  order-service  db.pool_size: 50 -> 5

这条信息**不在日志里**（日志只说 "config reloaded"），也不在指标里。
它只能通过查配置历史拿到 —— 而 agent 必须先**怀疑到**「配置变过」才会来查。

把配置混进日志里，这个场景就没有难度了；单独做成工具，
agent 就得自己走到这一步。这正是本项目要考验的推理能力。

## 返回什么

两样东西，缺一不可：

1. **变化**（``db.pool_size: 50 -> 5``）—— 这是「哪里变了」
2. **当前生效的配置全貌** —— 这是「现在是什么样」

只给变化，agent 不知道变化相对于什么；只给全貌，它得自己 diff。
**工具该干的活不要推给模型**（和 :mod:`fivewhys.mock.changes` 里 diff 成
``ConfigChange`` 是同一个理由）。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from pydantic import BaseModel, Field

from fivewhys.mock.changes import ConfigStore
from fivewhys.tools import Tool
from fivewhys.tools._render import blank_result, fit_lines, render_block, unknown_service

# 一次最多列多少条变化 / 多少个配置项。配置通常不多，给宽一点。
MAX_CHANGES = 50
MAX_ITEMS = 60


class GetConfigArgs(BaseModel):
    """get_config 的参数。"""

    service: str = Field(description="服务名，例如 order-service")
    at: datetime | None = Field(
        default=None,
        description="看这个时刻（ISO 8601）生效的配置。不传就看最新一份",
    )
    since: datetime | None = Field(
        default=None,
        description=(
            "只列这个时刻（ISO 8601）之后的配置变化。"
            "典型用法：从 query_logs 拿到「错误从 14:30 开始」，"
            "就传 since=14:25 看之前几分钟改过什么"
        ),
    )
    include_values: bool = Field(
        default=True,
        description="是否附带当前生效的完整配置（默认带上，方便和变化对照）",
    )


def build_get_config_tool(configs: ConfigStore, *, services: Sequence[str] = ()) -> Tool:
    """把 ConfigStore 绑进工具里。"""

    def _get(
        service: str,
        at: datetime | None,
        since: datetime | None,
        include_values: bool,
    ) -> str:
        if at is not None and since is not None and at < since:
            return (
                f"参数有误：at（{at:%H:%M:%S}）早于 since（{since:%H:%M:%S}）。"
                "since 是「从什么时候开始看变化」，应该早于 at。"
            )

        known = services or configs.services()
        if known and service not in known:
            return unknown_service(service, known)

        snapshots = configs.history(service, end=at)
        effective = snapshots[-1] if snapshots else None

        meta_parts = [f"服务={service}"]
        if since is not None:
            meta_parts.append(f"变化自 {since:%H:%M:%S} 起")
        if effective is not None:
            meta_parts.append(f"{effective.ts:%H:%M:%S} 生效")
        meta = "  ".join(meta_parts)

        if effective is None:
            return blank_result(
                f"共 0 条配置快照：没有 {service} 的配置记录",
                clue=(
                    "服务名是对的，所以这表示**这个服务没有配置快照**（有些服务确实不落）。"
                    "注意别把它当成「配置没问题」—— 它只说明这条路径查不到东西，"
                    "该换一条证据（日志 / 下游服务）继续查。"
                ),
                meta=meta,
            )

        changes = configs.changes(service, start=since, end=at)
        lines: list[str] = []

        # ---- 第一段：变化 ----
        if changes:
            change_lines, change_truncated = fit_lines(
                (
                    f"{change.ts:%H:%M:%S}  {change.key}: {change.old} -> {change.new}"
                    for change in changes
                ),
                limit=MAX_CHANGES,
            )
            lines.append(f"--- 配置变化（{len(changes)} 次）---")
            lines.extend(change_lines)
            if change_truncated:
                lines.append(f"（变化过多，只显示前 {MAX_CHANGES} 条）")
        else:
            lines.append("--- 配置变化（0 次）---")
            lines.append(
                "该服务在这段时间里**没有任何配置变化**"
                + ("（换个时间窗口或去掉 since 再看看）" if since else "")
            )

        # ---- 第二段：当前生效的配置 ----
        if include_values:
            items, items_truncated = fit_lines(
                (f"  {key} = {value}" for key, value in sorted(effective.values.items())),
                limit=MAX_ITEMS,
            )
            lines.append(f"--- {effective.ts:%H:%M:%S} 生效的配置（{len(effective.values)} 项）---")
            lines.extend(items)
            if items_truncated:
                lines.append(f"（配置项过多，只显示前 {MAX_ITEMS} 项）")
            if effective.note:
                lines.append(f"  note: {effective.note}")

        summary = f"查到 {len(changes)} 次配置变化，{len(snapshots)} 份配置快照"
        return render_block(summary, meta=meta, rows=lines)

    return Tool(
        name="get_config",
        description=(
            "查询某个服务的配置历史：哪些配置项被改过、什么时候改的、改成了什么，"
            "以及某个时刻生效的完整配置。\n"
            "**排障的第三步，也是最可能找到根因的一步**：配置变更是根因最常见的藏身处。"
            "当指标和日志都指向某个时间点时，用 since 查那个时间点**之前几分钟**"
            "的配置变化，往往能直接看到答案。\n"
            "局限：它只覆盖你传进去的**那一个服务**。这个服务没有变化不等于没问题 —— "
            "根因可能在它调用的下游（用 get_dependencies 找下游是谁），"
            "或者是某次发布引起的（用 get_deploy_history）。\n"
            '典型调用：get_config(service="order-service", '
            'since="2026-01-01T13:57:00Z")'
        ),
        args_model=GetConfigArgs,
        func=_get,
    )


__all__ = ["MAX_CHANGES", "MAX_ITEMS", "GetConfigArgs", "build_get_config_tool"]
