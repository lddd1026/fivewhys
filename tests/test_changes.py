"""FIV-8 验收测试：配置历史与发布记录。

对应需求：FR-1（模拟系统，M2 阶段：配置 + 发布）

最重要的是这一条设计意图：
**答案在配置里，不在日志里。**
日志是「现象」（一搜就全看见），配置是「证据」（必须先怀疑到才会去查）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from fivewhys.mock import ConfigStore, DeployStore, LogStore, MockSystem
from fivewhys.mock.changes import ConfigChange, DeployRecord
from fivewhys.mock.scenarios import inject_db_pool_exhausted

T0 = datetime(2026, 1, 1, 14, 0, tzinfo=UTC)


# --------------------------------------------------------------------------
# ConfigStore 的基本行为
# --------------------------------------------------------------------------


def test_record_and_len() -> None:
    store = ConfigStore()
    store.record_values(T0, "order-service", {"db.pool_size": 50})
    store.record_values(T0 + timedelta(minutes=1), "order-service", {"db.pool_size": 5})

    assert len(store) == 2
    assert store.services() == ["order-service"]


def test_latest_without_time_returns_newest() -> None:
    store = ConfigStore()
    store.record_values(T0, "svc", {"a": 1})
    store.record_values(T0 + timedelta(minutes=1), "svc", {"a": 2})

    latest = store.latest("svc")
    assert latest is not None
    assert latest.values == {"a": 2}


def test_latest_at_a_point_in_time() -> None:
    """排障时要问的是「故障当时那份配置是什么」，不是「现在是什么」。"""
    store = ConfigStore()
    store.record_values(T0, "svc", {"a": 1})
    store.record_values(T0 + timedelta(minutes=10), "svc", {"a": 2})

    at = store.latest("svc", T0 + timedelta(minutes=5))
    assert at is not None and at.values == {"a": 1}

    after = store.latest("svc", T0 + timedelta(minutes=15))
    assert after is not None and after.values == {"a": 2}


def test_latest_returns_none_when_nothing_recorded() -> None:
    assert ConfigStore().latest("svc") is None


def test_latest_returns_none_before_the_first_snapshot() -> None:
    store = ConfigStore()
    store.record_values(T0 + timedelta(minutes=10), "svc", {"a": 1})
    assert store.latest("svc", T0) is None


def test_history_filters_by_window() -> None:
    store = ConfigStore()
    for i in range(5):
        store.record_values(T0 + timedelta(minutes=i), "svc", {"a": i})

    window = store.history("svc", T0 + timedelta(minutes=1), T0 + timedelta(minutes=3))
    assert len(window) == 3


def test_history_filters_by_service() -> None:
    store = ConfigStore()
    store.record_values(T0, "a", {"x": 1})
    store.record_values(T0, "b", {"x": 1})
    assert len(store.history("a")) == 1


# --------------------------------------------------------------------------
# ⭐ changes() —— agent 真正需要的形态
# --------------------------------------------------------------------------


def test_changes_detects_a_modified_key() -> None:
    store = ConfigStore()
    store.record_values(T0, "svc", {"db.pool_size": 50, "other": 1})
    store.record_values(T0 + timedelta(seconds=2), "svc", {"db.pool_size": 5, "other": 1})

    changes = store.changes("svc")
    assert len(changes) == 1
    change = changes[0]
    assert isinstance(change, ConfigChange)
    assert change.key == "db.pool_size"
    assert change.old == 50
    assert change.new == 5
    assert change.ts == T0 + timedelta(seconds=2)
    assert change.description == "db.pool_size: 50 -> 5"


def test_changes_detects_added_and_removed_keys() -> None:
    store = ConfigStore()
    store.record_values(T0, "svc", {"gone": 1})
    store.record_values(T0 + timedelta(seconds=1), "svc", {"added": 2})

    keys = {c.key for c in store.changes("svc")}
    assert keys == {"gone", "added"}


def test_changes_ignores_unchanged_keys() -> None:
    store = ConfigStore()
    store.record_values(T0, "svc", {"a": 1, "b": 2})
    store.record_values(T0 + timedelta(seconds=1), "svc", {"a": 1, "b": 2})
    assert store.changes("svc") == []


def test_changes_filters_by_window() -> None:
    store = ConfigStore()
    store.record_values(T0, "svc", {"a": 1})
    store.record_values(T0 + timedelta(minutes=1), "svc", {"a": 2})
    store.record_values(T0 + timedelta(minutes=5), "svc", {"a": 3})

    early = store.changes("svc", end=T0 + timedelta(seconds=30))
    assert early == []

    late = store.changes("svc", start=T0 + timedelta(minutes=2))
    assert len(late) == 1 and late[0].new == 3


def test_changes_of_a_single_snapshot_is_empty() -> None:
    """只有一份快照时没有「变化」可言 —— 不能崩，也不能瞎报。"""
    store = ConfigStore()
    store.record_values(T0, "svc", {"a": 1})
    assert store.changes("svc") == []


# --------------------------------------------------------------------------
# 持久化
# --------------------------------------------------------------------------


def test_config_jsonl_round_trip(tmp_path: Path) -> None:
    store = ConfigStore()
    store.record_values(T0, "svc", {"a": 1, "b": "x"}, note="initial")
    store.record_values(T0 + timedelta(minutes=1), "svc", {"a": 2, "b": "x"}, note="reload")

    restored = ConfigStore.load_jsonl(store.dump_jsonl(tmp_path / "configs.jsonl"))
    assert restored.all() == store.all()


def test_deploy_jsonl_round_trip(tmp_path: Path) -> None:
    store = DeployStore()
    store.add(T0, "svc", "v1.2.3", operator="alice", note="hotfix")

    restored = DeployStore.load_jsonl(store.dump_jsonl(tmp_path / "deploys.jsonl"))
    assert restored.all() == store.all()


def test_deploy_record_shape() -> None:
    record = DeployRecord(ts=T0, service="svc", version="v1", operator="bob", note="n")
    assert record.service == "svc"


def test_deploy_history_filters() -> None:
    store = DeployStore()
    store.add(T0, "a", "v1")
    store.add(T0 + timedelta(minutes=1), "b", "v1")
    store.add(T0 + timedelta(minutes=2), "a", "v2")

    assert len(store.history()) == 3
    assert len(store.history("a")) == 2
    assert len(store.history(start=T0 + timedelta(minutes=1))) == 2
    assert len(store.history("a", end=T0)) == 1


# --------------------------------------------------------------------------
# 与 MockSystem 的集成
# --------------------------------------------------------------------------


def test_bootstrap_records_initial_config_for_each_service() -> None:
    logs = LogStore()
    system = MockSystem(logs, seed=0)
    system.emit_request(T0)

    assert set(system.configs.services()) == {
        "order-service",
        "payment-service",
        "inventory-service",
    }
    assert len(system.deploys) == 3


def test_bootstrap_is_idempotent() -> None:
    """重复调用不能把配置历史撑爆 —— 否则「变化」全是噪声。"""
    logs = LogStore()
    system = MockSystem(logs, seed=0)
    system.bootstrap(T0)
    system.bootstrap(T0 + timedelta(minutes=1))
    system.bootstrap(T0 + timedelta(minutes=2))

    assert len(system.configs) == 3


def test_initial_config_has_a_large_connection_pool() -> None:
    """初始 pool_size 是 50 —— 故障会把它改成 5。"""
    logs = LogStore()
    system = MockSystem(logs, seed=0)
    system.emit_request(T0)

    snapshot = system.configs.latest("order-service", T0)
    assert snapshot is not None
    assert snapshot.values["db.pool_size"] == 50


# --------------------------------------------------------------------------
# ⭐⭐ 核心：答案在配置里，不在日志里
# --------------------------------------------------------------------------


def test_answer_lives_in_config_not_in_logs() -> None:
    """这是刻意的设计，不是巧合。

    - 日志是**现象**：agent 一搜就全看见了，不需要推理
    - 配置是**证据**：agent 必须先怀疑到「配置变过」，才会去查它

    如果把 pool_size 写进日志，这个场景就没难度了 ——
    agent 只要 grep 一下 "pool" 就完事。
    """
    logs = LogStore()
    system = MockSystem(logs, seed=0)
    fault_at = T0 + timedelta(seconds=10)
    system.normal_operation(T0, fault_at, rps=2.0)

    truth = inject_db_pool_exhausted(
        logs,
        system.service("order-service"),
        fault_at,
        metrics=system.metrics,
        configs=system.configs,
    )

    # 日志里不能出现答案词
    log_text = " ".join(entry.message.lower() for entry in logs.all())
    for keyword in truth.answer_keywords:
        assert keyword.lower() not in log_text, f"日志泄漏了答案词「{keyword}」"

    # 但配置历史里必须有 —— 否则这个场景无解
    changes = system.configs.changes("order-service")
    assert changes, "配置历史里必须有一次变更"
    pool_changes = [c for c in changes if "pool_size" in c.key]
    assert pool_changes, f"应该有一次 pool_size 变更，实际：{changes}"
    assert pool_changes[0].old == 50
    assert pool_changes[0].new == 5


def test_config_change_timestamp_matches_the_log_line() -> None:
    """配置变更的时间点必须和日志里那条「config reloaded」严格对齐。

    对不齐的话 agent 就没法把它们关联起来 —— 而关联正是推理的核心。
    """
    logs = LogStore()
    system = MockSystem(logs, seed=0)
    fault_at = T0 + timedelta(seconds=10)
    system.normal_operation(T0, fault_at, rps=2.0)
    inject_db_pool_exhausted(
        logs,
        system.service("order-service"),
        fault_at,
        metrics=system.metrics,
        configs=system.configs,
    )

    reload_line = next(entry for entry in logs.all() if entry.message.startswith("config reloaded"))
    change = system.configs.changes("order-service")[0]

    assert change.ts == reload_line.ts, "配置变更时间与日志时间对不上"


def test_injector_without_configs_still_works() -> None:
    """不传 configs 时行为和之前完全一致 —— 向后兼容。"""
    logs = LogStore()
    service = MockSystem(logs, seed=0).service("order-service")
    truth = inject_db_pool_exhausted(logs, service, T0)
    assert truth.root_cause_service == "order-service"


def test_config_history_is_reproducible() -> None:
    def build() -> list[str]:
        logs = LogStore()
        system = MockSystem(logs, seed=5)
        system.normal_operation(T0, T0 + timedelta(seconds=10), rps=2.0)
        inject_db_pool_exhausted(
            logs,
            system.service("order-service"),
            T0 + timedelta(seconds=10),
            metrics=system.metrics,
            configs=system.configs,
        )
        return [str(s) for s in system.configs.all()]

    assert build() == build()


def test_system_accepts_external_config_stores() -> None:
    """场景包要把这些装在一起，所以必须能注入外部仓库。"""
    shared_configs = ConfigStore()
    shared_deploys = DeployStore()
    system = MockSystem(
        LogStore(),
        configs=shared_configs,
        deploys=shared_deploys,
        seed=0,
    )
    system.emit_request(T0)

    assert system.configs is shared_configs
    assert system.deploys is shared_deploys
    assert len(shared_configs) == 3


def test_custom_initial_configs() -> None:
    """允许自定义初始配置 —— M3 构造别的故障场景时会用。"""
    system = MockSystem(
        LogStore(),
        configs_by_service={"order-service": {"db.pool_size": 8}},
        seed=0,
    )
    system.emit_request(T0)

    snapshot = system.configs.latest("order-service", T0)
    assert snapshot is not None
    assert snapshot.values["db.pool_size"] == 8

    # 没给配置的服务应该拿到空配置，而不是崩掉
    other = system.configs.latest("payment-service", T0)
    assert other is not None
    assert other.values == {}


def test_clear() -> None:
    configs = ConfigStore()
    configs.record_values(T0, "svc", {"a": 1})
    configs.clear()
    assert len(configs) == 0

    deploys = DeployStore()
    deploys.add(T0, "svc", "v1")
    deploys.clear()
    assert len(deploys) == 0


@pytest.mark.parametrize("value", [0, 1, 5, 50, 1000])
def test_pool_size_values_survive_round_trip(value: int) -> None:
    store = ConfigStore()
    store.record_values(T0, "svc", {"db.pool_size": value})
    snapshot = store.latest("svc")
    assert snapshot is not None
    assert snapshot.values["db.pool_size"] == value
