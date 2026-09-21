"""``get_dependencies`` —— 用来「顺着调用链往下找」的工具。

## 它解决什么问题

排障里有一种很典型的情形：**症状在上游，根因在下游。**

::

    order-service 的日志里全是 502 —— 它自己没做错什么，是下游坏了
      -> 查 order-service 的日志：调 payment-service 时失败
        -> 查 payment-service：它调用 inventory-service 时失败
          -> 查 inventory-service：根因在这里

没有拓扑信息，agent 只能靠**猜**该去看哪个服务。有了它，路径是推出来的。
这也是 trace_id 之外的第二条跨服务线索：trace 给的是**这一次请求**，
拓扑给的是**谁可能影响谁**。

## 为什么两个方向都返回

- **下游**（我调用了谁）：症状在我这里、原因在下面 → 往下追
- **上游**（谁调用了 我）：原因在我这里、影响面在上面 → 评估爆炸半径

只给下游的工具会让 agent 不知道「这个服务坏了会影响到谁」。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from fivewhys.tools import Tool
from fivewhys.tools._render import blank_result, render_block


class GetDependenciesArgs(BaseModel):
    """get_dependencies 的参数。"""

    service: str | None = Field(
        default=None,
        description="服务名。不传就返回完整拓扑图（一眼看清整个系统怎么连的）",
    )


def build_get_dependencies_tool(topology: dict[str, list[str]]) -> Tool:
    """把拓扑图绑进工具里。

    Args:
        topology: ``{'order-service': ['payment-service', ...]}``。
            来自 :meth:`fivewhys.mock.topology.MockSystem.describe`。
    """

    callers: dict[str, list[str]] = {name: [] for name in topology}
    for name, downstreams in topology.items():
        for downstream in downstreams:
            callers.setdefault(downstream, []).append(name)

    def _deps(service: str | None) -> str:
        if not topology:
            return blank_result(
                "共 0 条依赖关系：这个场景没有拓扑数据",
                clue="说明场景是单服务的最小场景，可以直接在日志里找答案，不用跨服务追踪。",
            )

        if service is None:
            lines = [
                f"{name} -> {', '.join(topology[name]) if topology[name] else '（没有下游）'}"
                for name in sorted(topology)
            ]
            return render_block(
                f"共 {len(topology)} 个服务",
                meta="完整拓扑（箭头表示「调用」）",
                lines=lines,
            )

        if service not in topology:
            known = "、".join(sorted(topology))
            return blank_result(
                f"共 0 条依赖关系：拓扑里没有服务「{service}」",
                clue=f"服务名可能写错了。本场景的服务是：{known}",
            )

        downstreams = topology[service]
        upstreams = sorted(callers.get(service, []))
        lines = [
            f"下游（{service} 调用的）: "
            + (", ".join(downstreams) if downstreams else "（没有 —— 它是叶子服务）"),
            f"上游（调用 {service} 的）: "
            + (", ".join(upstreams) if upstreams else "（没有 —— 它是入口服务）"),
        ]

        # 一句话点明这条信息怎么用 —— 工具描述里也说了，这里再提醒一次，
        # 因为模型看到「上游/下游」两个词时未必立刻想到该怎么用。
        if downstreams:
            lines.append(
                f"提示：如果 {service} 的症状来自它的下游，根因可能在 "
                f"{'、'.join(downstreams)}；顺着调用链往下查。"
            )
        if upstreams:
            lines.append(
                f"提示：{'、'.join(upstreams)} 依赖 {service} —— "
                f"{service} 出问题会直接影响它们，评估影响面时要注意。"
            )

        return render_block(
            f"{service}：{len(downstreams)} 个下游，{len(upstreams)} 个上游",
            meta=f"服务={service}",
            lines=lines,
        )

    return Tool(
        name="get_dependencies",
        description=(
            "查询服务的调用依赖：它调用了谁（下游）、谁调用了它（上游）；"
            "不传服务名则返回完整拓扑图。"
            "典型用法：当某个服务的日志显示「调用下游失败」时，"
            "用这个工具确认下游到底是谁、以及下游还有没有自己的下游，"
            "顺着调用链一路追到真正的根因服务。"
            "症状在上游、根因在下游，是分布式故障最常见的样子。"
        ),
        args_model=GetDependenciesArgs,
        func=_deps,
    )


__all__ = ["GetDependenciesArgs", "build_get_dependencies_tool"]
