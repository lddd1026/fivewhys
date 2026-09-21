"""CLI 与入口点的测试。

``fivewhys doctor`` 是需求 FR-14a（离线自检）的主要交付物 ——
陌生人 clone 下来第一件事就是跑它。所以它的每个分支都该被覆盖，
包括各种 WARN 路径。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from fivewhys import __version__
from fivewhys.cli import app
from fivewhys.config import get_settings

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
    """
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
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
