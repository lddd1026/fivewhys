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
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

# 让脚本能直接运行（不必先 pip install -e .）
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rich.console import Console  # noqa: E402
from rich.table import Table  # noqa: E402

from fivewhys.agent import diagnose  # noqa: E402
from fivewhys.config import get_settings  # noqa: E402
from fivewhys.mock.logstore import LogStore  # noqa: E402
from fivewhys.mock.scenarios import inject_db_pool_exhausted  # noqa: E402
from fivewhys.mock.service import MockService  # noqa: E402
from fivewhys.models import AgentRun, GroundTruth  # noqa: E402
from fivewhys.scoring import PASS_THRESHOLD, score_diagnosis  # noqa: E402
from fivewhys.tools import DataSource, build_registry  # noqa: E402

console = Console()

T0 = datetime(2026, 1, 1, 14, 0, tzinfo=UTC)
FAULT_AT = T0 + timedelta(minutes=30)

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


def build_scenario(seed: int) -> tuple[LogStore, GroundTruth, str]:
    """造一个「正常 30 分钟 + 故障 5 分钟」的场景。"""
    store = LogStore()
    service = MockService("order-service", store, seed=seed)
    service.normal_operation(T0, FAULT_AT)
    truth = inject_db_pool_exhausted(store, service, FAULT_AT)

    # 需求 FR-15：只给服务名 + 粗略时间 + 表面症状，不给根因
    question = f"order-service 从 {FAULT_AT:%H:%M} 前后开始错误率飙升，帮忙定位一下原因"
    return store, truth, question


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


def show_offline(store: LogStore, truth: GroundTruth, question: str) -> None:
    """不调 LLM，只展示场景本身 —— 对应需求 FR-14a 的离线可看性。"""

    console.print("[bold]场景概览[/bold]（不调用任何 LLM）\n")
    console.print(f"  问题      : {question}")
    console.print(f"  日志总量  : {len(store)} 条  {store.stats()}")
    console.print(f"  标准答案  : [{truth.fault_category}] {truth.root_cause_service}")
    console.print(f"  判分关键词: {', '.join(truth.match_keywords)}\n")

    console.print("[bold]故障期间的日志（前 12 条）[/bold]")
    fault_lines = [e for e in store.all() if e.ts >= truth.injected_at]
    for entry in fault_lines[:12]:
        style = {"ERROR": "red", "WARN": "yellow"}.get(entry.level.value, "dim")
        trace = f"  trace={entry.trace_id}" if entry.trace_id else ""
        console.print(
            f"  [{style}]{entry.ts:%H:%M:%S} {entry.level.value:<5}[/{style}]"
            f" {entry.message}{trace}",
            soft_wrap=True,
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
    console.print()
    console.print(f"  [dim]答案词（不许出现在日志）: {', '.join(truth.answer_keywords)}[/dim]")
    console.print(f"  [dim]判分词（给 agent 答案打分）: {', '.join(truth.match_keywords)}[/dim]")


def show_trace(run: AgentRun) -> None:
    console.print("\n  [dim]推理轨迹：[/dim]")
    for record in run.tool_calls:
        mark = "[green]OK  [/green]" if record.ok else "[red]FAIL[/red]"
        detail = (record.result_summary or record.error or "").splitlines()[0][:70]
        console.print(f"    step{record.step} {mark} {record.tool:<12} {detail}")


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------


async def run_one(seed: int, trace: bool) -> tuple[AgentRun, float, list[str]]:
    store, truth, question = build_scenario(seed)
    run = await diagnose(
        scenario_id=truth.scenario_id,
        question=question,
        registry=build_registry(DataSource.logs_only(store)),
        settings=get_settings(),
    )

    # 注意参数名叫 trace 而不是 show_trace ——
    # 叫 show_trace 会把模块级的 show_trace() 函数遮蔽掉，
    # 变成 "TypeError: 'bool' object is not callable"。
    # 这个 bug 是端到端测试发现的：--offline 不走这里，单测也不跑脚本。
    if trace:
        show_trace(run)

    if run.diagnosis is None:
        return run, 0.0, [f"未提交结论（{run.stop_reason}）"]

    result = score_diagnosis(run.diagnosis, truth)
    return run, result.total, result.notes


async def main() -> int:
    parser = argparse.ArgumentParser(description="FIV-5 端到端验证")
    parser.add_argument("--runs", type=int, default=5, help="跑几次（默认 5）")
    parser.add_argument("--trace", action="store_true", help="打印工具调用轨迹")
    parser.add_argument("--offline", action="store_true", help="不调 LLM，只展示场景")
    args = parser.parse_args()

    settings = get_settings()

    if args.offline:
        store, truth, question = build_scenario(0)
        show_offline(store, truth, question)
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
