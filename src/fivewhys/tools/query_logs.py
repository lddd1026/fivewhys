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
from typing import Literal

from pydantic import BaseModel, Field

from fivewhys.mock.logstore import LogStore
from fivewhys.tools import Tool
from fivewhys.tools._render import (
    MAX_RESPONSE_CHARS,
    blank_result,
    render_block,
    unknown_service,
)

# 从 _render 转出来，保持 `from fivewhys.tools.query_logs import MAX_RESPONSE_CHARS` 可用。
# 预算现在由五个工具共用一份实现（见 _render 的说明），但这个名字在 FIV-2 就公开了。
__all__ = ["MAX_RESPONSE_CHARS", "QueryLogsArgs", "build_query_logs_tool"]


# 允许的日志级别。用 Literal 而不是 str，是为了让**模型看到可选值**，
# 并且**写错时报错而不是静默返回空**：
#   levels=["warning"] 以前会被规整成 WARNING，和 WARN 对不上 → 过滤出 0 条
#   → 工具回一句「这个服务没有异常」。一次拼写错误就变成了「服务是健康的」。
LogLevelName = Literal["DEBUG", "INFO", "WARN", "ERROR"]

# 默认只看 WARN/ERROR —— 别让 INFO 把上下文淹了。
DEFAULT_LEVELS: list[LogLevelName] = ["WARN", "ERROR"]


class QueryLogsArgs(BaseModel):
    """query_logs 的参数。字段 description 会直接给模型看，措辞很重要。"""

    service: str = Field(description="服务名，例如 order-service")
    start: datetime = Field(description="时间窗口起点（ISO 8601）")
    end: datetime = Field(description="时间窗口终点（ISO 8601）")
    levels: list[LogLevelName] = Field(
        default_factory=lambda: list(DEFAULT_LEVELS),
        description=(
            "要看的日志级别，可选值：DEBUG / INFO / WARN / ERROR。"
            "默认只看 WARN 和 ERROR，避免被 INFO 淹没"
        ),
    )
    keyword: str | None = Field(
        default=None,
        description=(
            "可选的关键字过滤（不区分大小写的子串匹配），也可以直接传一个 trace_id。"
            "常见用法：传 trace_id 查同一次请求的完整链路，"
            "或传某个错误里的词缩小范围"
        ),
    )
    limit: int = Field(
        default=50,
        le=200,
        description="最多返回多少条（1~200）。返回内容同时受 6000 字符预算限制",
    )


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

        meta = f"服务={service}  时间={window}  级别={level_text}"
        if keyword:
            meta += f"  关键字={keyword}"

        # 摘要只说命中多少条；被截断时由 truncated_note 说明「这是部分结果」。
        # 不再写「显示 M 条」—— M 是 render_block 内部裁剪的结果，
        # 在调用方算一遍就成了两处记账（正是 FIV-D 类 bug 的温床）。
        return render_block(
            f"共命中 {total} 条",
            truncated_note=(
                f"（结果被截断：limit={limit}，单次返回预算 {MAX_RESPONSE_CHARS} 字符。"
                "如需更多请缩小时间窗口或加关键字）"
            ),
            meta=meta,
            rows=(
                f"{entry.ts:%H:%M:%S} {entry.level.value:<5} {entry.message}"
                + (f"  trace={entry.trace_id}" if entry.trace_id else "")
                for entry in matched
            ),
            limit=limit,
        )

    return Tool(
        name="query_logs",
        description=(
            "查询某个服务在指定时间窗口内的日志，默认只返回 WARN/ERROR。\n"
            "**排障的第二步**：拿到异常时间点之后，用它看具体报错长什么样、"
            "从哪一行开始。\n"
            "日志行末尾的 trace=xxx 表示「同一次请求」（跨所有服务共享）—— "
            "把某个 trace_id 当 keyword 再查一次，就能看到这次请求的完整链路，"
            "比盲目放大时间窗口有效得多。\n"
            "局限：日志只给**症状**，通常不写原因 —— "
            "「connection wait time 飙升」不等于「连接池满了」，"
            "根因往往要去 get_config 或下游服务找。\n"
            '典型调用：query_logs(service="order-service", '
            'start="2026-01-01T14:02:00Z", end="2026-01-01T14:07:00Z")'
        ),
        args_model=QueryLogsArgs,
        func=_query,
    )


__all__ = ["MAX_RESPONSE_CHARS", "QueryLogsArgs", "build_query_logs_tool"]
