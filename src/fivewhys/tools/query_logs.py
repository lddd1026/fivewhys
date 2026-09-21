"""`query_logs` —— 让 agent「看见」日志的工具。

## 为什么返回字符串，而不是结构化数据

工具返回值会**直接进模型的上下文**。所以它不是给程序读的，是给模型读的：

- 一行一条，格式紧凑
- 带一个头部说明「共命中几条、显示几条」
- 有**预算上限**，不能把几万行塞进上下文

这也是 NFR-11 存在的原因。

**返回格式的拼装、预算裁剪、空结果写法现在是 5 个工具共用的**
（见 :mod:`fivewhys.tools._render`）—— 预算必须只有一份实现，
否则它就不是预算。

## 关于预算为什么按「字符数」而不是「token 数」

NFR-11 要求单次返回 ≤ 2000 token。理论上应该直接数 token，但：

``tiktoken``（litellm 的依赖）**首次使用需要联网下载编码表**。而 FR-14a 要求
「无需 API Key、离线也能跑通自检」。为了一个预算估算引入一个联网依赖，不划算。

所以这里按**字符数**做预算，用保守比例换算（英文约 3 字符/token）：

    6000 字符 ÷ 3 ≈ 2000 token

等 M7 需要精确成本核算时，再评估是否引入真正的 tokenizer。

## 关于空结果

查不到日志**不是错误，是线索**。工具应该明确告诉模型这一点，而不是干巴巴地
返回「无数据」—— 后者会让模型以为工具坏了，从而放弃这条路径。

## 关于 trace 提示

日志里带 ``trace=xxx`` 时，同一个 trace 下的其它日志是**同一次请求**。
这是排障的核心手法（分布式追踪），所以在工具描述里明确教给模型。

⚠️ 既然教了，就**必须真的能做到**：``keyword`` 过滤同时匹配 ``message``
和 ``trace_id`` 两个字段。trace_id 是独立字段、不在 message 里，
只匹配 message 的话这个手法就落空了 —— 这个 bug 是手工验证发现的，
单测没抓到（因为单测只试了 message 匹配）。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from pydantic import BaseModel, Field

from fivewhys.mock.logstore import LogStore
from fivewhys.tools import Tool
from fivewhys.tools._render import (
    MAX_RESPONSE_CHARS,
    blank_result,
    fit_lines,
    render_block,
    unknown_service,
)

# 从 _render 转出来，保持 `from fivewhys.tools.query_logs import MAX_RESPONSE_CHARS` 可用。
# 预算现在由五个工具共用一份实现（见 _render 的说明），但这个名字在 FIV-2 就公开了。
__all__ = ["MAX_RESPONSE_CHARS", "QueryLogsArgs", "build_query_logs_tool"]


class QueryLogsArgs(BaseModel):
    """query_logs 的参数。字段 description 会直接给模型看，措辞很重要。"""

    service: str = Field(description="服务名，例如 order-service")
    start: datetime = Field(description="时间窗口起点（ISO 8601）")
    end: datetime = Field(description="时间窗口终点（ISO 8601）")
    levels: list[str] = Field(
        default_factory=lambda: ["WARN", "ERROR"],
        description="要看的日志级别。默认只看 WARN/ERROR，避免被 INFO 淹没",
    )
    keyword: str | None = Field(
        default=None,
        description=(
            "可选的关键字过滤（不区分大小写的子串匹配）。"
            "常见用法：传一个 trace_id 查同一次请求的完整链路，"
            "或传某个错误里的词缩小范围"
        ),
    )
    limit: int = Field(default=50, le=200, description="最多返回多少条")


def build_query_logs_tool(store: LogStore, *, services: Sequence[str] = ()) -> Tool:
    """把 LogStore 绑进工具里。

    用闭包而不是全局变量：测试时可以给每个场景一个独立的 store，
    并发跑多个场景时不会互相污染。

    Args:
        store: 日志仓库。
        services: 这个场景里存在的服务名。用来在模型写错服务名时纠正它 ——
            日志仓库本身没有「服务清单」，光看它分不清「服务不存在」
            和「服务存在但这段窗口没有日志」。
    """

    def _query(
        service: str,
        start: datetime,
        end: datetime,
        levels: list[str],
        keyword: str | None,
        limit: int,
    ) -> str:
        if end < start:
            return (
                f"参数有误：end（{end:%H:%M:%S}）早于 start（{start:%H:%M:%S}）。"
                "请给出正确的时间窗口。"
            )

        if services and service not in services:
            return unknown_service(service, services)

        # 级别统一成大写，容忍模型传 "warn" 这种写法
        wanted = {lv.strip().upper() for lv in levels}
        kw = keyword.lower() if keyword else None

        matched = [
            entry
            for entry in store.all()
            if entry.service == service
            and start <= entry.ts <= end
            and entry.level.value in wanted
            and (
                kw is None
                or kw in entry.message.lower()
                # trace_id 是独立字段，不在 message 里。
                # 不单独匹配它的话，「用 trace_id 追同一次请求」这个手法就做不到 ——
                # 而工具描述里明确教了模型这么用。
                or (entry.trace_id is not None and kw in entry.trace_id.lower())
            )
        ]
        matched.sort(key=lambda entry: entry.ts)

        total = len(matched)
        window = f"{start:%H:%M:%S}~{end:%H:%M:%S}"
        level_text = "/".join(sorted(wanted))

        # 空结果也是线索 —— 明确告诉模型这一点，避免它以为工具坏了
        if total == 0:
            hint = f"，关键字「{keyword}」" if keyword else ""
            return blank_result(
                f"共命中 0 条：{service} 在 {window} 之间没有 {level_text} 级别的日志{hint}",
                clue=(
                    "该服务在这个时间窗口内没有异常。"
                    "可以试试放宽时间窗口、换一个服务，或降低级别过滤。"
                ),
            )

        lines, truncated = fit_lines(
            (
                f"{entry.ts:%H:%M:%S} {entry.level.value:<5} {entry.message}"
                + (f"  trace={entry.trace_id}" if entry.trace_id else "")
                for entry in matched
            ),
            limit=limit,
        )

        meta = f"服务={service}  时间={window}  级别={level_text}"
        if keyword:
            meta += f"  关键字={keyword}"

        return render_block(
            f"共命中 {total} 条，显示 {len(lines)} 条",
            note=(
                f"（受 limit={limit} 和 {MAX_RESPONSE_CHARS} 字符预算限制，"
                "如需更多请缩小时间窗口或加关键字）"
                if truncated
                else None
            ),
            meta=meta,
            lines=lines,
        )

    return Tool(
        name="query_logs",
        description=(
            "查询某个服务在指定时间窗口内的日志。"
            "排障的第一步通常就是它：先看有没有报错，再看报错从什么时间点开始。"
            "默认只返回 WARN/ERROR，避免被 INFO 日志淹没。"
            "如果结果为空，说明这个服务在这个时间窗口内没有异常日志 —— 这本身就是线索。"
            "注意：日志行末尾的 trace=xxx 表示「同一次请求」。"
            "可以把某个 trace_id 当作 keyword 再查一次，就能看到这次请求的完整链路。"
        ),
        args_model=QueryLogsArgs,
        func=_query,
    )


__all__ = ["MAX_RESPONSE_CHARS", "QueryLogsArgs", "build_query_logs_tool"]
