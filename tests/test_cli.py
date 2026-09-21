"""CLI 与入口点的测试。

``fivewhys doctor`` 是需求 FR-14a（离线自检）的主要交付物 ——
陌生人 clone 下来第一件事就是跑它。所以它的每个分支都该被覆盖，
包括各种 WARN 路径。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from fivewhys import __version__
from fivewhys.cli import app
from fivewhys.config import get_settings
from fivewhys.mock.injectors import available
from fivewhys.snapshot import build_snapshot, combine_digests, load_snapshot, save_snapshot

PROJECT_ROOT = Path(__file__).resolve().parents[1]

runner = CliRunner()


@pytest.fixture(autouse=True)
def _fresh_settings() -> None:
    """``get_settings`` 有 lru_cache，改环境变量后必须清掉。"""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# --------------------------------------------------------------------------
# 基础命令
# --------------------------------------------------------------------------


def test_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_help_lists_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "doctor" in result.stdout


def test_no_args_shows_help() -> None:
    result = runner.invoke(app, [])
    assert "doctor" in result.stdout


# --------------------------------------------------------------------------
# doctor 的各个分支
# --------------------------------------------------------------------------


def test_doctor_passes_without_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """没有 API Key 也应该是「通过」—— 只是 WARN。

    需求 FR-14a：离线自检不需要 API Key。如果这里直接失败，
    陌生人 clone 下来第一步就卡住了。

    ⚠️ 用 ``setenv("")`` 而不是 ``delenv()``（FIV-16 踩到）：

    ``doctor`` 现在会调 ``describe_model`` → 第一次 ``import litellm``，
    而 litellm 的 ``__init__`` 里有 ``load_dotenv(override=False)`` ——
    它会把 ``.env`` 里的 ``DEEPSEEK_API_KEY`` **重新灌回** ``os.environ``。
    于是「删掉变量」这个动作会被随后的 import 撤销，测试的结果取决于
    litellm 有没有被别处先 import 过 —— 一个典型的顺序依赖。

    设成空串则是稳定的：``load_dotenv(override=False)`` 见到 key 已存在就跳过，
    而空串在我们的检查里就是「没配」——这正是 ``.env.example`` 出厂的样子。
    """
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0, result.stdout
    assert "未设置 DEEPSEEK_API_KEY" in result.stdout
    assert "环境就绪" in result.stdout


def test_doctor_reports_api_key_when_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-fake-for-test")
    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0
    assert "DEEPSEEK_API_KEY 已设置" in result.stdout


def test_doctor_warns_on_unknown_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """模型名里的 provider 不在映射表里时，要提示而不是静默放过。"""
    monkeypatch.setenv("FIVEWHYS_LLM_MODEL", "some-new-vendor/some-model")
    get_settings.cache_clear()
    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0
    assert "不认识 provider" in result.stdout


def test_doctor_reports_existing_env_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    monkeypatch.chdir(tmp_path)  # type: ignore[arg-type]
    (tmp_path / ".env").write_text("DEEPSEEK_API_KEY=sk-x\n", encoding="utf-8")  # type: ignore[operator]

    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert "已存在" in result.stdout


def test_doctor_warns_when_env_file_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    monkeypatch.chdir(tmp_path)  # type: ignore[arg-type]
    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0
    assert "未找到" in result.stdout


def test_doctor_can_check_another_model_without_touching_config() -> None:
    """`doctor --model` 是 FIV-16 的核心用途：**花钱之前**问清这个模型能不能用。

    不断言具体窗口数字（那会随 litellm 版本变），只断言四件事都被回答了：
    名字认不认识、窗口多大、闸门多少、key 放哪。
    """
    result = runner.invoke(app, ["doctor", "--model", "gemini/gemini-2.0-flash"])

    assert result.exit_code == 0, result.stdout
    assert "上下文闸门" in result.stdout
    # 覆盖只作用于这一次：配置里的模型必须没变
    assert "本次用 --model 覆盖" in result.stdout


def test_doctor_flags_an_unknown_model_name() -> None:
    """名字写错时要说「可能写错了」，而不是等第一次调用去发现。

    以前这个场景还夹着两行 litellm 用 `print()` 甩出来的红色
    "Provider List: ..."（不走 logging，压 logger 级别没用）——
    正好盖在我们的警告上。见 tests/test_providers.py 的回归测试。
    """
    result = runner.invoke(app, ["doctor", "--model", "not/a-model"])

    assert result.exit_code == 0, result.stdout
    assert "名字可能写错了" in result.stdout


# --------------------------------------------------------------------------
# .env.example 必须真的能用（FIV-16 发现）
#
# 这个文件里写着 FIVEWHYS_MODEL=... —— 而 Settings 的字段是 llm_model，
# 环境变量名是 **FIVEWHYS_LLM_MODEL**。多一个/少一个词都不会报错，
# pydantic 只会静默忽略它（extra="ignore"），于是用户改了模型却毫无效果。
#
# 更讽刺的是：这个文件自己就写着「写成 FIVWHYS_ 不会报错，只会静默失效」。
# 一个「关于静默失效的警告」本身静默失效了 —— 所以必须有机器来查。
# --------------------------------------------------------------------------


def test_env_example_only_uses_real_settings_keys() -> None:
    """`.env.example` 里每个 FIVEWHYS_* 都必须是 Settings 真的认得的字段。"""
    from pathlib import Path

    from fivewhys.config import Settings

    example = Path(__file__).resolve().parents[1] / ".env.example"
    lines = example.read_text(encoding="utf-8").splitlines()

    # 认得的变量名 = 前缀 + 字段名
    known = {f"FIVEWHYS_{name.upper()}" for name in Settings.model_fields}

    checked = 0
    for number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue  # 注释、空行、说明文字
        name = stripped.split("=", 1)[0].strip()
        if not name.startswith("FIVEWHYS_"):
            continue  # provider 的 key（DEEPSEEK_API_KEY 等）不归 Settings 管
        assert name in known, (
            f".env.example 第 {number} 行写了 {name}，但 Settings 里没有这个字段 —— "
            f"它会被静默忽略。认得的名字：{sorted(known)}"
        )
        checked += 1

    assert checked >= 5, f"只查到 {checked} 个变量，解析 .env.example 的方式可能不对"


def test_env_example_model_key_is_the_one_settings_reads() -> None:
    """单独钉住模型那一行 —— 它是最容易被写错、也最贵的一个。

    写错的后果不是报错，是**换了模型却还在用旧的**，而所有指标都跟着错。
    """
    from pathlib import Path

    example = Path(__file__).resolve().parents[1] / ".env.example"
    active = {
        line.split("=", 1)[0].strip()
        for line in example.read_text(encoding="utf-8").splitlines()
        if "=" in line and not line.strip().startswith("#")
    }

    assert "FIVEWHYS_LLM_MODEL" in active
    assert "FIVEWHYS_MODEL" not in active, "少了 LLM 三个字，这行会被静默忽略"


# --------------------------------------------------------------------------
# ⭐ .env 里的 provider key 必须真的进到进程环境里（FIV-D2）
#
# 这条以前是坏的：`SettingsConfigDict(env_file=".env")` 只服务它自己的字段
# （FIVWHYS_* 那些），而 DEEPSEEK_API_KEY 是 **litellm 从 os.environ 读的**。
# 用户照着 README「cp .env.example .env 填上 key」做完，key 被静默忽略，
# 然后收到一个莫名其妙的鉴权失败。
#
# 用子进程测：它精确复现用户的做法（一个放着 .env 的目录 + 跑 python），
# 而且不碰当前进程的模块状态。
# --------------------------------------------------------------------------


def test_env_file_key_reaches_the_process_environment(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("DEEPSEEK_API_KEY=sk-from-dotenv\n", encoding="utf-8")

    env = {key: value for key, value in os.environ.items() if key != "DEEPSEEK_API_KEY"}
    env["PYTHONPATH"] = str(PROJECT_ROOT / "src")
    env["PYTHONUTF8"] = "1"

    result = subprocess.run(  # noqa: S603 —— 参数是我们自己拼的
        [
            sys.executable,
            "-c",
            "import os, fivewhys.config; print(os.environ.get('DEEPSEEK_API_KEY'))",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "sk-from-dotenv", (
        "只写进 .env 的 key 没有进 os.environ —— litellm 拿不到它，用户会收到莫名其妙的鉴权失败"
    )


def test_real_environment_variable_wins_over_the_env_file(tmp_path: Path) -> None:
    """真实环境变量要压过 .env —— CI 注入的凭据、临时导出的变量都该赢。"""
    (tmp_path / ".env").write_text("DEEPSEEK_API_KEY=sk-from-dotenv\n", encoding="utf-8")

    env = {**os.environ, "DEEPSEEK_API_KEY": "sk-from-real-env"}
    env["PYTHONPATH"] = str(PROJECT_ROOT / "src")
    env["PYTHONUTF8"] = "1"

    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-c",
            "import os, fivewhys.config; print(os.environ.get('DEEPSEEK_API_KEY'))",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )

    assert result.stdout.strip() == "sk-from-real-env"


def test_doctor_fails_when_a_dependency_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """硬性检查失败必须退出码非 0 —— 否则 CI 里会误判成环境正常。"""
    monkeypatch.setattr(
        "fivewhys.cli.REQUIRED_DEPS",
        ("litellm", "this_module_definitely_does_not_exist"),
    )
    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 1
    assert "硬性检查未通过" in result.stdout


# --------------------------------------------------------------------------
# build-scenario
# --------------------------------------------------------------------------


def test_build_scenario_writes_a_package(tmp_path: Path) -> None:
    result = runner.invoke(app, ["build-scenario", "--seed", "1", "--out", str(tmp_path)])

    assert result.exit_code == 0, result.stdout
    assert "场景已落盘" in result.stdout
    assert "校验通过" in result.stdout

    created = list(tmp_path.iterdir())
    assert len(created) == 1, "应该只创建一个场景目录"
    assert (created[0] / "scenario.json").exists()
    assert (created[0] / "logs.jsonl").exists()
    assert (created[0] / "metrics.jsonl").exists()


def test_build_scenario_shows_the_question(tmp_path: Path) -> None:
    result = runner.invoke(app, ["build-scenario", "--seed", "0", "--out", str(tmp_path)])
    assert "order-service 从" in result.stdout


def test_build_scenario_is_reproducible(tmp_path: Path) -> None:
    first, second = tmp_path / "a", tmp_path / "b"
    runner.invoke(app, ["build-scenario", "--seed", "5", "--out", str(first)])
    runner.invoke(app, ["build-scenario", "--seed", "5", "--out", str(second)])

    manifest_a = next(first.iterdir()) / "scenario.json"
    manifest_b = next(second.iterdir()) / "scenario.json"
    assert manifest_a.read_text(encoding="utf-8") == manifest_b.read_text(encoding="utf-8")


def test_build_scenario_accepts_a_category(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["build-scenario", "--category", "memory_leak", "--out", str(tmp_path)],
    )

    assert result.exit_code == 0, result.stdout
    assert "memory_leak" in result.stdout
    assert "memory-leak" in next(tmp_path.iterdir()).name


def test_build_scenario_rejects_an_unregistered_category(tmp_path: Path) -> None:
    """``slow_query`` 在枚举里但还没实现 —— 报错要指出这一点，而不是只说「不合法」。

    只断言「说了没有这个故障」：错误信息会被 Rich 按终端宽度折行，
    长单词（故障名）在中间断开，断言具体词会误判。
    """
    result = runner.invoke(
        app,
        ["build-scenario", "--category", "slow_query", "--out", str(tmp_path)],
    )

    assert result.exit_code != 0
    assert "没有这个故障" in result.output


def test_faults_lists_every_registered_fault() -> None:
    result = runner.invoke(app, ["faults"])

    assert result.exit_code == 0
    for category in available():
        assert category.value in result.stdout


# --------------------------------------------------------------------------
# snapshot（FIV-12）
# --------------------------------------------------------------------------


def test_snapshot_writes_packages_and_a_manifest(tmp_path: Path) -> None:
    manifest = tmp_path / "snapshot.json"
    out = tmp_path / "scenarios"

    result = runner.invoke(app, ["snapshot", "--out", str(out), "--manifest", str(manifest)])

    assert result.exit_code == 0, result.stdout
    assert "自校验通过" in result.stdout
    assert manifest.exists()

    snapshot = load_snapshot(manifest)
    assert len(snapshot.scenarios) == len(available())
    for entry in snapshot.scenarios:
        assert (out / entry.scenario_id / "scenario.json").exists()


def test_snapshot_check_passes_right_after_taking(tmp_path: Path) -> None:
    manifest = tmp_path / "snapshot.json"
    runner.invoke(
        app, ["snapshot", "--out", str(tmp_path / "scenarios"), "--manifest", str(manifest)]
    )

    result = runner.invoke(app, ["snapshot", "--check", "--manifest", str(manifest)])

    assert result.exit_code == 0, result.stdout
    assert "快照校验通过" in result.stdout


def test_snapshot_check_fails_when_the_snapshot_is_stale(tmp_path: Path) -> None:
    """场景数据变了（这里把记录里的指纹改成对不上的值）→ 校验必须失败，并告诉人怎么修。

    注意不能用「换一个种子」来伪造：快照里记了种子，校验会照着那个种子重放，
    照样一致。**唯一能造成不一致的就是代码变了或快照被改过** —— 这正是它的意义。
    """
    manifest = tmp_path / "snapshot.json"
    fresh = build_snapshot(seed=0)
    entry = fresh.scenarios[0]
    files = {**entry.files, "logs.jsonl": "f" * 64}
    drifed = fresh.model_copy(
        update={
            "scenarios": [
                entry.model_copy(update={"files": files, "digest": combine_digests(files.items())}),
                *fresh.scenarios[1:],
            ]
        }
    )
    save_snapshot(drifed, manifest)

    result = runner.invoke(app, ["snapshot", "--check", "--manifest", str(manifest)])

    assert result.exit_code == 1
    assert "快照校验未通过" in result.stdout
    assert "fivewhys snapshot" in result.stdout, "报错里要给出修复命令"


def test_snapshot_check_reports_a_missing_manifest(tmp_path: Path) -> None:
    """缺快照文件是最常见的第一步失误 —— 要给一句人话，不要甩 traceback。"""
    result = runner.invoke(app, ["snapshot", "--check", "--manifest", str(tmp_path / "nope.json")])

    assert result.exit_code == 1
    assert "没有快照文件" in result.stdout
    assert "Traceback" not in result.output


# --------------------------------------------------------------------------
# 模块入口点：python -m fivewhys
# --------------------------------------------------------------------------


def test_module_entry_point_works() -> None:
    """``python -m fivewhys version`` 必须能用 —— 有些环境里只有这种调用方式。"""
    result = subprocess.run(
        [sys.executable, "-m", "fivewhys", "version"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert __version__ in result.stdout
