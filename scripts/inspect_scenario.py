"""FIV-13 手工验证：**只用五件工具**，走一遍完整的排障证据链。

这个脚本回答一个问题：

    把 5 个工具交给一个（足够聪明的）模型，它**够不够**推出根因？

它不调用任何 LLM —— 它把每个工具的**原始返回**原样打印出来，
也就是 agent 在真实评测里会看到的东西。你读一遍就知道：

- 线索够不够（能不能从指标走到日志、再走到配置）
- 有没有哪一步「查了等于没查」
- 答案会不会不小心从日志里泄漏出去

用法::

    python scripts/inspect_scenario.py                     # 用默认的 db_pool 场景
    python scripts/inspect_scenario.py -c dependency_5xx   # 换一种故障
    python scripts/inspect_scenario.py -c no_fault         # 健康场景：看它会不会"查出错来"
    python scripts/inspect_scenario.py --full              # 不截断工具返回

**不联网、不花钱、不需要 API Key。**
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 让脚本能直接运行（不必先 pip install -e .）
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from datetime import timedelta  # noqa: E402

from rich.console import Console  # noqa: E402
from rich.panel import Panel  # noqa: E402
from rich.table import Table  # noqa: E402

from fivewhys.models import FaultCategory  # noqa: E402
from fivewhys.scenario import DEFAULT_SCENARIO_ROOT, Scenario, build_scenario  # noqa: E402
from fivewhys.tools import DataSource, build_registry  # noqa: E402

console = Console()

# 工具返回里如果有这么长的行，终端里看着难受 —— 只影响显示，不影响内容。
DISPLAY_LIMIT = 24


def load(args: argparse.Namespace) -> Scenario:
    """要么从磁盘上的场景包加载，要么现造一个。"""
    if args.scenario is not None:
        return Scenario.load(args.scenario)

    if args.category is not None:
        return build_scenario(FaultCategory(args.category), seed=args.seed)

    # 没指定就用 data/scenarios 里第一个能加载的场景包
    if DEFAULT_SCENARIO_ROOT.exists():
        for candidate in sorted(DEFAULT_SCENARIO_ROOT.iterdir()):
            if (candidate / "scenario.json").exists():
                return Scenario.load(candidate)

    console.print("[yellow]data/scenarios 里没有场景包，现造一个 db_pool 场景[/yellow]")
    return build_scenario(FaultCategory.DB_POOL_EXHAUSTED, seed=args.seed)


def show_tool(name: str, title: str, output: str, *, full: bool) -> None:
    lines = output.splitlines()
    shown = (
        lines
        if full or len(lines) <= DISPLAY_LIMIT
        else [
            *lines[:DISPLAY_LIMIT],
            f"…（还有 {len(lines) - DISPLAY_LIMIT} 行，用 --full 看全部）",
        ]
    )
    console.print(Panel("\n".join(shown), title=f"{name} · {title}", title_align="left"))


def pick_service(question: str, topology: dict[str, list[str]]) -> str:
    """挑出「该问哪个服务」。

    **只用 agent 拿得到的信息**：题面里点名的那个服务。
    题面里没有（或拓扑为空）时，退回拓扑的第一个 ——
    ``describe()`` 保持插入顺序，第一个就是入口服务。

    ⚠️ 曾经这里写的是 ``sorted(topology)[0]``，那会挑到 **inventory-service**
    （按字母排第三个），于是五个工具全都如实回答「没问题」。
    工具是对的，是调用方问错了对象 —— 这个 bug 是手工跑脚本时发现的：
    输出里服务的名字不对。**单测抓不到它，因为单测都显式传了服务名。**
    """
    for name in sorted(topology, key=len, reverse=True):
        if name in question:
            return name
    return next(iter(topology), "order-service")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="只用 5 个工具走一遍排障证据链")
    parser.add_argument(
        "--scenario", type=Path, help="场景包目录（默认取 data/scenarios 里的一个）"
    )
    parser.add_argument("-c", "--category", help="不用磁盘，现造一个该故障的场景")
    parser.add_argument("--seed", type=int, default=0, help="随机种子（配合 --category）")
    parser.add_argument("--service", help="问哪个服务（默认取拓扑里的入口服务）")
    parser.add_argument("--full", action="store_true", help="不截断工具返回")
    args = parser.parse_args(argv)

    scenario = load(args)
    truth = scenario.ground_truth
    source = DataSource.from_scenario(scenario)
    registry = build_registry(source)

    service = args.service or pick_service(scenario.question, scenario.topology)

    # ⚠️ 这里直接用了标准答案的时间点。因为这个脚本是**给人看工具输出**的，
    # 不是评测 agent —— agent 只看得到 question 里的「14:02 前后」。
    at = truth.injected_at
    window_start = at - timedelta(minutes=5)
    window_end = at + timedelta(minutes=5)

    console.print()
    console.print(f"[bold]问题（agent 看到的原始输入）[/bold] {scenario.question}")
    console.print(
        f"[dim]脚本自己用的窗口：{window_start:%H:%M:%S}~{window_end:%H:%M:%S}"
        f"（取自标准答案 {at:%H:%M:%S}；agent 只知道「{at:%H:%M} 前后」）[/dim]"
    )
    console.print(
        f"[dim]标准答案：{truth.fault_category.value} @ {truth.root_cause_service}"
        f" —— {truth.root_cause}[/dim]"
    )
    console.print()

    show_tool(
        "query_metrics",
        "第一步：有没有问题、从什么时候开始",
        registry.get("query_metrics")(service=service, start=window_start, end=window_end),
        full=args.full,
    )
    show_tool(
        "query_logs",
        "第二步：那个时间点的具体报错",
        registry.get("query_logs")(service=service, start=window_start, end=window_end),
        full=args.full,
    )
    show_tool(
        "get_config",
        "第三步：这段时间配置改过什么（根因通常在这）",
        registry.get("get_config")(service=service, since=window_start, at=window_end),
        full=args.full,
    )
    show_tool(
        "get_deploy_history",
        "第四步：找嫌疑，也用来排除嫌疑",
        registry.get("get_deploy_history")(service=service, start=window_start, end=window_end),
        full=args.full,
    )
    show_tool(
        "get_dependencies",
        "第五步：症状在上游时，顺着调用链往下追",
        registry.get("get_dependencies")(service=service),
        full=args.full,
    )

    # ---- 自检：答案只该出现在配置里 ----
    logs_out = registry.get("query_logs")(
        service=service,
        start=window_start,
        end=window_end,
        levels=["INFO", "WARN", "ERROR"],
        limit=200,
    )
    config_out = registry.get("get_config")(service=service)
    leaked = [kw for kw in truth.answer_keywords if kw.lower() in logs_out.lower()]
    in_config = [kw for kw in truth.answer_keywords if kw.lower() in config_out.lower()]

    if not truth.answer_keywords:
        config_verdict = "[yellow]不适用（该场景没有答案词）[/yellow]"
    elif in_config:
        config_verdict = "[green]OK[/green]"
    else:
        config_verdict = "[red]配置里查不到答案 —— 这个场景无解[/red]"

    table = Table(title="自检：答案藏在哪", header_style="bold")
    table.add_column("检查项", style="cyan")
    table.add_column("结果")
    table.add_row(
        "日志里没有答案词（FR-2）",
        "[green]OK[/green]" if not leaked else f"[red]泄漏：{leaked}[/red]",
    )
    table.add_row("答案能在配置里查到", config_verdict)
    table.add_row("工具数量", str(len(registry)))
    console.print(table)

    if leaked:
        console.print("[red]日志泄漏了答案 —— agent 不需要推理就能答对，场景失去意义。[/red]")
        return 1

    console.print(
        "\n[dim]读一遍上面五段输出：如果从「指标异常」能一路推到「配置被改小」，"
        "说明工具层给够了信息。[/dim]"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
