"""FIV-5 端到端验证：agent 到底能不能查出根因。

这是 M1 的最终验收：给 agent 一个真实的故障场景，让它自己调查，
然后**按需求 §6.1 的判分规则**对照标准答案评分。

用法::

    python scripts/demo_m1.py                 # 跑 5 次（M1 验收标准）
    python scripts/demo_m1.py --runs 3
    python scripts/demo_m1.py --trace         # 打印每一轮的推理轨迹
    python scripts/demo_m1.py --offline       # 不调 LLM，只看场景长什么样

前置：``.env`` 里配好 ``DEEPSEEK_API_KEY``

**这是唯一需要联网和花钱的一步。** 其余测试全部离线可跑。

## 用的就是评测集里的那个场景

场景由 :func:`fivewhys.scenario.build_scenario` 构造 —— 和
``fivewhys snapshot`` / M6 评测台用的是**同一个构造器、同一份数据形状**：
三类服务、日志 / 指标 / 配置 / 发布 / 拓扑五类数据齐备，5 个工具都有真实数据。

（FIV-13 之前这里只喂了日志，另外四个工具拿到的是空数据 ——
那样跑出来的「通过 3/5」证明不了 agent 会在真实排障路径上工作。）
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

# 让脚本能直接运行（不必先 pip install -e .）
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rich.console import Console  # noqa: E402
from rich.panel import Panel  # noqa: E402
from rich.table import Table  # noqa: E402

from fivewhys.agent import diagnose  # noqa: E402
from fivewhys.config import get_settings  # noqa: E402
from fivewhys.logs import configure_logging  # noqa: E402
from fivewhys.models import AgentRun, FaultCategory  # noqa: E402
from fivewhys.scenario import Scenario  # noqa: E402
from fivewhys.scenario import build_scenario as build_full_scenario  # noqa: E402
from fivewhys.scoring import PASS_THRESHOLD, score_diagnosis  # noqa: E402
from fivewhys.tools import DataSource, build_registry  # noqa: E402

console = Console()

T0 = datetime(2026, 1, 1, 14, 0, tzinfo=UTC)

API_KEY_BY_PROVIDER = {
    "deepseek": "DEEPSEEK_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}


# --------------------------------------------------------------------------
# 场景
# --------------------------------------------------------------------------


def make_scenario(seed: int) -> Scenario:
    """造一个「连接池耗尽」场景 —— 和评测集用的是同一个构造器。"""
    return build_full_scenario(FaultCategory.DB_POOL_EXHAUSTED, seed=seed, base_time=T0)


# --------------------------------------------------------------------------
# 判分
#
# 规则实现在 fivewhys.scoring 里（正式模块，有测试覆盖），
# 因为它是整个项目的核心资产 —— 判分错了，「准确率」这个数字就没有意义。
# M6 的评测台会直接复用它。
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# 打印
# --------------------------------------------------------------------------


def show_offline(scenario: Scenario) -> None:
    """不调 LLM，只展示场景本身 —— 对应需求 FR-14a 的离线可看性。

    ⚠️ 这里**故意把答案也打出来**。它不进 agent 的上下文（agent 只拿到
    ``scenario.question``），是给**你**看的：一眼就能确认这个场景该考什么、
    线索够不够、答案藏在哪。
    """
    truth = scenario.ground_truth
    registry = build_registry(DataSource.from_scenario(scenario))

    console.print("[bold]场景概览[/bold]（不调用任何 LLM）\n")
    console.print(f"  问题      : {scenario.question}")
    # ⚠️ 不要写 f"[{category}]" —— Rich 会把方括号当成标记（markup）吞掉，
    # 屏幕上只剩一个服务名，类别凭空消失。这个 bug 是跑 --offline 看输出时发现的。
    console.print(
        f"  标准答案  : [bold]{truth.fault_category.value}[/bold] @ {truth.root_cause_service}"
    )
    console.print(f"  根因描述  : {truth.root_cause}")
    console.print()
    console.print(f"  日志      : {len(scenario.logs)} 条  {scenario.logs.stats()}")
    console.print(f"  指标采样  : {len(scenario.metrics)} 条")
    console.print(f"  配置快照  : {len(scenario.configs)} 条")
    console.print(f"  发布记录  : {len(scenario.deploys)} 条")
    for name, calls in scenario.topology.items():
        console.print(f"  拓扑      : {name} -> {'、'.join(calls) if calls else '（无下游）'}")
    console.print(f"  可用工具  : {' / '.join(registry.names())}")
    console.print(f"  判分关键词: {', '.join(truth.match_keywords)}\n")

    # 只看异常日志 —— 故障窗口里的正常流量占绝大多数（一次请求 10 行跨服务日志），
    # 不过滤的话前 12 条全是 INFO，"症状"一条也看不见。
    fault_lines = [e for e in scenario.logs.all() if e.ts >= truth.injected_at]
    abnormal = [e for e in fault_lines if e.level.value in {"WARN", "ERROR"}]

    console.print("[bold]故障期间的异常日志（前 12 条）[/bold]")
    for entry in abnormal[:12]:
        style = {"ERROR": "red", "WARN": "yellow"}.get(entry.level.value, "dim")
        trace = f"  trace={entry.trace_id}" if entry.trace_id else ""
        console.print(
            f"  [{style}]{entry.ts:%H:%M:%S} {entry.level.value:<5}[/{style}]"
            f" {entry.message}{trace}",
            soft_wrap=True,
        )
    console.print(
        f"  [dim]（另有 {len(fault_lines) - len(abnormal)} 条正常请求日志："
        "同一次请求的日志散落在三个服务里，靠 trace_id 串起来）[/dim]"
    )

    text = " ".join(e.message.lower() for e in fault_lines)
    leaked = [kw for kw in truth.answer_keywords if kw.lower() in text]

    console.print()
    console.print(
        "  [green]✓ 答案词没有泄漏到日志里[/green]"
        if not leaked
        else f"  [red]✗ 答案词泄漏了：{leaked}[/red]"
    )
    console.print(
        "  [green]✓ 关键线索已出现[/green]（connection wait time 飙升）"
        if "connection wait time" in text
        else "  [red]✗ 关键线索缺失 —— agent 将无从推理[/red]"
    )

    # ---- 答案藏在哪：配置里 ----
    changes = scenario.configs.changes(truth.root_cause_service)
    if changes:
        lines = "\n".join(f"    {c.ts:%H:%M:%S}  {c.key}: {c.old} -> {c.new}" for c in changes)
        console.print()
        console.print(
            Panel(
                lines,
                title=f"答案在配置里（{truth.root_cause_service}），不在日志里",
                title_align="left",
            )
        )
    console.print()
    console.print(f"  [dim]答案词（不许出现在日志）: {', '.join(truth.answer_keywords)}[/dim]")
    console.print(f"  [dim]判分词（给 agent 的答案打分）: {', '.join(truth.match_keywords)}[/dim]")
    console.print("\n  [dim]想看 agent 具体会读到什么：python scripts/inspect_scenario.py[/dim]")


def show_trace(run: AgentRun) -> None:
    console.print("\n  [dim]推理轨迹：[/dim]")
    for record in run.tool_calls:
        mark = "[green]OK  [/green]" if record.ok else "[red]FAIL[/red]"
        detail = (record.result_summary or record.error or "").splitlines()[0][:70]
        console.print(f"    step{record.step} {mark} {record.tool:<18} {detail}")


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------


def explain_failure(run: AgentRun) -> str:
    """把「为什么没出结论」说成人话。

    ⚠️ 这段是上线前测试补的：坏 key 时以前只在表格备注里写「未提交结论（error）」，
    用户既不知道是 401 还是网络断了，也不知道该动哪里。
    现在按错误类型给一句**可执行的**提示。
    """
    detail = (run.error or "").strip().splitlines()[0] if run.error else ""
    lowered = detail.lower()

    if "401" in lowered or "authentication" in lowered or "api key" in lowered:
        hint = "API Key 不对或已失效 —— 检查 .env 里的 DEEPSEEK_API_KEY"
    elif "429" in lowered or "rate limit" in lowered:
        hint = "被限流了 —— 等一会儿再跑，或把 --runs 调小"
    elif "timeout" in lowered or "timed out" in lowered:
        hint = "请求超时 —— 检查网络，或调大 FIVEWHYS_TIMEOUT_S"
    elif "connect" in lowered or "network" in lowered or "dns" in lowered:
        hint = "连不上 provider —— 检查网络/代理，或确认 FIVEWHYS_API_BASE 是对的"
    elif "402" in lowered or "insufficient" in lowered or "balance" in lowered:
        hint = "账户余额不足 —— 去 provider 后台充值"
    else:
        hint = "加 -v 跑一次看完整报错"

    return f"{run.stop_reason}：{detail[:120]} —— {hint}" if detail else f"{run.stop_reason}"


async def run_one(seed: int, trace: bool) -> tuple[AgentRun, float, list[str]]:
    scenario = make_scenario(seed)
    run = await diagnose(
        scenario_id=scenario.scenario_id,
        question=scenario.question,
        registry=build_registry(DataSource.from_scenario(scenario)),
        settings=get_settings(),
    )

    # 注意参数名叫 trace 而不是 show_trace ——
    # 叫 show_trace 会把模块级的 show_trace() 函数遮蔽掉，
    # 变成 "TypeError: 'bool' object is not callable"。
    # 这个 bug 是端到端测试发现的：--offline 不走这里，单测也不跑脚本。
    if trace:
        show_trace(run)

    if run.diagnosis is None:
        return run, 0.0, [explain_failure(run)]

    result = score_diagnosis(run.diagnosis, scenario.ground_truth)
    return run, result.total, result.notes


async def main() -> int:
    parser = argparse.ArgumentParser(description="FIV-5 端到端验证")
    parser.add_argument("--runs", type=int, default=5, help="跑几次（默认 5）")
    parser.add_argument("--trace", action="store_true", help="打印工具调用轨迹")
    parser.add_argument("--offline", action="store_true", help="不调 LLM，只展示场景")
    parser.add_argument("-v", "--verbose", action="store_true", help="打印调试日志（含堆栈）")
    args = parser.parse_args()

    # 默认安静：一行错误信息，不甩 litellm 的堆栈和横幅（上线前测试发现的）
    configure_logging(verbose=args.verbose)

    settings = get_settings()

    if args.offline:
        show_offline(make_scenario(0))
        return 0

    provider = settings.llm_model.split("/", 1)[0]
    key_name = API_KEY_BY_PROVIDER.get(provider)
    if key_name and not os.environ.get(key_name):
        console.print(f"[red]缺少 {key_name}[/red]")
        console.print("把 key 填进 .env 再跑：")
        console.print("  Copy-Item .env.example .env")
        console.print(f"  # 然后编辑 .env 填入 {key_name}=sk-...")
        return 1

    console.print(f"[bold]FIV-5 端到端验证[/bold]  模型={settings.llm_model}  次数={args.runs}\n")

    table = Table(header_style="bold")
    table.add_column("轮次", justify="right")
    table.add_column("得分", justify="right")
    table.add_column("停止原因")
    table.add_column("调用", justify="right")
    table.add_column("成本", justify="right")
    table.add_column("耗时", justify="right")
    table.add_column("备注", style="dim")

    passed = 0
    total_cost = 0.0

    for index in range(args.runs):
        run, points, notes = await run_one(index, args.trace)
        ok = points >= PASS_THRESHOLD
        passed += int(ok)
        total_cost += run.total_cost_usd

        table.add_row(
            str(index + 1),
            f"[green]{points:.0%}[/green]" if ok else f"[red]{points:.0%}[/red]",
            run.stop_reason,
            str(len(run.tool_calls)),
            f"${run.total_cost_usd:.4f}",
            f"{run.duration_s:.1f}s" if run.duration_s else "-",
            "；".join(notes) if notes else "完全正确",
        )

    console.print(table)
    console.print()

    rate = passed / args.runs
    verdict = rate >= 0.6  # M1 验收：跑 5 次至少 3 次对
    console.print(
        f"  通过 [bold]{passed}/{args.runs}[/bold]  "
        f"（判定线 {PASS_THRESHOLD:.0%}）  总成本 ${total_cost:.4f}"
    )
    console.print(
        "[green]M1 验收通过：agent 能自己查出根因。[/green]"
        if verdict
        else "[red]M1 验收未通过：还需要调优。[/red]"
    )
    return 0 if verdict else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
