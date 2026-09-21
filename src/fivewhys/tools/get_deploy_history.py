"""``get_deploy_history`` —— 既用来找嫌疑，也用来**排除**嫌疑。

## 它有两个方向

这个工具被调用的理由，有一半是**为了排除嫌疑人**：

- **找嫌疑**：错误从 14:30 开始 → 14:28 刚发过一版 → 嫌疑很大
- **排除嫌疑**：错误从 14:30 开始，但最近一次发布在 12:00 → 不是发布引起的，别在这上面浪费时间

第二种用法同样重要。一个只会顺着嫌疑跑、不会排除的 agent，
会把步数全花在「查了但没用」的路径上 —— 那是 M7 要优化的东西。

## 为什么发布记录和配置变化要分开成两个工具

它们看起来都是「变更」，但**证据强度完全不同**：

- 发布记录只说「发了 v2.3.1」，**不说改了什么** —— 它是**线索**
- 配置历史说「db.pool_size 从 50 改成 5」 —— 它是**证据**

混成一个 ``check_recent_changes`` 是很自然的想法（那也是 M7 的变体 B），
但那样 agent 就分不清「一条改了什么都不知道的发布记录」和「一条明确的配置变更」
哪个更值得追。**这一版按数据源切开，就是为了 M7 能拿它当对照组。**
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from pydantic import BaseModel, Field

from fivewhys.mock.changes import DeployStore
from fivewhys.tools import Tool
from fivewhys.tools._render import blank_result, fit_lines, render_block, unknown_service

MAX_RECORDS = 40


class GetDeployHistoryArgs(BaseModel):
    """get_deploy_history 的参数。"""

    service: str | None = Field(
        default=None,
        description="服务名。不传就列出所有服务的发布记录（跨服务排查时有用）",
    )
    start: datetime | None = Field(default=None, description="时间窗口起点，不传则不限")
    end: datetime | None = Field(default=None, description="时间窗口终点，不传则不限")
    limit: int = Field(default=20, le=MAX_RECORDS, description="最多返回多少条")


def build_get_deploy_history_tool(deploys: DeployStore, *, services: Sequence[str] = ()) -> Tool:
    """把 DeployStore 绑进工具里。"""

    def _history(
        service: str | None,
        start: datetime | None,
        end: datetime | None,
        limit: int,
    ) -> str:
        if start is not None and end is not None and end < start:
            return (
                f"参数有误：end（{end:%H:%M:%S}）早于 start（{start:%H:%M:%S}）。"
                "请给出正确的时间窗口。"
            )

        known = list(services) or sorted({record.service for record in deploys.all()})
        if service is not None and known and service not in known:
            return unknown_service(service, known)

        records = deploys.history(service, start, end)
        scope = service or "全部服务"
        meta_parts = [f"服务={scope}"]
        if start is not None or end is not None:
            left = f"{start:%H:%M:%S}" if start is not None else "不限"
            right = f"{end:%H:%M:%S}" if end is not None else "不限"
            meta_parts.append(f"时间={left}~{right}")
        meta = "  ".join(meta_parts)

        if not records:
            return blank_result(
                f"共 0 条发布记录：{scope} 在指定范围内没有发布",
                clue=(
                    "如果时间窗口内本来就没有发布，说明这次的故障**不是发布引起的** —— "
                    "这是一条排除性证据，可以放心把发布排除掉。"
                    "（如果连时间范围都没给，那就是这个服务真的没有发布记录）"
                ),
                meta=meta,
            )

        lines, truncated = fit_lines(
            (
                f"{record.ts:%H:%M:%S}  {record.service:<18} {record.version:<10} "
                f"by {record.operator}" + (f"  note: {record.note}" if record.note else "")
                for record in records
            ),
            limit=limit,
        )

        latest = records[-1]
        summary = (
            f"共 {len(records)} 条发布记录，显示 {len(lines)} 条；"
            f"最近一次是 {latest.ts:%H:%M:%S} 的 {latest.service} {latest.version}"
        )
        return render_block(
            summary,
            note="（受 limit 和输出预算限制，已截断）" if truncated else None,
            meta=meta,
            lines=lines,
        )

    return Tool(
        name="get_deploy_history",
        description=(
            "查询服务的发布记录（谁在什么时候发了哪个版本）。"
            "**它有两个用法，同样重要**："
            "一是找嫌疑 —— 如果故障开始的时间点紧跟一次发布，那这次发布很可疑；"
            "二是排除嫌疑 —— 如果故障开始前很久都没有发布，就可以把发布排除掉，"
            "不必再在这条路径上花时间。"
            "注意：发布记录只说「发了什么版本」，**不说改了什么**；"
            "想知道具体改了哪个配置项，用 get_config。"
        ),
        args_model=GetDeployHistoryArgs,
        func=_history,
    )


__all__ = ["MAX_RECORDS", "GetDeployHistoryArgs", "build_get_deploy_history_tool"]
