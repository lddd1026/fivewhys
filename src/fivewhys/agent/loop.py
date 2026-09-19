"""agent 主循环。

==============================================================================
TODO(M1-4)  实现这个循环
==============================================================================

别怕，核心只有 20 行。README 里讲过：

    messages = [{"role": "user", "content": question}]
    for step in range(max_steps):
        reply = llm(messages, tools=specs)
        if not reply.tool_calls:
            return reply.content
        messages.append(reply)
        for call in reply.tool_calls:
            result = tools[call.name](**call.args)
            messages.append({"role": "tool", "content": str(result)})

落到这个项目里，要做的是：

  1. 用 `litellm.acompletion(...)` 发请求
     - model 用 `settings.llm_model`
     - messages 第一项是 system prompt（用 `build_system_prompt` 渲染）
     - tools 用 `registry.specs()`
     - **必须传 `response_format` 或 tools 让模型走 function calling**
  2. 解析 `response.choices[0].message.tool_calls`
     - litellm 会把它统一成 OpenAI 格式
     - `call.function.name` 是工具名，`call.function.arguments` 是 JSON **字符串**，
       记得 `json.loads`，并处理解析失败的情况（这是真实痛点，见 M7）
  3. 执行工具：`registry.get(name)(**args)`
     - **每个工具调用都要包 try/except**。工具抛异常不能让整个诊断崩掉，
       要把错误信息作为工具结果喂回模型，让它自己纠错
     - 记一条 `ToolCallRecord`（step / tool / args / ok / latency_ms）
  4. 把结果以 `{"role": "tool", "tool_call_id": ..., "content": ...}` 追加回 messages
  5. 终止条件（**按优先级**）：
     - 模型调用了 `submit_diagnosis` → stop_reason="submitted"
     - 达到 `settings.max_steps`         → stop_reason="max_steps"
     - 累计成本超过 `settings.max_cost_usd` → stop_reason="max_cost"
  6. 累计 token 和成本：litellm 的响应里通常有 `response.usage`，
     成本可以用 `litellm.completion_cost(response)` 估算

实现完之后，你应该能跑出第一个真实结果（哪怕很糙）：
  M1 的验收标准 = 跑 5 次，至少 3 次给出正确根因。
"""

from __future__ import annotations

from fivewhys.models import AgentRun
from fivewhys.tools import ToolRegistry


async def diagnose(
    *,
    scenario_id: str,
    question: str,
    registry: ToolRegistry,
) -> AgentRun:
    """对一个问题做多步根因诊断。

    Args:
        scenario_id: 场景标识，用于和 GroundTruth 对应
        question:    现象描述，例如「order-service 从 14:30 开始错误率飙升」
        registry:    可用工具

    Returns:
        完整的 AgentRun 轨迹，`diagnosis` 字段是结构化结论。
    """
    # TODO(M1-4)：实现。参考上面的六步说明。
    raise NotImplementedError("TODO(M1-4)：实现 agent 主循环")


__all__ = ["diagnose"]
