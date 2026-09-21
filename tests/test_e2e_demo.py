"""端到端测试：把 ``demo_m1.py`` 当子进程跑，指向本地假 LLM 服务。

## 这是不开网络、不花钱能拿到的最强保证

真实的子进程、真实的 HTTP、真实的 litellm、真实的工具调用往返、
真实的判分与结论输出。**唯一假的是「模型智力」。**

它能抓到 ``ScriptedLLM``（假 Python 对象）抓不到的东西：
HTTP 层、JSON 序列化、litellm 的适配、进程边界、CLI 参数解析、退出码。

## 但它不能替代 FIV-5 的真实运行

响应是脚本化的，不会推理。它证明「链路是通的」，不证明「模型够聪明」。

对应需求：FR-6（诊断 agent）、FR-11（指标计算）、FR-14b（快速上手）
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from fivewhys.config import get_settings
from mock_llm_server import fake_llm_server

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_env_var_prefix_is_fivewhys(monkeypatch: pytest.MonkeyPatch) -> None:
    """守住环境变量前缀 ``FIVEWHYS_``。

    写成 ``FIVWHYS_``（少一个 E）不会报任何错 —— 设置项静默保持默认值，
    然后所有请求都打到官方接口上。这个拼写错误真的发生过一次，
    表现是「明明指着本地假服务，却收到真实的鉴权失败」。
    """
    monkeypatch.setenv("FIVEWHYS_API_BASE", "http://127.0.0.1:9")
    get_settings.cache_clear()
    try:
        assert get_settings().api_base == "http://127.0.0.1:9"
    finally:
        get_settings.cache_clear()

    monkeypatch.delenv("FIVEWHYS_API_BASE", raising=False)
    monkeypatch.setenv("FIVWHYS_API_BASE", "http://127.0.0.1:9")
    get_settings.cache_clear()
    try:
        assert get_settings().api_base is None, "拼错的前缀不该生效 —— 这正是要防的"
    finally:
        get_settings.cache_clear()


def _run_demo(
    base_url: str | None,
    *args: str,
    with_key: bool = True,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """在子进程里跑 demo。

    ⚠️ ``cwd`` 是有意义的，不只是路径问题：``.env`` 是按**当前目录**找的。
    默认在项目根目录跑（找得到 ``.env``）；要测「没有任何 key」的场景，
    必须换到一个**没有 .env 的目录**去 —— 光从环境变量里删掉 key 不够，
    因为 ``.env`` 会把它补回来（FIV-D2 之后它真的会生效了）。
    """
    env = {key: value for key, value in os.environ.items() if key != "DEEPSEEK_API_KEY"}
    env["PYTHONUTF8"] = "1"
    if with_key:
        env["DEEPSEEK_API_KEY"] = "sk-fake-for-local-test"
    else:
        # ⚠️ 故意设成**空字符串**，而不是删掉它。
        #
        # 删掉没用：`load_dotenv(override=False)` 会把项目根目录 `.env` 里的
        # 真实 key 补回来（python-dotenv 的 find_dotenv 会从调用它的文件出发
        # 逐级向上找，所以换 cwd 也躲不掉）。
        # 而空字符串正好就是一个**真实存在**的情形：
        # 用户 `cp .env.example .env` 但忘了填 key —— 模板里那行就是空的。
        env["DEEPSEEK_API_KEY"] = ""
    if base_url:
        # ⚠️ 前缀是 FIVEWHYS_（五个字母 five + whys）。
        # 写成 FIVWHYS_ 不会报错，只会静默失效 —— settings.api_base 保持 None，
        # litellm 转头去调真实的 DeepSeek 接口，然后被拒。
        # 这个拼写错误真的发生过一次，所以下面专门加了一个测试守住它。
        env["FIVEWHYS_API_BASE"] = base_url

    return subprocess.run(  # noqa: S603 —— 参数是我们自己拼的，不是外部输入
        [sys.executable, str(PROJECT_ROOT / "scripts" / "demo_m1.py"), *args],
        cwd=cwd or PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=240,
        check=False,
    )


def test_demo_passes_against_local_llm() -> None:
    """完整链路：子进程 -> HTTP -> litellm -> 工具调用 -> 判分 -> 判定。"""
    with fake_llm_server() as base_url:
        result = _run_demo(base_url, "--runs", "2", "--trace")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "通过 2/2" in result.stdout
    assert "M1 验收通过" in result.stdout

    # 工具调用真的发生了（trace 模式会打印轨迹）
    assert "query_logs" in result.stdout
    assert "submitted" in result.stdout


def test_demo_offline_mode_needs_no_key_and_no_network() -> None:
    """FR-14a：没有任何 API Key 也能看到场景长什么样。"""
    result = _run_demo(None, "--offline", with_key=False)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "答案词没有泄漏到日志里" in result.stdout
    assert "关键线索已出现" in result.stdout


def test_demo_offline_shows_the_whole_scenario_and_where_the_answer_is() -> None:
    """离线的输出要能让人一眼看懂这个场景考什么、答案藏在哪。

    断言故障类别里那对方括号 —— Rich 会把 ``[db_pool_exhausted]`` 当成标记
    （markup）整个吞掉，屏幕上只剩一个服务名，类别凭空消失。
    这个 bug 就是跑 ``--offline`` 看输出时发现的。
    """
    result = _run_demo(None, "--offline", with_key=False)

    assert "db_pool_exhausted" in result.stdout, "故障类别被 Rich 的标记吞了"
    assert "db.pool_size: 50 -> 5" in result.stdout, "没把「答案在配置里」展示出来"
    for tool in ("query_metrics", "query_logs", "get_config"):
        assert tool in result.stdout, f"没告诉人 {tool} 可用"


def test_demo_walks_the_whole_evidence_chain_through_the_loop() -> None:
    """端到端跑的是**完整证据链**，不只是查一次日志。

    假模型会依次调用指标 / 日志 / 配置 / 发布，再提交结论。
    断言每一步都真的被主循环分发过 —— 工具 schema、参数校验、工具分发，
    链路上任何一处坏了都会在这里露出来。
    """
    with fake_llm_server() as base_url:
        result = _run_demo(base_url, "--runs", "1", "--trace")

    assert result.returncode == 0, result.stdout + result.stderr
    for tool in ("query_metrics", "query_logs", "get_config", "get_deploy_history"):
        assert tool in result.stdout, f"{tool} 没有被调用"
    assert "submitted" in result.stdout
    assert "M1 验收通过" in result.stdout


def test_demo_fails_cleanly_without_api_key(tmp_path: Path) -> None:
    """缺 key 时要给出能照着做的提示，而不是抛一堆栈。

    在一个**没有 .env 的目录**里跑：`.env` 会提供 key，所以光删环境变量不够。
    这同时更贴近真实场景 —— 陌生人 clone 下来还没配 key 的时候。
    """
    result = _run_demo(None, "--runs", "1", with_key=False, cwd=tmp_path)

    assert result.returncode == 1
    assert "缺少 DEEPSEEK_API_KEY" in result.stdout
    assert ".env" in result.stdout
    assert "Traceback" not in result.stdout


def test_demo_stops_at_the_total_budget() -> None:
    """⭐ 总预算闸门（上线前审查 PRE-7）。

    单次诊断有 $0.10 硬上限，但 `--runs 100` 原先没有任何全局保险丝 ——
    花的是用户的钱，必须有个「到这儿就停」的地方。
    保险丝装在知道还要跑几次的地方，也就是这里。
    """
    with fake_llm_server() as base_url:
        result = _run_demo(base_url, "--runs", "5", "--max-total-usd", "0")

    assert result.returncode == 1
    assert "达到总预算" in result.stdout


def _load_demo_module() -> object:
    """把 ``scripts/demo_m1.py`` 当模块加载（它不是包的一部分）。"""
    import importlib.util

    path = PROJECT_ROOT / "scripts" / "demo_m1.py"
    spec = importlib.util.spec_from_file_location("demo_m1_for_tests", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_demo_does_not_claim_a_verdict_on_partial_runs() -> None:
    """只跑了一部分时**不判定通过** —— 拿不完整的数据下结论就是假数字。

    判定逻辑抽成了纯函数 `judge()`，所以这里可以直接测各种组合，
    不需要真花钱把预算跑超（假 LLM 服务的成本是 0，
    在它上面永远走不到「跑了一部分就超预算」那条路）。
    """
    demo = _load_demo_module()

    assert demo.judge(passed=5, attempted=5, planned=5)[0] is True
    assert demo.judge(passed=3, attempted=5, planned=5)[0] is True, "3/5 是判定线"

    ok, why = demo.judge(passed=2, attempted=5, planned=5)
    assert ok is False and "未通过" in why

    ok, why = demo.judge(passed=2, attempted=2, planned=5)
    assert ok is False, "只跑了 2 次却说通过 = 编数字"
    assert "未跑满" in why and "不构成验收结论" in why

    ok, why = demo.judge(passed=0, attempted=0, planned=5)
    assert ok is False and "一次都没跑" in why


def test_demo_reports_failure_when_model_is_useless() -> None:
    """模型什么都不做时，判定必须是「未通过」而不是崩溃。

    这也是 M6 要统计的情形之一：agent 没能提交结论。
    """

    def useless_responder(_messages: list[dict[str, object]]) -> dict[str, object]:
        return {
            "id": "chatcmpl-mock",
            "object": "chat.completion",
            "created": 0,
            "model": "deepseek/deepseek-chat",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "我不知道。"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }

    with fake_llm_server(useless_responder) as base_url:
        result = _run_demo(base_url, "--runs", "1")

    assert result.returncode == 1
    assert "M1 验收未通过" in result.stdout
    assert "max_steps" in result.stdout
    assert "Traceback" not in result.stdout
