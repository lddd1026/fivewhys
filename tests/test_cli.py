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
from fivewhys.mock.injectors import available
from fivewhys.snapshot import build_snapshot, combine_digests, load_snapshot, save_snapshot

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
