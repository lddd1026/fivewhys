"""五个工具共用的几件小事：预算、拼装、空结果。

## 为什么要抽出来

工具返回值会**直接进模型的上下文**，所以「一次能返回多少字」是一条**全局预算**（NFR-11），
不是一个工具自己的事。五个工具各写一份截断逻辑，迟早出现
「query_logs 守 6000 字符、query_metrics 无上限」这种不一致 ——
而不一致的预算就不是预算。

同理，五个工具的返回格式也必须长得一样。模型是在**读**这些文本，
格式统一它才学得会：「第一行是结论，第二行是元信息，然后才是明细」。

## 统一的返回格式

::

    共命中 12 条，显示 12 条          <- 结论：查到多少 / 给你看多少
    （受输出预算限制已截断……）        <- 只在被截断时出现
    服务=order-service  时间=14:02~14:07   <- 元信息：这次查了什么
    14:02:01 ERROR deadline exceeded     <- 明细
    …

空结果也必须按这个格式返回，并且**必须给出下一步线索** ——
「无数据」三个字会让模型以为工具坏了，然后放弃这条最有价值的路径。
"""

from __future__ import annotations

from collections.abc import Iterable

# NFR-11：单次工具调用返回 ≤ 2000 token。
#
# 为什么按字符数而不是 token 数：``tiktoken`` 首次使用需要联网下载编码表，
# 而 FR-14a 要求「无需 API Key、离线也能跑通自检」。为了一个预算估算引入
# 联网依赖不划算。按英文约 3 字符/token 保守换算：6000 字符 ≈ 2000 token。
# 等 M7 要精确核算成本时再评估是否引入真正的 tokenizer。
MAX_RESPONSE_CHARS = 6000

TRUNCATED_NOTE = "（受输出预算限制，结果已截断 —— 请缩小时间窗口或加过滤条件）"


def fit_lines(
    lines: Iterable[str],
    *,
    limit: int | None = None,
    budget: int = MAX_RESPONSE_CHARS,
) -> tuple[list[str], bool]:
    """按「条数上限」和「字符预算」双重裁剪。

    两个上限都要有：``limit`` 防的是「模型一次要一万条」，``budget`` 防的是
    「每条都很长，一百条就把上下文撑爆」。只防其中一个都会漏。

    ⚠️ 它只管**明细行**。整段返回的预算由 :func:`render_block` 负责 ——
    上线前审查踩过这个坑：只在明细行上卡预算、表头不计入，
    最宽的参数下实测 6108 字符，超了 6000 的线。

    Returns:
        ``(保留的行, 是否被截断)``
    """
    kept: list[str] = []
    used = 0
    for index, line in enumerate(lines):
        if limit is not None and index >= limit:
            return kept, True
        cost = len(line) + 1  # +1 是换行符
        if used + cost > budget:
            return kept, True
        kept.append(line)
        used += cost
    return kept, False


def render_block(
    summary: str,
    *,
    note: str | None = None,
    truncated_note: str = TRUNCATED_NOTE,
    meta: str | None = None,
    rows: Iterable[str] = (),
    limit: int | None = None,
    budget: int = MAX_RESPONSE_CHARS,
) -> str:
    """拼成一次工具调用的**完整返回**，并保证它不超过上下文预算。

    统一的格式（详见模块 docstring）::

        共命中 12 条，显示 12 条               <- summary
        这本身就是线索：……                     <- note（总是显示，例如空结果）
        （受输出预算限制，结果已截断）           <- truncated_note（只在被截断时）
        服务=order-service  时间=14:02~14:07   <- meta
        14:02:01 ERROR deadline exceeded       <- rows
        …

    **预算管的是整段返回** —— NFR-11 说的是「单次工具调用返回 ≤ 2000 token」，
    所以表头和截断提示都要先扣掉，剩下的才是明细行的额度。

    （上线前审查实测：只在明细行上卡预算、表头不计入，最宽的参数下会到
    6108 字符，超了 6000 的线。当时的测试里还带了个 +200 的容差，
    正好把这个越界掩盖住。）
    """
    header = [summary]
    header.extend(part for part in (note, meta) if part)
    header_len = sum(len(part) + 1 for part in header)
    # 截断提示自己也要占额度 —— 先预留，免得到时候又超出去
    reserved = len(truncated_note) + 1

    kept, truncated = fit_lines(
        rows,
        limit=limit,
        budget=max(0, budget - header_len - reserved),
    )

    parts = [summary]
    if note:
        parts.append(note)
    if truncated:
        parts.append(truncated_note)
    if meta:
        parts.append(meta)
    parts.extend(kept)
    return "\n".join(parts)


def blank_result(
    summary: str,
    *,
    clue: str,
    meta: str | None = None,
) -> str:
    """空结果的统一写法：说清楚「查了什么」，再给出**下一步**。

    Args:
        summary: 例如「共命中 0 条」。
        clue: 这说明了什么、接下来可以试什么。**不能省。**
        meta: 这次查询的参数。
    """
    return render_block(summary, note=f"这本身就是线索：{clue}", meta=meta)


def unknown_service(service: str, known: Iterable[str], *, meta: str | None = None) -> str:
    """服务名不存在时的统一回答。

    ## 为什么值得单独一个函数

    模型写错服务名（``payments-service`` / ``Order-Service``）是**必然会发生**的事。
    如果工具对错名字只回一句「这段时间没有日志」，模型会当成
    「这个服务是健康的」—— **一条错误的服务名，被理解成一条排除性证据**，
    然后一路推到一个错误的结论上。

    所以每个工具都必须在**自己查不到东西之前**先判断服务名是否存在，
    并把合法名字列出来。这类「静默地把错误当成结论」的坑，
    正是这个项目反复在防的东西（对比 FR-2 的答案泄漏、指标与日志不同源）。
    """
    names = "、".join(sorted(known)) if known else "（这个场景没有服务清单）"
    return blank_result(
        f"共 0 条：本场景里没有服务「{service}」",
        clue=f"服务名可能写错了 —— 名字对不上时「查不到」不代表「没问题」。可用的服务是：{names}",
        meta=meta,
    )


__all__ = [
    "MAX_RESPONSE_CHARS",
    "TRUNCATED_NOTE",
    "blank_result",
    "fit_lines",
    "render_block",
    "unknown_service",
]
