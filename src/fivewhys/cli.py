"""fivewhys 命令行入口。

M0 阶段只有 `doctor` 和 `version` 是真正可用的。
后面的子命令会在对应里程碑里逐个加上。
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.padding import Padding
from rich.table import Table

from fivewhys import __version__
from fivewhys.config import get_settings
from fivewhys.logs import configure_logging
from fivewhys.mock.injectors import available, catalogue
from fivewhys.models import FaultCategory
from fivewhys.scenario import DEFAULT_SCENARIO_ROOT, build_scenario
from fivewhys.snapshot import (
    DEFAULT_SNAPSHOT_PATH,
    Snapshot,
    UnsafeOutputRootError,
    load_snapshot,
    save_snapshot,
    take_snapshot,
    validate_all_scenarios,
    verify_snapshot,
)
from fivewhys.trace import (
    DEFAULT_TRACE_ROOT,
    EVENT_RESPONSE,
    EVENT_TOOL_RESULT,
    find_run,
    read_events,
    summarize,
)

app = typer.Typer(
    name="fivewhys",
    help="Agent-driven root cause analysis. Ask why five times.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()

VERBOSE = False


@app.callback()
def main_callback(
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="打印调试日志（含完整异常堆栈）",
    ),
) -> None:
    """fivewhys —— agent 驱动的根因分析。"""
    global VERBOSE
    VERBOSE = verbose
    configure_logging(verbose=verbose)


# provider 前缀 -> 需要的环境变量名
PROVIDER_API_KEYS: dict[str, str] = {
    "deepseek": "DEEPSEEK_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "groq": "GROQ_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}

REQUIRED_DEPS: tuple[str, ...] = (
    "litellm",
    "pydantic",
    "pydantic_settings",
    "typer",
    "rich",
)


@app.command()
def version() -> None:
    """打印版本号。"""
    console.print(f"fivewhys [bold cyan]{__version__}[/bold cyan]")


@app.command()
def doctor() -> None:
    """体检：检查环境是否就绪。

    M0 的验收标准就是这条命令全绿。
    """
    table = Table(title="fivewhys doctor", show_lines=False, header_style="bold")
    table.add_column("检查项", style="cyan", no_wrap=True)
    table.add_column("结果", no_wrap=True)
    table.add_column("说明", style="dim")

    hard_failures = 0

    # --- 1. Python 版本 ---
    py_ok = sys.version_info >= (3, 11)
    table.add_row(
        "Python",
        "[green]OK[/green]" if py_ok else "[red]FAIL[/red]",
        f"{sys.version.split()[0]}（需要 >= 3.11）",
    )
    hard_failures += 0 if py_ok else 1

    # --- 2. 依赖是否装齐 ---
    missing = [dep for dep in REQUIRED_DEPS if importlib.util.find_spec(dep) is None]
    table.add_row(
        "依赖",
        "[green]OK[/green]" if not missing else "[red]FAIL[/red]",
        "全部就绪" if not missing else f"缺少：{', '.join(missing)}",
    )
    hard_failures += 0 if not missing else 1

    # --- 3. 配置 ---
    settings = get_settings()
    table.add_row("模型", "[green]OK[/green]", settings.llm_model)
    table.add_row("5 Whys 深度", "[green]OK[/green]", str(settings.max_why_depth))

    # --- 4. API Key（软检查，M5 之前用不到）---
    provider = settings.llm_model.split("/", 1)[0]
    key_name = PROVIDER_API_KEYS.get(provider)
    if key_name is None:
        table.add_row(
            "API Key",
            "[yellow]WARN[/yellow]",
            f"不认识 provider「{provider}」，请确认 key 已按 litellm 约定设置",
        )
    elif os.environ.get(key_name):
        table.add_row("API Key", "[green]OK[/green]", f"{key_name} 已设置")
    else:
        table.add_row(
            "API Key",
            "[yellow]WARN[/yellow]",
            f"未设置 {key_name} —— M5 联网调用前必须补上",
        )

    # --- 5. .env ---
    env_path = Path(".env")
    if env_path.exists():
        table.add_row(".env", "[green]OK[/green]", "已存在")
    else:
        table.add_row(
            ".env",
            "[yellow]WARN[/yellow]",
            "未找到。执行：cp .env.example .env",
        )

    console.print(table)

    if hard_failures:
        console.print(f"\n[red]{hard_failures} 项硬性检查未通过，先解决它们。[/red]")
        raise typer.Exit(code=1)

    console.print("\n[green]环境就绪。[/green]")
    console.print("[dim]下一步：fivewhys build-scenario 造一个场景，")
    console.print("[dim]        然后 python scripts/demo_m1.py 跑诊断。[/dim]")


def _print_problems(title: str, problems: list[str]) -> None:
    """打印问题列表。

    用 ``Padding`` 缩进，而**不是**在每行前面加 ``- ``
    —— 这些消息是中文，一整句就是一个「没有空格的长单词」。
    Rich 的换行器遇到「当前行非空且这个超长词放不下」时，
    会先把当前行（也就是光秃秃的 ``  - ``）输出，再把词硬折到下一行，
    看起来像是打错了字。让第一行空着，它就会正常填满再折。
    """
    console.print(f"[red]{title}[/red]")
    for problem in problems:
        console.print(Padding(problem, (0, 0, 0, 4)))


@app.command("trace")
def trace_cmd(
    run_ref: str = typer.Argument(
        "latest",
        help="run_id（可以只写前缀），或者 latest 看最近一次",
    ),
    root: Path = typer.Option(  # noqa: B008
        DEFAULT_TRACE_ROOT,
        "--root",
        "-r",
        help="轨迹根目录",
    ),
    full: bool = typer.Option(False, "--full", help="打印每一步的完整内容（很长）"),
) -> None:
    """看一次诊断的完整轨迹（需求 FR-9）。

    为什么要有这个命令：审查报告里那条没查明的失败，教训是
    **「轨迹落盘了但没人读」等于没落盘**。参数一给，
    人就能一眼看出模型每一步看到了什么、调了什么、最后交了什么。
    """
    try:
        path = find_run(run_ref, root=root)
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    info = summarize(path)
    console.print(f"[bold]{info['run_id']}[/bold]")
    console.print(f"  问题      : {info['question']}")
    console.print(f"  模型      : {info['model']}")
    console.print(
        f"  结果      : {info['stop_reason']}"
        + (f"  [red]{info['error']}[/red]" if info["error"] else "")
    )
    console.print(
        f"  规模      : {info['steps']} 步 / {info['tool_calls']} 次工具调用"
        f"（失败 {info['failed_tool_calls']}）"
    )
    console.print(
        f"  成本/耗时 : ${info['total_cost_usd']:.4f} / "
        f"{info['duration_s']:.1f}s   token {info['total_tokens']}"
    )
    if info["broken_lines"]:
        console.print(f"  [yellow]坏行 {info['broken_lines']} 条（写到一半断了）[/yellow]")
    if not info["has_finish"]:
        console.print("  [yellow]没有收尾事件 —— 这次运行是被打断的[/yellow]")

    console.print()
    console.print(_trace_table(path, full=full))

    if info["diagnosis"]:
        console.print()
        console.print("[bold]最终结论[/bold]")
        console.print(f"  根因      : {info['diagnosis']['root_cause']}")
        console.print(
            f"  服务/类别 : {info['diagnosis']['root_cause_service']}"
            f" / {info['diagnosis']['fault_category']}"
        )
        console.print(f"  摘要      : {info['diagnosis']['summary']}")
    console.print(f"\n[dim]原始文件：{path}[/dim]")


def _trace_table(path: Path, *, full: bool) -> Table:
    """逐步表格：**把「调用」和它的「结果」配成对**。

    为什么要配对：轨迹里 response 和 tool_result 是分开的事件，
    直接按顺序打印会变成「三步的调用排在一起、三步的结果又排在一起」，
    人读的时候对不上号 —— 而「哪个结果对应哪次调用」正是复盘的第一件事。
    """
    table = Table(header_style="bold")
    table.add_column("步", justify="right", no_wrap=True)
    table.add_column("动作", no_wrap=True)
    table.add_column("内容", overflow="fold", max_width=90)

    pending: list[tuple[int, dict[str, str]]] = []
    for event in read_events(path):
        kind = event.get("kind")

        if kind == EVENT_RESPONSE and event.get("tool_calls"):
            pending.extend((int(event["step"]), call) for call in event["tool_calls"])
            continue

        if kind == EVENT_TOOL_RESULT:
            if pending:
                step, call = pending.pop(0)
            else:  # pragma: no cover —— 正常轨迹里结果总是跟着调用
                step, call = (
                    int(event.get("step", 0)),
                    {"name": str(event.get("tool", "?")), "arguments": ""},
                )
            table.add_row(
                str(step), f"[cyan]{call['name']}[/cyan]", _clip(call.get("arguments"), full)
            )
            table.add_row(
                "",
                "  ↳ [green]OK[/green]" if event.get("ok") else "  ↳ [red]FAIL[/red]",
                _clip(event.get("result") if event.get("ok") else event.get("error"), full),
            )
            continue

        if kind == EVENT_RESPONSE and event.get("content"):
            table.add_row(str(event["step"]), "[dim]说话[/dim]", _clip(event["content"], full))
        elif kind == "broken":
            table.add_row("", "[yellow]坏行[/yellow]", _clip(event.get("raw"), full))

    # 没有结果的调用（比如中途崩了）也要显示，不能悄悄吞掉
    for step, call in pending:
        table.add_row(str(step), f"[cyan]{call['name']}[/cyan]", _clip(call.get("arguments"), full))
        table.add_row("", "  ↳ [yellow]无结果[/yellow]", "（这一步没执行完）")

    return table


def _clip(text: object, full: bool, limit: int = 90) -> str:
    body = str(text or "")
    if full:
        return body
    first = body.splitlines()[0] if body else ""
    return first[:limit]


@app.command("faults")
def faults_cmd() -> None:
    """列出已注册的故障 —— `--category` 能填什么，看这里。"""
    table = Table(title="已注册的故障", header_style="bold")
    table.add_column("--category", style="cyan", no_wrap=True)
    table.add_column("名称", no_wrap=True)
    table.add_column("说明", style="dim")

    for entry in catalogue():
        table.add_row(entry["category"], entry["name"], entry["description"])

    console.print(table)
    console.print(f"[dim]共 {len(available())} 种。加新故障见 injectors/__init__.py 的说明。[/dim]")


def _resolve_category(name: str) -> FaultCategory:
    """把命令行传来的字符串解析成故障类别。

    故意用 ``str`` 而不是直接用枚举当类型：枚举里有 9 个值，
    但 M3 只注册了 6 种。Typer 会照枚举提示 9 个选项，
    用户选了没实现的那个，报的错会很难懂。这里只认注册过的。
    """
    known = {category.value: category for category in available()}
    if name not in known:
        options = "、".join(known)
        raise typer.BadParameter(
            f"没有这个故障：「{name}」。可选：{options}（详情看 fivewhys faults）"
        )
    return known[name]


@app.command("build-scenario")
def build_scenario_cmd(
    category: str = typer.Option(
        FaultCategory.DB_POOL_EXHAUSTED.value,
        "--category",
        "-c",
        help="故障类别。可选值看 fivewhys faults",
    ),
    seed: int = typer.Option(0, "--seed", "-s", help="随机种子。同一个种子产出完全相同的场景"),
    out: Path = typer.Option(  # noqa: B008 —— typer 的惯用写法
        DEFAULT_SCENARIO_ROOT,
        "--out",
        "-o",
        help="输出目录",
    ),
) -> None:
    """构造一个评测场景并落盘。

    落盘之后这个场景就是**一份文件**：谁跑、什么时候跑，结果都一样。
    评测比的就是「同一批场景下 agent 表现如何」，所以场景必须固化下来。
    """
    scenario = build_scenario(_resolve_category(category), seed=seed)
    target = scenario.save(out)

    console.print(f"[green]场景已落盘[/green] {target}")
    console.print()
    console.print(f"  [bold]问题[/bold]     {scenario.question}")
    console.print(f"  [bold]根因服务[/bold] {scenario.ground_truth.root_cause_service}")
    console.print(f"  [bold]故障类别[/bold] {scenario.ground_truth.fault_category}")
    console.print(
        f"  [bold]数据[/bold]     日志 {len(scenario.logs)} 条 / 指标 {len(scenario.metrics)} 条 / "
        f"配置快照 {len(scenario.configs)} 条 / 发布 {len(scenario.deploys)} 条"
    )
    console.print()

    problems = scenario.validate()
    if problems:
        _print_problems("场景校验未通过：", problems)
        raise typer.Exit(code=1)

    console.print("[green]校验通过[/green]：日志未泄漏答案，question 未泄漏判分词")


@app.command("snapshot")
def snapshot_cmd(
    out: Path = typer.Option(  # noqa: B008
        DEFAULT_SCENARIO_ROOT,
        "--out",
        "-o",
        help="场景包输出目录（生成物，不进版本库）",
    ),
    manifest: Path = typer.Option(  # noqa: B008
        DEFAULT_SNAPSHOT_PATH,
        "--manifest",
        "-m",
        help="快照文件路径（进版本库的那份指纹）",
    ),
    seed: int = typer.Option(0, "--seed", "-s", help="随机种子"),
    check: bool = typer.Option(
        False,
        "--check",
        help="只校验：用当前代码重造场景，和已有快照比对（不写任何文件）",
    ),
    keep_stale: bool = typer.Option(
        False,
        "--keep-stale",
        help="不清理输出目录里陈旧的场景包（默认会清 —— 见 --help 的说明）",
    ),
) -> None:
    """给评测集拍快照，或校验它没被改过（需求 FR-4 / NFR-1）。

    为什么要有这一步：M7 要比较「改进前 vs 改进后」的准确率，
    前提是**两次跑的评测集一模一样**。注入器里随手改一行日志条数，
    评测集就变了 —— 而这个模块会把这件事变成一次红灯，而不是一个悄悄偏移的曲线。

    ``--check`` 不读磁盘上的场景包：它比对的是「代码 + 种子 → 字节」。
    所以它能在 CI 里跑，也能在刚 clone 下来、``data/`` 还是空的时候跑。
    """
    if check:
        _check_snapshot(manifest)
        return

    problems = validate_all_scenarios(seed=seed)
    if problems:
        _print_problems("有场景不可用，先修掉再拍快照：", problems)
        raise typer.Exit(code=1)

    try:
        snapshot, stale = take_snapshot(out, seed=seed, clean_stale=not keep_stale)
    except UnsafeOutputRootError as exc:
        # 这是**故意**的拒绝，不是崩溃：清理是全项目唯一会删东西的地方，
        # 宁可不干活，也不要在一个看起来像源码目录的地方乱删。
        console.print(f"[red]拒绝清理[/red] {exc}")
        console.print("[dim]场景包没有生成。换个 --out，或者加 --keep-stale 跳过清理。[/dim]")
        raise typer.Exit(code=1) from exc

    target = save_snapshot(snapshot, manifest)

    console.print(f"[bold]{snapshot.summary()}[/bold]")
    console.print(_snapshot_table(snapshot))

    for directory in stale:
        console.print(f"[yellow]清掉了陈旧的场景包[/yellow] {directory.name}")
    console.print(f"[green]场景包[/green] {out}")
    console.print(f"[green]快照已写入[/green] {target}")

    # 刚拍完立刻自校验一次 —— 立刻暴露「构造过程本身不确定」，
    # 否则要等到别人机器上校验失败才发现。
    drift = verify_snapshot(snapshot)
    if drift:
        _print_problems("刚拍完就校验不过 —— 场景构造过程不可复现：", drift)
        raise typer.Exit(code=1)

    console.print("[green]自校验通过[/green]：同样的代码和种子重造了一遍，字节完全一致")


def _snapshot_table(snapshot: Snapshot) -> Table:
    """快照清单表格。

    ⚠️ 场景 id 很长（``order-service-dependency-5xx-20260101140200``，42 字符）。
    如果给它 ``no_wrap``，在 80 列宽的终端里 Rich 会把**别的**列压成零宽度 ——
    表现是「表头莫名其妙少了一列」，这个坑 FIV-12 踩过一次。
    正确做法：让 id 列做那个可伸缩的列，超宽就省略号，其余列 ``no_wrap``。
    """
    table = Table(header_style="bold")
    table.add_column("场景", style="cyan", max_width=38, overflow="ellipsis")
    table.add_column("故障类别", no_wrap=True)
    table.add_column("指纹", no_wrap=True)
    for entry in snapshot.scenarios:
        table.add_row(
            entry.scenario_id,
            entry.fault_category.value,
            entry.digest[:12],
        )
    return table


def _check_snapshot(manifest: Path) -> None:
    """校验模式：打印逐个场景的比对结果，有问题就退出码 1。"""
    try:
        snapshot = load_snapshot(manifest)
    except FileNotFoundError as exc:
        # 缺文件是很常见的第一步失误（还没拍过快照）。
        # 直接抛出去会甩一段 traceback 给用户 —— 需求 G4 要的是「5 分钟跑通」。
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    console.print(f"[bold]{snapshot.summary()}[/bold]")

    problems = verify_snapshot(snapshot)

    table = Table(header_style="bold")
    table.add_column("场景", style="cyan", max_width=38, overflow="ellipsis")
    table.add_column("指纹", no_wrap=True)
    table.add_column("结果", no_wrap=True)
    for entry in snapshot.scenarios:
        broken = any(entry.scenario_id in problem for problem in problems)
        table.add_row(
            entry.scenario_id,
            entry.digest[:12],
            "[red]变了[/red]" if broken else "[green]一致[/green]",
        )
    console.print(table)

    if problems:
        _print_problems("快照校验未通过：", problems)
        console.print(
            "\n[dim]如果改动是有意的（比如确实加了新故障），重拍快照即可："
            "[bold]fivewhys snapshot[/bold][/dim]"
        )
        raise typer.Exit(code=1)

    console.print("[green]快照校验通过[/green]：代码 + 种子重造出来的字节与快照完全一致")


if __name__ == "__main__":
    app()
