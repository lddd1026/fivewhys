"""agent 主循环 —— 整个项目的核心。

## 核心只有 20 行

::

    messages = [{"role": "user", "content": question}]
    for step in range(max_steps):
        reply = llm(messages, tools)
        if not reply.tool_calls:
            return reply.content
        messages.append(reply)
        for call in reply.tool_calls:
            result = tools[call.name](**call.args)
            messages.append({"role": "tool", "content": str(result)})

**剩下的全是「让它在真实世界里可靠地工作」**，这也正是本项目要展示的东西：

- 模型返回的 JSON 可能非法 → 解析失败要把**具体错误**喂回去，让它自己纠正
- 工具可能抛异常 → 不能让整个诊断崩掉
- 模型可能只顾说话不调工具 → 要推它一把，不能当作结束
- 成本可能失控 → 要有熔断
- 必须留下完整轨迹 → 否则 M6/M7 无从分析失败、无从算指标

## 停止条件（FR-7）

================  ==========================================================
stop_reason       触发
================  ==========================================================
submitted         提交了结论，且**没用满**追问预算（提前收敛）
max_why_depth     提交了结论，但**用满了**追问预算（被预算停住，不是自然到底）
max_steps         达到步数上限（兜底）
max_cost          达到成本上限（兜底）
max_tokens        达到 token 上限（累计用量或单次请求上下文，兜底）
error             运行异常，见 ``AgentRun.error``
================  ==========================================================

**关于 ``max_why_depth`` 的准确含义**（值得说清楚，因为它容易被误解）：

5 Whys 的深度是**提示词**层面的约束 —— 用 tool calling 时，模型在提交结论前
不会「暴露」自己追问到第几层，所以循环**没有办法**在提交之前拦住它。
能做、也确实做了的是：**提交时看它的链条有多长**，据此区分两种停止：

- 链长 < 上限 → ``submitted``：它认为已经追到底了（提前收敛）
- 链长 ≥ 上限 → ``max_why_depth``：它是被追问预算停住的

这个区分对读报告的人有意义：「每次都顶到 5 层」和「平均 2.7 层就收敛」
是两种很不一样的 agent。而**判分对两者完全一样**（需求 §6.2：都按 §6.1 判），
所以这个标记不会影响准确率，它只影响你怎么解释那些数字。

追问**浅**了不做校验：那是准确率问题（M6 会算出来），不是正确性问题 ——
为它把一次已经正确的诊断判死得不偿失。实际层数从 ``run.diagnosis.why_chain``
直接数，M6 的指标里有「平均追问层数」和「用满预算的比例」两项。

> 修正说明（FIV-18）：这段原先只写了「循环不校验长度」，而 FR-7 把
> ``max_why_depth`` 列为**主要停止条件**。现在两者一致了 —— 它是一个
> **提交时判定**的标签，不是一个提交前的闸门。
> 现在改成了事实：不校验，但信息本来就在 ``diagnosis`` 里。
"""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from fivewhys.agent.evidence import check_evidence
from fivewhys.agent.llm import (
    LiteLLMClient,
    LLMClient,
    LLMResponse,
    Message,
    ToolCall,
    estimate_request_tokens,
)
from fivewhys.agent.prompts import build_system_prompt
from fivewhys.config import Settings, get_settings
from fivewhys.models import AgentRun, Diagnosis, ToolCallRecord
from fivewhys.tools import ToolRegistry
from fivewhys.trace import TraceWriter, new_run_id

logger = logging.getLogger(__name__)

SUBMIT_TOOL_NAME = "submit_diagnosis"

# 模型只顾说话时推它一把，而不是把这次诊断判死
_NUDGE = (
    "请继续调查。如果证据已经足够，必须调用 "
    f"{SUBMIT_TOOL_NAME} 提交结构化结论 —— 这是唯一能结束调查的方式。"
)


# --------------------------------------------------------------------------
# submit_diagnosis 的工具定义
# --------------------------------------------------------------------------


def _inline_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """把 JSON Schema 里的 ``$ref`` 就地展开。

    为什么要这样：``Diagnosis`` 是嵌套模型（``WhyStep`` / ``Evidence``），
    Pydantic 生成的 schema 会带 ``$defs`` + ``$ref``。部分 provider 的
    function calling 对 ``$ref`` 支持不好，会导致参数缺失或被直接拒绝。
    就地展开最稳。
    """
    defs: dict[str, Any] = schema.get("$defs", {})

    def resolve(node: Any) -> Any:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                target = resolve(defs.get(ref.rsplit("/", 1)[-1], {}))
                extra = {key: value for key, value in node.items() if key != "$ref"}
                if extra and isinstance(target, dict):
                    return {**target, **extra}
                return target
            return {key: resolve(value) for key, value in node.items()}
        if isinstance(node, list):
            return [resolve(item) for item in node]
        return node

    resolved = resolve({key: value for key, value in schema.items() if key != "$defs"})
    if not isinstance(resolved, dict):  # pragma: no cover —— Diagnosis 的 schema 一定是对象
        raise TypeError("Diagnosis 的 JSON Schema 必须是对象")
    return resolved


def submit_tool_spec() -> dict[str, Any]:
    """``submit_diagnosis`` 的工具定义。

    它是个**虚拟工具**：不执行任何副作用，被调用即代表调查结束。
    之所以做成工具而不是「让模型输出 JSON」，是因为这样和普通工具走同一条路径，
    模型更容易理解，终止条件也更清晰。
    """
    return {
        "type": "function",
        "function": {
            "name": SUBMIT_TOOL_NAME,
            "description": (
                "提交最终的根因诊断结论。"
                "这是唯一能结束调查的方式 —— 只在证据足够时才调用。"
                "为什么是根因（而不是症状）：那个一旦修复、症状就消失的东西。"
            ),
            "parameters": _inline_refs(Diagnosis.model_json_schema()),
        },
    }


# --------------------------------------------------------------------------
# 消息构造
# --------------------------------------------------------------------------


def _assistant_message(response: LLMResponse) -> Message:
    return {
        "role": "assistant",
        "content": response.content or "",
        "tool_calls": [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": call.arguments},
            }
            for call in response.tool_calls
        ],
    }


def _tool_message(call_id: str, content: str) -> Message:
    return {"role": "tool", "tool_call_id": call_id, "content": content}


# --------------------------------------------------------------------------
# 单次工具调用
# --------------------------------------------------------------------------


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _execute_tool(registry: ToolRegistry, call: ToolCall, step: int) -> ToolCallRecord:
    """执行一次工具调用，**任何失败都变成记录，不抛异常**。

    工具报错不能让整个诊断崩掉 —— 要把错误信息喂回模型，让它自己纠错。
    这是 agent 和普通程序最不一样的地方：**错误是一种输入，不是终点**。
    """
    started = time.perf_counter()

    # 第一道关：模型给的参数是不是合法 JSON 对象
    try:
        raw = json.loads(call.arguments) if call.arguments.strip() else {}
        if not isinstance(raw, dict):
            raise ValueError(f"参数必须是 JSON 对象，收到 {type(raw).__name__}")
    except (json.JSONDecodeError, ValueError) as exc:
        return ToolCallRecord(
            step=step,
            tool=call.name,
            args={},
            ok=False,
            error=f"参数不是合法的 JSON 对象：{exc}",
            latency_ms=_elapsed_ms(started),
        )

    # 第二道关：工具名是否存在（模型可能编造工具名）
    try:
        tool = registry.get(call.name)
    except KeyError as exc:
        return ToolCallRecord(
            step=step,
            tool=call.name,
            args=raw,
            ok=False,
            error=str(exc).strip("'\""),
            latency_ms=_elapsed_ms(started),
        )

    # 第三道关：参数校验 + 真正执行
    try:
        result = tool(**raw)
    except Exception as exc:  # noqa: BLE001 —— 工具的任何异常都转成给模型的反馈
        logger.warning("工具 %s 执行失败", call.name, exc_info=True)
        return ToolCallRecord(
            step=step,
            tool=call.name,
            args=raw,
            ok=False,
            error=f"工具执行失败：{type(exc).__name__}: {exc}",
            latency_ms=_elapsed_ms(started),
        )

    return ToolCallRecord(
        step=step,
        tool=call.name,
        args=raw,
        ok=True,
        result_summary=str(result),
        latency_ms=_elapsed_ms(started),
    )


def _parse_diagnosis(arguments: str) -> tuple[Diagnosis | None, str]:
    """把模型的提交解析成 ``Diagnosis``。失败时返回具体错误，供喂回模型。

    只捕 ``ValidationError`` 就够了：Pydantic v2 的 ``model_validate_json``
    对「完全不是 JSON」「JSON 语法错」「字段缺失或类型错」**一律**抛它
    （而 ``ValidationError`` 本身就是 ``ValueError`` 的子类）。

    原先还写了一个 ``except ValueError``，实测永远走不到 —— 是死代码，已删。
    证据：三种畸形输入都落进 ``ValidationError``。
    """
    try:
        return Diagnosis.model_validate_json(arguments), ""
    except ValidationError as exc:
        return None, f"结论不符合要求的字段结构：\n{exc}"


# --------------------------------------------------------------------------
# 主循环
# --------------------------------------------------------------------------


async def diagnose(
    *,
    scenario_id: str,
    question: str,
    registry: ToolRegistry,
    llm: LLMClient | None = None,
    settings: Settings | None = None,
    trace: TraceWriter | None = None,
) -> AgentRun:
    """对一个问题做多步根因诊断。

    Args:
        scenario_id: 场景标识，用于和 ``GroundTruth`` 对应
        question: 现象描述，例如「order-service 从 14:30 前后开始错误率飙升」
        registry: 可用工具
        llm: LLM 客户端。**默认走 litellm；测试时注入假客户端即可脱离网络**
        settings: 运行配置，默认读环境变量
        trace: 轨迹写入器。默认按 settings 自动建一个 —— 需求 FR-9 说的是
            每次诊断**必须**落盘完整轨迹；关掉只有一个理由：测试不想往仓库写数据

    Returns:
        完整的 ``AgentRun``：轨迹 + 结构化结论 + 成本/耗时/停止原因。
    """
    settings = settings or get_settings()
    client = llm or LiteLLMClient(
        model=settings.llm_model,
        temperature=settings.temperature,
        api_base=settings.api_base,
        timeout_s=settings.llm_timeout_s,
        max_output_tokens=settings.max_output_tokens,
    )

    writer = trace
    if writer is None and settings.trace_enabled:
        writer = TraceWriter(new_run_id(scenario_id), root=settings.trace_root)

    run = AgentRun(
        scenario_id=scenario_id,
        model=client.model,
        run_id=writer.run_id if writer else "",
        trace_path=str(writer.path) if writer else None,
    )
    tools = [*registry.specs(), submit_tool_spec()]
    messages: list[Message] = [
        {
            "role": "system",
            "content": build_system_prompt(
                tool_specs=tools,
                max_depth=settings.max_why_depth,
            ),
        },
        {"role": "user", "content": question},
    ]

    if writer is not None:
        writer.start(
            scenario_id=scenario_id,
            question=question,
            model=client.model,
            tool_names=registry.names(),
            settings=settings.model_dump(mode="json"),
        )

    stop_reason = "max_steps"

    try:
        for step in range(1, settings.max_steps + 1):
            run.steps = step

            # ---- 上下文守卫：**发出去之前**先估一下（token 用量控制）----
            # 每步都把工具返回追加进历史，prompt 会自己长大。等到 provider 报
            # 「上下文超限」时，这一轮的钱已经花了 —— 提前停既省钱也能说清原因。
            estimated = estimate_request_tokens(messages, tools)
            if estimated > settings.max_context_tokens:
                stop_reason = "max_tokens"
                run.stop_note = (
                    f"第 {step} 步的请求预估 {estimated:,} token，超过上下文上限 "
                    f"{settings.max_context_tokens:,} —— 提前停止（历史被工具返回撑满了）"
                )
                logger.warning("诊断停止：%s", run.stop_note)
                break

            if writer is not None:
                writer.request(step=step, messages=list(messages))

            call_started = time.perf_counter()
            response = await client.complete(messages, tools)
            call_latency = _elapsed_ms(call_started)

            run.total_cost_usd += response.cost_usd
            run.total_tokens += response.total_tokens

            if writer is not None:
                writer.response(
                    step=step,
                    content=response.content,
                    tool_calls=[
                        {"id": call.id, "name": call.name, "arguments": call.arguments}
                        for call in response.tool_calls
                    ],
                    prompt_tokens=response.prompt_tokens,
                    completion_tokens=response.completion_tokens,
                    cost_usd=response.cost_usd,
                    latency_ms=call_latency,
                )

            # 模型只在说话，没调工具 —— 不当作结束，推它一把
            if not response.tool_calls:
                messages.append({"role": "assistant", "content": response.content or ""})
                messages.append({"role": "user", "content": _NUDGE})
            else:
                messages.append(_assistant_message(response))

                for call in response.tool_calls:
                    if call.name == SUBMIT_TOOL_NAME:
                        diagnosis, error = _parse_diagnosis(call.arguments)
                        if diagnosis is not None and settings.verify_evidence:
                            # ---- 证据来源校验（FR-8）----
                            # 结构合法不代表内容诚实：模型可以编一条「看起来很像」
                            # 的证据来支持一个它猜出来的根因。这里查的是
                            # 「这条证据声称来自的那次调用，真的发生过吗」。
                            problems = check_evidence(
                                diagnosis,
                                run.tool_calls,
                                known_tools=registry.names(),
                            )
                            if problems:
                                error = "结论里的证据无法核对：\n" + "\n".join(
                                    f"  - {problem}" for problem in problems
                                )
                                diagnosis = None

                        if diagnosis is not None:
                            run.diagnosis = diagnosis
                            # ---- 用的是哪种「停」？（FR-7）----
                            # 深度是提示词约束，循环在提交前观测不到；提交时能看到链长。
                            # 链长顶到上限 → 它是被追问预算停住的，记 max_why_depth；
                            # 没顶到 → 它自己认为追到底了，记 submitted。
                            # 需求 §6.2：两者判分完全一样，这个标签只影响怎么解释数字。
                            depth = len(diagnosis.why_chain)
                            stop_reason = (
                                "max_why_depth" if depth >= settings.max_why_depth else "submitted"
                            )
                            messages.append(_tool_message(call.id, "结论已接收，调查结束。"))
                            if writer is not None:
                                # 提交也要记结果：否则轨迹里「提交被拒」和
                                # 「提交成功」长得一模一样 —— 而它们是两种完全不同的失败模式
                                writer.tool_result(
                                    step=step,
                                    tool=SUBMIT_TOOL_NAME,
                                    args={},
                                    ok=True,
                                    result=f"结论已接收（{stop_reason}，追问 {depth} 层）",
                                )
                            break
                        # 解析失败：把校验错误喂回去，让它自己纠正
                        run.tool_calls.append(
                            ToolCallRecord(
                                step=step,
                                tool=call.name,
                                args={},
                                ok=False,
                                error=error,
                            )
                        )
                        if writer is not None:
                            writer.tool_result(
                                step=step,
                                tool=SUBMIT_TOOL_NAME,
                                args={},
                                ok=False,
                                result="",
                                error=error,
                            )
                        messages.append(
                            _tool_message(call.id, f"结论格式不合法，请修正后重新提交：{error}")
                        )
                        continue

                    record = _execute_tool(registry, call, step)
                    run.tool_calls.append(record)
                    messages.append(
                        _tool_message(call.id, record.result_summary or record.error or "")
                    )
                    if writer is not None:
                        writer.tool_result(
                            step=step,
                            tool=record.tool,
                            args=record.args,
                            ok=record.ok,
                            result=record.result_summary,
                            error=record.error,
                            latency_ms=record.latency_ms,
                        )

            if run.diagnosis is not None:
                break

            # ---- token 用量控制：累计用量（provider 上报的硬数字）----
            # 放在诊断检查**之后**：这一步已经交出合法结论的话，不该因为
            # 「刚好多花了几个 token」而丢掉它。
            if run.total_tokens > settings.max_total_tokens:
                stop_reason = "max_tokens"
                run.stop_note = (
                    f"累计 {run.total_tokens:,} token，超过单次诊断上限 "
                    f"{settings.max_total_tokens:,}（第 {step} 步后停止）"
                )
                logger.warning("诊断停止：%s", run.stop_note)
                break

            if run.total_cost_usd > settings.max_cost_usd:
                stop_reason = "max_cost"
                run.stop_note = (
                    f"累计 ${run.total_cost_usd:.4f}，超过单次诊断上限 "
                    f"${settings.max_cost_usd:.4f}（第 {step} 步后停止）"
                )
                logger.warning("诊断停止：%s", run.stop_note)
                break

    except Exception as exc:  # noqa: BLE001 —— 崩溃要记录成结果，而不是抛给调用方
        # ⚠️ 日志分两级，因为**调用方看到的东西不一样**：
        #
        # 默认只打一行（WARNING，无堆栈）—— 用户看到的是
        # 「调用模型失败：AuthenticationError: ... 401」，而不是 60 行 litellm 内部堆栈。
        # 上线前实测：一个坏 key 会在结果表格之前甩出 60 行 traceback，
        # 而表格里只写着「未提交结论（error）」—— 既吓人又没说清原因。
        #
        # 完整堆栈仍然保留，但要开 DEBUG 才看（`-v` / FIVEWHYS 日志级别）。
        logger.warning("诊断中断：%s: %s", type(exc).__name__, exc)
        logger.debug("诊断中断的完整堆栈", exc_info=True)
        run.stop_reason = "error"
        run.error = f"{type(exc).__name__}: {exc}"
        run.finished_at = datetime.now(UTC)
        # 异常路径也要收尾：否则复盘时分不清「跑完了」和「进程被杀」
        if writer is not None:
            writer.finish(run)
        return run

    run.stop_reason = stop_reason
    run.finished_at = datetime.now(UTC)
    if writer is not None:
        writer.finish(run)
    return run


__all__ = ["SUBMIT_TOOL_NAME", "diagnose", "submit_tool_spec"]
