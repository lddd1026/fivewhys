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
from rich.table import Table

from fivewhys import __version__
from fivewhys.config import get_settings
from fivewhys.scenario import DEFAULT_SCENARIO_ROOT, build_db_pool_scenario

app = typer.Typer(
    name="fivewhys",
    help="Agent-driven root cause analysis. Ask why five times.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()

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


@app.command("build-scenario")
def build_scenario_cmd(
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
    scenario = build_db_pool_scenario(seed=seed)
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
        console.print("[red]场景校验未通过：[/red]")
        for problem in problems:
            console.print(f"  - {problem}")
        raise typer.Exit(code=1)

    console.print("[green]校验通过[/green]：日志未泄漏答案，question 未泄漏判分词")


if __name__ == "__main__":
    app()
