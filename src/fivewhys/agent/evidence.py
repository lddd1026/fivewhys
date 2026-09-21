"""证据来源校验 —— 结论里的每条证据都必须能对上一次**真实的工具调用**（需求 FR-8）。

## 要防的是什么

大模型最危险的失败模式不是「答错」，而是**编一个像样的理由来支持答案**：

    根因：连接池被调小
    证据：query_metrics(order-service) 显示错误率从 0% 涨到 13%

如果这次调查里**根本没调用过** `query_metrics`，那这条证据是凭空写出来的。
而它读起来完全合理 —— 人扫一眼不会怀疑。判分也照样给它满分，
因为 §6.1 只看根因对不对，不看结论是怎么得出来的。

这才是真正会害人的东西：**一个结论正确、过程编造的诊断，比一个明确说
「我查不出来」的诊断糟糕得多**。前者会让人相信一个没有被验证过的推理。

## 校验规则（只做**能判定**的那部分）

对每一条 ``evidence``（顶层和 ``why_chain`` 里的都算）：

1. 它的 ``source`` 里必须提到一个**这次真的调用过的工具名**；
2. 如果它提到了一个「存在但这次没调用」的工具名 → 直接判为编造。

**故意不做的检查**：不校验 source 里提到的**服务名/参数**是否与调用时一致。
因为那是不可判定的：一句「query_logs 显示 order-service 与 payment-service 都正常」
里出现的 payment-service，可能是对比说明，不是声称查过它。
—— **宁可漏判，也不要误判**：误判会把一个诚实的结论退回去重做，
而重做会烧 token、还可能把正确答案拖成 max_steps。

## 判定不出来的时候怎么办

`source` 一个工具名都没提（比如写成「日志」）→ 也**算不合格**，但反馈信息不一样：
列出这次**实际调用过**的工具，让模型照着改。这比「证据不合法」有用得多 ——
它知道该写什么。

## 与 §6.2 的关系

一条结论被反复拒绝、最终没提交出来 → 按 ``max_steps`` 记「错」，
计入准确率分母。这是公平的：**说不清证据来自哪里，就是方法上的失败。**
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from fivewhys.models import Diagnosis, ToolCallRecord


def evidence_sources(diagnosis: Diagnosis) -> list[str]:
    """把结论里**所有**证据的 source 都取出来。

    ⚠️ 顶层 ``evidence`` 和每个 ``why_chain`` 步骤里的 ``evidence`` 都要算 ——
    FR-8 说的是「结论里的每条证据」，只查顶层等于给了一个明显的后门：
    把编造的证据塞进某一层 why 里就绕过去了。
    """
    sources = [item.source for item in diagnosis.evidence]
    for step in diagnosis.why_chain:
        sources.extend(item.source for item in step.evidence)
    return sources


def check_evidence(
    diagnosis: Diagnosis,
    tool_calls: Sequence[ToolCallRecord],
    *,
    known_tools: Iterable[str] = (),
) -> list[str]:
    """检查每条证据的来源。返回问题列表，空列表表示通过。

    Args:
        diagnosis: 模型提交的结论。
        tool_calls: 这次调查里**真正执行过**的工具调用（含失败的 ——
            调用过但报错，仍然算「查过」，结论里引用它是诚实的）。
        known_tools: 系统里存在但**这次没调用**的工具名。用来区分
            「引用了没查过的工具」（编造）和「压根没提工具名」（写得含糊）。

    Returns:
        人话写的问题列表。调用方会把它们喂回给模型。
    """
    # `args` 里记的是模型给的原始参数，可能带服务名；这里只用工具名做判定
    executed = {record.tool for record in tool_calls}
    known = set(known_tools)
    sources = evidence_sources(diagnosis)

    if not sources:
        return ["结论里一条证据都没有 —— 没有证据的根因等于猜测，请补上你依据的工具返回"]

    problems: list[str] = []
    unknown_reference: list[str] = []

    for index, source in enumerate(sources, start=1):
        text = source.strip()
        if not text:
            problems.append(f"第 {index} 条证据的 source 是空的 —— 必须写明它来自哪次工具调用")
            continue

        # ① 提到了这次真调用过的工具 → 合格（措辞宽松无所谓）
        if any(name in text for name in executed):
            continue

        # ② 提到了一个存在但这次没调用的工具 → 编造，明确指出来
        faked = sorted(name for name in known - executed if name in text)
        if faked:
            unknown_reference.append(f"「{text}」引用了 {'、'.join(faked)}，但这次调查里没调用过它")
            continue

        # ③ 一个工具名都没提 → 含糊，告诉它可以引用哪些
        problems.append(
            f"第 {index} 条证据的来源「{text}」看不出是哪次工具调用 —— source 里要写出工具名"
        )

    if unknown_reference:
        problems.append(
            "以下证据声称来自某次调用，但那次调用**不存在**（这属于编造证据）：\n  - "
            + "\n  - ".join(unknown_reference)
        )

    if problems:
        did = "、".join(sorted(executed)) or "（这次一次工具都没调用）"
        problems.append(f"这次调查实际调用过的工具：{did} —— 证据只能引用它们")

    return problems


__all__ = ["check_evidence", "evidence_sources"]
