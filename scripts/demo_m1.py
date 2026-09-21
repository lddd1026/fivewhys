"""FIV-5 端到端验证：agent 到底能不能查出根因。

这是 M1 的最终验收：给 agent 一个真实的故障场景，让它自己调查，
然后**按需求 §6.1 的判分规则**对照标准答案评分。

用法::

    python scripts/demo_m1.py                 # 跑 5 次（M1 验收标准）
    python scripts/demo_m1.py --runs 3
    python scripts/demo_m1.py --trace         # 打印每一轮的推理轨迹
    python scripts/demo_m1.py --offline       # 不调 LLM，只看场景长什么样
    python scripts/demo_m1.py --model gpt-4o-mini   # 换模型，不改 .env（FIV-16）

前置：``.env`` 里配好对应的 API Key（``fivewhys doctor`` 会告诉你该设哪个变量）

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
from fivewhys.config import Settings, get_settings  # noqa: E402
from fivewhys.logs import configure_logging  # noqa: E402
from fivewhys.models import AgentRun, FaultCategory  # noqa: E402
from fivewhys.providers import api_key_env_for, describe_model  # noqa: E402
from fivewhys.scenario import Scenario  # noqa: E402
from fivewhys.scenario import build_scenario as build_full_scenario  # noqa: E402
from fivewhys.scoring import PASS_THRESHOLD, score_diagnosis  # noqa: E402
from fivewhys.tools import DataSource, build_registry  # noqa: E402

console = Console()

T0 = datetime(2026, 1, 1, 14, 0, tzinfo=UTC)


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
        hint = "API Key 不对或已失效 —— 检查 .env 里的 key（变量名看 `fivewhys doctor`）"
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


async def run_one(seed: int, trace: bool, settings: Settings) -> tuple[AgentRun, float, list[str]]:
    scenario = make_scenario(seed)
    run = await diagnose(
        scenario_id=scenario.scenario_id,
        question=scenario.question,
        registry=build_registry(DataSource.from_scenario(scenario)),
        settings=settings,
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


def judge(passed: int, attempted: int, planned: int) -> tuple[bool, str]:
    """验收判定 —— 抽成纯函数，因为它决定「这个数字能不能写进 README」。

    Args:
        passed: 得分 ≥ 判定线的轮次。
        attempted: 实际跑完的轮次。
        planned: 原计划跑几轮。

    Returns:
        ``(是否通过, 一句话说明)``

    两条规矩：

    1. 通过线是「至少 60%」—— 对应 M1 的「跑 5 次至少 3 次对」。
    2. **没跑满就不算通过**。预算中止、中途出错都算没跑满 ——
       拿 2 次的数据说「5 次验收通过」就是编数字。
    """
    if attempted == 0:
        return False, "一次都没跑 —— 预算太低或参数有误。"
    if attempted < planned:
        return False, f"未跑满全部轮次（{attempted}/{planned}），不构成验收结论。"
    rate = passed / attempted
    if rate >= 0.6:
        return True, "M1 验收通过：agent 能自己查出根因。"
    return False, "M1 验收未通过：还需要调优。"


async def main() -> int:
    parser = argparse.ArgumentParser(description="FIV-5 端到端验证")
    parser.add_argument("--runs", type=int, default=5, help="跑几次（默认 5）")
    parser.add_argument(
        "--max-total-usd",
        type=float,
        default=0.50,
        help="所有轮次加起来的成本上限（美元）。超过就停，默认 0.50",
    )
    parser.add_argument(
        "--max-total-tokens",
        type=int,
        default=2_000_000,
        help="所有轮次加起来的 token 上限。超过就停，默认 200 万",
    )
    parser.add_argument("--trace", action="store_true", help="打印工具调用轨迹")
    parser.add_argument("--offline", action="store_true", help="不调 LLM，只展示场景")
    parser.add_argument(
        "--model",
        default=None,
        help="换一个模型跑（litellm 的 provider/model 格式），不改 .env",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="打印调试日志（含堆栈）")
    args = parser.parse_args()

    # 默认安静：一行错误信息，不甩 litellm 的堆栈和横幅（上线前测试发现的）
    configure_logging(verbose=args.verbose)

    settings = get_settings()
    if args.model:
        # 用 model_copy 而不是改环境变量：这次覆盖只作用于本进程，
        # 不污染 .env，也不影响同一台机器上别的运行。
        settings = settings.model_copy(update={"llm_model": args.model})

    if args.offline:
        show_offline(make_scenario(0))
        return 0

    key_name = api_key_env_for(settings.llm_model)
    # 模型不认识时也提醒一句：与其等到 401/404，不如现在说清「名字可能不对」
    info = describe_model(settings.llm_model)
    if not info.known:
        console.print(
            f"[yellow]{settings.llm_model} 不在 litellm 本地表里[/yellow]"
            " —— 名字可能写错了（先用 `fivewhys doctor --model ...` 确认）"
        )
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
    total_tokens = 0
    attempted = 0
    traces: list[str] = []
    # provider 实际服务的模型名（约束 C-7）。可能不等于请求的那个 —— 实测
    # 请求 deepseek/deepseek-chat 时对方回 deepseek-flash。**报告里必须写真实的那个。**
    served: set[str] = set()

    for index in range(args.runs):
        # ---- 总预算闸门（上线前审查 PRE-7）----
        # 单次诊断有 $0.10 硬上限，但 `--runs 100` 这种**多轮**调用原先没有任何全局保险丝。
        # 保险丝要装在「知道还要跑几次」的地方 —— 也就是这里，而不是单次诊断里。
        if total_tokens >= args.max_total_tokens:
            console.print(
                f"[yellow]达到 token 预算 {args.max_total_tokens:,}"
                f"（已用 {total_tokens:,}），停止剩余 {args.runs - index} 次。[/yellow]"
            )
            console.print("[dim]要跑完就调大 --max-total-tokens。[/dim]")
            break

        if total_cost >= args.max_total_usd:
            console.print(
                f"[yellow]达到总预算 ${args.max_total_usd:.4f}（已花 ${total_cost:.4f}），"
                f"停止剩余 {args.runs - index} 次。[/yellow]"
            )
            console.print("[dim]要跑完就调大 --max-total-usd；先确认花的钱是你能接受的。[/dim]")
            break

        run, points, notes = await run_one(index, args.trace, settings)
        attempted += 1
        ok = points >= PASS_THRESHOLD
        passed += int(ok)
        total_cost += run.total_cost_usd
        total_tokens += run.total_tokens
        if run.served_model:
            served.add(run.served_model)
        if run.trace_path:
            traces.append(run.trace_path)

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

    # 轨迹路径要打出来：落盘了但没人知道在哪，等于没落盘
    console.print()
    if traces:
        console.print(f"[dim]轨迹：{traces[-1]}[/dim]")
        console.print("[dim]       用 `fivewhys trace latest` 看这次诊断的每一步[/dim]")
    console.print()

    if attempted == 0:
        console.print("[red]一次都没跑 —— 预算太低或参数有误。[/red]")
        return 1

    incomplete = attempted < args.runs

    verdict, verdict_text = judge(passed, attempted, args.runs)
    console.print(
        f"  通过 [bold]{passed}/{attempted}[/bold]"
        + (f"（计划 {args.runs} 次，预算中止）" if incomplete else "")
        + f"  （判定线 {PASS_THRESHOLD:.0%}）  总成本 ${total_cost:.4f}"
    )
    # 约束 C-7：这个数字是要写进 README 的，所以必须标清「真正跑的是哪个模型」。
    # 只报请求名等于在报告里写了一个没跑过的模型。
    if served:
        actual = "、".join(sorted(served))
        suffix = ""
        if actual != settings.llm_model:
            suffix = f"  [dim]（请求的是 {settings.llm_model}）[/dim]"
        console.print(f"  实际模型  : {actual}{suffix}")
    console.print(
        f"[green]{verdict_text}[/green]"
        if verdict
        else f"[{'yellow' if incomplete else 'red'}]{verdict_text}[/]"
    )
    return 0 if verdict else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
