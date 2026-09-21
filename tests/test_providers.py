"""模型元信息层（FIV-16 / 需求 FR-6、约束 C-7）。

这一层的价值全在**离线**：窗口多大、key 放哪个变量、名字认不认识，
这些问题都能在不发一次请求的情况下回答。所以整套测试也不联网。

## 为什么不能只测「函数能跑」

`describe_model` 的返回值直接决定上下文闸门。它返回错的窗口 →
闸门不是太松（等 provider 报错，那时钱已花）就是太紧（把能跑完的调查掐掉）。
所以这里断言的是**数值关系**（闸门 = 窗口 × 安全比例、未知时回落保守值），
不是「没抛异常」。
"""

from __future__ import annotations

import pytest

from fivewhys.providers import (
    CONTEXT_SAFETY_RATIO,
    PROVIDER_API_KEYS,
    UNKNOWN_MODEL_CONTEXT_TOKENS,
    ModelInfo,
    api_key_env_for,
    describe_model,
)

# --------------------------------------------------------------------------
# 真实模型：本地表里查得到
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model", "expected_provider"),
    [
        ("deepseek/deepseek-chat", "deepseek"),
        ("gpt-4o-mini", "gpt-4o-mini"),  # 无前缀时整串就是 provider 段
        ("gemini/gemini-2.0-flash", "gemini"),
    ],
)
def test_known_models_report_a_window(model: str, expected_provider: str) -> None:
    """认识的模型必须报出窗口 —— 报不出来，闸门就只能拍脑袋。"""
    info = describe_model(model)

    assert info.model == model
    assert info.provider == expected_provider
    if not info.known:  # pragma: no cover —— litellm 表里没有的话这条就是空转
        pytest.skip(f"当前 litellm 版本的本地表里没有 {model}")
    assert info.max_input_tokens, f"{model} 的窗口是空的"
    assert info.max_input_tokens > 0


def test_windows_differ_enough_that_a_hardcoded_limit_is_wrong() -> None:
    """把窗口差异**钉死成断言** —— 它就是「不许写死闸门」这个决定的依据。

    如果哪天这些模型的窗口变得一样了，这条会红，提醒我们重新评估这个设计。
    """
    windows = []
    for model in ("deepseek/deepseek-chat", "gpt-4o-mini", "gemini/gemini-2.0-flash"):
        info = describe_model(model)
        if not info.known:  # pragma: no cover
            pytest.skip(f"本地表里没有 {model}")
        assert info.max_input_tokens is not None
        windows.append(info.max_input_tokens)

    assert max(windows) / min(windows) >= 2, f"窗口没差多少（{windows}），设计前提没了"


# --------------------------------------------------------------------------
# 上下文闸门：这一层唯一真正影响行为的东西
# --------------------------------------------------------------------------


def test_context_limit_is_a_fraction_of_the_window() -> None:
    """闸门 = 窗口 × 安全比例，**不是**等于窗口。

    为什么必须留余量：我们按字符估的 token 会偏、模型这一轮还要输出、
    provider 自己也留了余量。贴着窗口设等于把「提前拦住」变成「看谁先报错」。
    """
    info = ModelInfo(model="x/y", known=True, max_input_tokens=100_000)

    assert info.context_limit() == int(100_000 * CONTEXT_SAFETY_RATIO)
    assert info.context_limit() < 100_000, "闸门必须小于窗口本身"
    assert CONTEXT_SAFETY_RATIO < 1.0


def test_explicit_override_wins() -> None:
    """用户设了具体数字就按它来 —— 复现实验时这是唯一能固定闸门的办法。"""
    info = ModelInfo(model="x/y", known=True, max_input_tokens=100_000)

    assert info.context_limit(override=12_345) == 12_345

    # override=0 也要生效：它是「设了」，不是「没设」。
    # 写成 `if override:` 就会把 0 当成没设 —— 这个坑在别处踩过。
    assert info.context_limit(override=0) == 0


def test_unknown_window_falls_back_to_the_conservative_default() -> None:
    """窗口查不到 → 用保守值。

    宁可早停：早停有明确的 stop_note 可查；猜大了则是 provider 报错，
    **而那时钱已经花了**。
    """
    info = ModelInfo(model="nobody/knows-me", known=True, max_input_tokens=None)

    assert info.context_limit() == UNKNOWN_MODEL_CONTEXT_TOKENS
    assert UNKNOWN_MODEL_CONTEXT_TOKENS <= 32_000, "未知窗口时的兜底值不能太大"


def test_unknown_model_name_does_not_raise() -> None:
    """名字写错是**正常返回值**，不是异常。

    调用方是 doctor 和诊断前的一次计算，它们要的是「能不能用、窗口多大」。
    把「未知」当成异常，调用方就会忘了处理它 —— 而这个函数的调用点
    正好都在「还没来得及 try」的位置。
    """
    info = describe_model("definitely/not-a-real-model-xyz")

    assert info.known is False
    assert info.context_limit() == UNKNOWN_MODEL_CONTEXT_TOKENS


def test_unknown_model_does_not_print_litellm_noise(capsys: pytest.CaptureFixture[str]) -> None:
    """查未知模型时**不许往屏幕上打东西**。

    litellm 走 "LLM Provider NOT provided" 分支时用 ``print()`` 甩两遍
    "Provider List: ..."（不走 logging，压 logger 级别没用）。
    它恰好出现在最需要看清的场合：``doctor --model`` 就是用来查名字写错的，
    我们的警告被库噪音夹在中间就白做了。
    修法是调用前设 ``litellm.suppress_debug_info = True``。
    """
    describe_model("definitely-not-a-provider/some-model")

    captured = capsys.readouterr()
    assert captured.out == "", f"litellm 往 stdout 打了东西：{captured.out!r}"
    assert captured.err == "", f"litellm 往 stderr 打了东西：{captured.err!r}"


# --------------------------------------------------------------------------
# API Key 的变量名：只能有一处定义
# --------------------------------------------------------------------------


def test_api_key_env_lookup() -> None:
    assert api_key_env_for("deepseek/deepseek-chat") == "DEEPSEEK_API_KEY"
    assert api_key_env_for("openai/gpt-4o-mini") == "OPENAI_API_KEY"
    assert api_key_env_for("anthropic/claude-3-5-sonnet-20241022") == "ANTHROPIC_API_KEY"


def test_unknown_provider_has_no_key_variable() -> None:
    """不认识的 provider 返回 None，让调用方说「请按 litellm 约定自己设」。

    返回一个**编造的**变量名更糟：用户会照着去设，然后发现没用。
    """
    assert api_key_env_for("some-new-provider/some-model") is None


def test_key_variable_names_are_real_env_var_names() -> None:
    """变量名得是能设的环境变量 —— 全大写、无空格。"""
    for provider, name in PROVIDER_API_KEYS.items():
        assert name == name.upper(), f"{provider} 的变量名 {name} 不是全大写"
        assert " " not in name
        assert name.endswith("_API_KEY"), f"{provider}：{name} 不符合 litellm 的约定"


# --------------------------------------------------------------------------
# 成本/价格（只用来显示，**不用来记账** —— 记账用 provider 上报的数字）
# --------------------------------------------------------------------------


def test_price_helpers_convert_to_per_million() -> None:
    info = ModelInfo(
        model="x/y",
        known=True,
        input_cost_per_token=0.00000014,
        output_cost_per_token=0.00000028,
    )

    assert info.input_price_per_million == pytest.approx(0.14)
    assert info.output_price_per_million == pytest.approx(0.28)


def test_price_helpers_return_none_when_unknown() -> None:
    """查不到价格就返回 None，让显示层决定不显示 —— 不要造一个 0.0。"""
    info = ModelInfo(model="x/y", known=True)

    assert info.input_price_per_million is None
    assert info.output_price_per_million is None


def test_end_to_end_describe_of_the_default_model() -> None:
    """默认模型必须是可以直接跑诊断的（否则 README 里那条命令就是坏的）。"""
    from fivewhys.config import Settings

    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    info = describe_model(settings.llm_model)

    assert info.known, f"默认模型 {settings.llm_model} 不在本地表里 —— 改默认值或改名字"
    assert info.api_key_env is not None, "默认模型没有对应的 key 变量名，用户会不知道填哪"


# --------------------------------------------------------------------------
# 接进主循环之后的行为（这才是「接入多模型」的真正验收）
# --------------------------------------------------------------------------


def _settings(**overrides: object):
    from fivewhys.config import Settings

    base: dict[str, object] = {
        "max_steps": 6,
        "max_cost_usd": 1.0,
        "trace_enabled": False,
        "verify_evidence": False,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg]


def _registry():
    from fivewhys.mock.logstore import LogStore
    from fivewhys.tools import DataSource, build_registry

    return build_registry(DataSource.logs_only(LogStore()))


def _served(response, model: str):
    """给一轮脚本响应贴上「provider 实际回的模型名」。

    用 ``dataclasses.replace`` 而不是给 doubles 加参数：这只服务于一种断言，
    不值得让它出现在**所有**测试都要读的构造器签名里。
    """
    import dataclasses

    return dataclasses.replace(response, served_model=model)


async def test_run_records_the_served_model_not_the_requested_one() -> None:
    """约束 C-7：报告里要标**实际服务**的模型。

    实测请求 ``deepseek/deepseek-chat``，DeepSeek 回的是 ``deepseek-flash``。
    只记请求名，等于在报告里写了一个没跑过的模型。
    """
    from doubles import ScriptedLLM, call, submit  # type: ignore[import-not-found]
    from fivewhys.agent import diagnose

    llm = ScriptedLLM(
        [
            _served(call("query_logs", {"service": "order-service"}), "deepseek-flash"),
            _served(submit(), "deepseek-flash"),
        ],
        model="deepseek/deepseek-chat",
    )

    run = await diagnose(
        scenario_id="s1",
        question="q",
        registry=_registry(),
        llm=llm,
        settings=_settings(),
    )

    assert run.model == "deepseek/deepseek-chat", "请求名照旧记着 —— 两个都要"
    assert run.served_model == "deepseek-flash"


async def test_served_model_survives_into_the_trace(tmp_path) -> None:
    """轨迹里也得有 —— 复盘的人只看得到轨迹，看不到内存里的 run。"""
    from doubles import ScriptedLLM, call, submit  # type: ignore[import-not-found]
    from fivewhys.agent import diagnose
    from fivewhys.trace import TraceWriter, read_events, summarize

    llm = ScriptedLLM(
        [
            _served(call("query_logs", {"service": "order-service"}), "deepseek-flash"),
            _served(submit(), "deepseek-flash"),
        ],
        model="deepseek/deepseek-chat",
    )
    writer = TraceWriter("served-model-run", root=tmp_path)

    await diagnose(
        scenario_id="s1",
        question="q",
        registry=_registry(),
        llm=llm,
        settings=_settings(),
        trace=writer,
    )

    finish = next(e for e in read_events(writer.path) if e["kind"] == "finish")
    assert finish["served_model"] == "deepseek-flash"
    assert summarize(writer.path)["served_model"] == "deepseek-flash"


async def test_context_gate_follows_the_models_own_window() -> None:
    """**这条是整个 FIV-16 的核心。**

    同一份「超大 prompt」，在窗口小的模型上要被提前拦住，
    在窗口大的模型上必须能正常跑完。写死一个闸门做不到这件事 ——
    要么对大的那个提前停（浪费能力），要么对小的那个等 provider 报错（白花钱）。
    """
    from doubles import ScriptedLLM, submit  # type: ignore[import-not-found]
    from fivewhys.agent import diagnose

    # 200k 字符 ≈ 66k token：超过「未知窗口」的兜底闸门（32k），
    # 远低于 gemini-2.0-flash 的闸门（1,048,576 × 0.8）。
    huge_question = "q" * 200_000

    small = ScriptedLLM([submit()], model="nobody/knows-this-model")
    blocked = await diagnose(
        scenario_id="s1",
        question=huge_question,
        registry=_registry(),
        llm=small,
        settings=_settings(max_context_tokens=None),
    )
    assert blocked.stop_reason == "max_tokens"
    assert blocked.stop_note is not None
    assert "上下文上限" in blocked.stop_note
    assert small.rounds == 0, "要在**发出去之前**拦住 —— 发了就是花了钱"

    big = ScriptedLLM([submit()], model="gemini/gemini-2.0-flash")
    if not describe_model("gemini/gemini-2.0-flash").known:  # pragma: no cover
        pytest.skip("本地表里没有 gemini-2.0-flash")
    allowed = await diagnose(
        scenario_id="s1",
        question=huge_question,
        registry=_registry(),
        llm=big,
        settings=_settings(max_context_tokens=None),
    )
    assert allowed.stop_reason != "max_tokens", "窗口大的模型被同一个闸门挡了 —— 说明闸门还是写死的"
    assert big.rounds == 1


async def test_explicit_context_limit_still_overrides_the_window() -> None:
    """自动算只是**默认**：显式配置必须仍然能压过它（复现实验要靠这个）。"""
    from doubles import ScriptedLLM, submit  # type: ignore[import-not-found]
    from fivewhys.agent import diagnose

    llm = ScriptedLLM([submit()], model="gemini/gemini-2.0-flash")
    run = await diagnose(
        scenario_id="s1",
        question="q" * 200_000,
        registry=_registry(),
        llm=llm,
        settings=_settings(max_context_tokens=100),
    )

    assert run.stop_reason == "max_tokens"
    assert "100" in (run.stop_note or "")
