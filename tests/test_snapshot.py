"""FIV-12 验收测试：场景快照与可复现性。

对应需求：FR-4（相同的场景标识必须产生逐字节一致的数据）、NFR-1（显式种子）、
FR-15（场景包可独立加载）。

这一层的价值全在**能不能抓到问题**上。一个永远返回「一致」的校验器
等于不存在，所以下面每个「能抓到」的测试都人为制造一种故障：
改一个字节、删一份文件、改场景 id、改快照文件本身。
"""

from __future__ import annotations

import subprocess
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from fivewhys.mock import LogStore, MockService
from fivewhys.mock.injectors import available
from fivewhys.models import FaultCategory, GroundTruth, LogLevel
from fivewhys.scenario import (
    CONFIGS_NAME,
    LOGS_NAME,
    MANIFEST_NAME,
    METRICS_NAME,
    Scenario,
    ScenarioManifest,
    build_all_scenarios,
    build_scenario,
)
from fivewhys.snapshot import (
    DEFAULT_SNAPSHOT_PATH,
    ScenarioDigest,
    Snapshot,
    UnsafeOutputRootError,
    build_snapshot,
    combine_digests,
    digest_scenario,
    digest_scenario_dir,
    hash_text,
    load_snapshot,
    save_snapshot,
    take_snapshot,
    validate_all_scenarios,
    verify_snapshot,
)

T0 = datetime(2026, 1, 1, 14, 0, tzinfo=UTC)
REPO_ROOT = Path(__file__).resolve().parents[1]
COMMITTED_SNAPSHOT = REPO_ROOT / DEFAULT_SNAPSHOT_PATH


@pytest.fixture(scope="module")
def fresh() -> Snapshot:
    """当前代码拍出来的快照（内存里，不写盘）。"""
    return build_snapshot(seed=0, base_time=T0)


# --------------------------------------------------------------------------
# 指纹
# --------------------------------------------------------------------------


def test_hash_text_is_sha256() -> None:
    assert len(hash_text("abc")) == 64
    assert hash_text("abc") == hash_text("abc")


def test_combine_digests_ignores_order() -> None:
    """文件系统的遍历顺序不稳定，合成指纹前必须先排序。"""
    first = combine_digests([("a", "1"), ("b", "2")])
    second = combine_digests([("b", "2"), ("a", "1")])
    assert first == second


def test_combine_digests_notices_a_missing_file() -> None:
    both = combine_digests([("a", "1"), ("b", "2")])
    only_one = combine_digests([("a", "1")])
    assert both != only_one


def test_same_seed_gives_the_same_digest() -> None:
    """FR-4 的核心断言：同一个种子，两次构造，字节完全一致。"""
    first = build_scenario(FaultCategory.DB_POOL_EXHAUSTED, seed=5, base_time=T0)
    second = build_scenario(FaultCategory.DB_POOL_EXHAUSTED, seed=5, base_time=T0)

    assert first.to_files() == second.to_files()
    assert digest_scenario(first).digest == digest_scenario(second).digest


def test_different_seeds_give_different_digests() -> None:
    """反过来的断言也要有 —— 否则「指纹恒定」可能是因为它压根没算数据。"""
    first = digest_scenario(build_scenario(FaultCategory.DB_POOL_EXHAUSTED, seed=1, base_time=T0))
    second = digest_scenario(build_scenario(FaultCategory.DB_POOL_EXHAUSTED, seed=2, base_time=T0))

    assert first.scenario_id == second.scenario_id, "换种子不该换场景 id"
    assert first.digest != second.digest


def test_digest_of_memory_matches_digest_of_disk(tmp_path: Path) -> None:
    """落盘的字节和算指纹的字节必须一致 —— 这是整件事成立的前提。

    如果这两条路各写一份序列化实现，迟早漂移，而指纹一旦与文件不符，
    指纹就什么也证明不了。
    """
    scenario = build_scenario(FaultCategory.MEMORY_LEAK, seed=3, base_time=T0)
    target = scenario.save(tmp_path)

    assert digest_scenario(scenario) == digest_scenario_dir(target)


def test_digest_records_every_package_file(tmp_path: Path) -> None:
    scenario = build_scenario(FaultCategory.CERT_EXPIRED, seed=0, base_time=T0)
    entry = digest_scenario_dir(scenario.save(tmp_path))

    assert entry.file_names == [
        CONFIGS_NAME,
        "deploys.jsonl",
        LOGS_NAME,
        METRICS_NAME,
        MANIFEST_NAME,
    ]
    assert entry.total_bytes > 0
    assert entry.fault_category == FaultCategory.CERT_EXPIRED


def test_digest_changes_when_one_file_changes(tmp_path: Path) -> None:
    """改一个文件的一个字节，指纹就必须变，且要指得出是哪个文件。"""
    scenario = build_scenario(FaultCategory.BAD_CONFIG_ROLLOUT, seed=0, base_time=T0)
    target = scenario.save(tmp_path)
    before = digest_scenario_dir(target)

    configs = target / CONFIGS_NAME
    configs.write_text(configs.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    after = digest_scenario_dir(target)

    assert before.digest != after.digest
    assert before.files[CONFIGS_NAME] != after.files[CONFIGS_NAME]
    assert before.files[LOGS_NAME] == after.files[LOGS_NAME], "没动的文件不该变"


def test_digest_of_a_directory_without_a_manifest_is_a_clear_error(tmp_path: Path) -> None:
    """指错了目录要报「这里没有场景包」，而不是抛一个看不懂的解析错误。"""
    with pytest.raises(FileNotFoundError, match=MANIFEST_NAME):
        digest_scenario_dir(tmp_path)


def test_cleaning_a_missing_directory_is_a_no_op(tmp_path: Path) -> None:
    """清理函数不能因为目录不存在就炸 —— 它是每条生成路径都会走的守卫。"""
    from fivewhys.snapshot import _clean_stale

    assert _clean_stale(tmp_path / "还没建过", set()) == []


def test_digest_ignores_file_mtime(tmp_path: Path) -> None:
    """重写一份内容相同的数据，指纹必须不变 —— 指纹算的是内容，不是时间。"""
    scenario = build_scenario(FaultCategory.NO_FAULT, seed=0, base_time=T0)
    target = scenario.save(tmp_path)
    before = digest_scenario_dir(target)

    logs = target / LOGS_NAME
    logs.write_text(logs.read_text(encoding="utf-8"), encoding="utf-8")

    assert digest_scenario_dir(target) == before


def test_saved_files_use_lf_on_every_platform(tmp_path: Path) -> None:
    """换行符必须是 LF。

    Windows 上 ``open("w")`` 默认把 ``\\n`` 写成 CRLF，那样同一份数据
    在两台机器上指纹不同，「可复现」就成了「在我机器上可复现」。
    """
    scenario = build_scenario(FaultCategory.DB_POOL_EXHAUSTED, seed=0, base_time=T0)
    target = scenario.save(tmp_path)

    for name in (MANIFEST_NAME, LOGS_NAME, METRICS_NAME, CONFIGS_NAME, "deploys.jsonl"):
        raw = (target / name).read_bytes()
        assert b"\r\n" not in raw, f"{name} 里出现了 CRLF"


# --------------------------------------------------------------------------
# 拍快照
# --------------------------------------------------------------------------


def test_snapshot_covers_every_registered_fault(fresh: Snapshot) -> None:
    assert {entry.fault_category for entry in fresh.scenarios} == set(available())
    assert len(fresh.scenarios) == len(available())


def test_snapshot_ids_are_sorted_and_unique(fresh: Snapshot) -> None:
    ids = [entry.scenario_id for entry in fresh.scenarios]
    assert ids == sorted(ids)
    assert len(set(ids)) == len(ids), "场景 id 必须唯一，否则评测结果会串台"


def test_take_snapshot_writes_packages_and_is_loadable(tmp_path: Path) -> None:
    snapshot, stale = take_snapshot(tmp_path, seed=0, base_time=T0)

    assert stale == []
    for entry in snapshot.scenarios:
        assert (tmp_path / entry.scenario_id / MANIFEST_NAME).exists()
        assert digest_scenario_dir(tmp_path / entry.scenario_id) == entry

    manifest = tmp_path / "snapshot.json"
    save_snapshot(snapshot, manifest)
    assert load_snapshot(manifest) == snapshot


def test_take_snapshot_is_reproducible(tmp_path: Path) -> None:
    """两次拍快照必须得到同样的指纹，连快照文件的字节都一样。

    所以快照文件里**不能有生成时间** —— 那会自己破坏自己要保证的东西。
    """
    first, _ = take_snapshot(tmp_path / "a", seed=0, base_time=T0)
    second, _ = take_snapshot(tmp_path / "b", seed=0, base_time=T0)

    assert first == second
    assert save_snapshot(first, tmp_path / "a.json").read_bytes() == (
        save_snapshot(second, tmp_path / "b.json").read_bytes()
    )


def test_take_snapshot_cleans_stale_packages(tmp_path: Path) -> None:
    """上一轮留下的场景包必须被清掉。

    否则 M6 遍历目录时会把它算进评测集 —— 「20 个场景」悄悄变成 21 个，
    而没有任何地方会报错。

    ⚠️ 这里必须造一个**真**场景包（manifest 合法且 scenario_id 与目录名一致）——
    PRE-2 之后，随手写个 ``{}`` 的目录不再算我们的包，也就不会被删。
    那条收紧是有意的：清理只该删自己认得的东西。
    """
    stale_dir = _write_package(tmp_path, "order-service-old-fault-20200101000000")

    snapshot, stale = take_snapshot(tmp_path, seed=0, base_time=T0)

    assert [path.name for path in stale] == [stale_dir.name]
    assert not stale_dir.exists()
    assert len(snapshot.scenarios) == len(available())


def test_take_snapshot_keeps_foreign_directories(tmp_path: Path) -> None:
    """目录里如果放着别的东西，宁可留着也不动 —— 清理脚本误删是最糟的失败。"""
    keep = tmp_path / "notes"
    keep.mkdir()
    (keep / "README.md").write_text("别删我", encoding="utf-8")

    take_snapshot(tmp_path, seed=0, base_time=T0)

    assert (keep / "README.md").read_text(encoding="utf-8") == "别删我"


def test_take_snapshot_can_keep_stale_packages(tmp_path: Path) -> None:
    stale_dir = tmp_path / "order-service-old-fault-20200101000000"
    stale_dir.mkdir()
    (stale_dir / MANIFEST_NAME).write_text("{}", encoding="utf-8")

    _, stale = take_snapshot(tmp_path, seed=0, base_time=T0, clean_stale=False)

    assert stale == []
    assert stale_dir.exists()


# --------------------------------------------------------------------------
# ⭐ 清理是唯一会删东西的地方 —— 上线前加固（PRE-2）
#
# 三条纪律：只删我们认得的场景包 / 不跟随符号链接 / 整目录一起删。
# 下面每个测试都对应一条实测过的失效。
# --------------------------------------------------------------------------


def _write_package(root: Path, name: str, *, scenario_id: str | None = None) -> Path:
    """手搓一个最小场景包目录（只写清理逻辑需要的那份文件）。"""
    package = root / name
    package.mkdir(parents=True, exist_ok=True)
    (package / MANIFEST_NAME).write_text(
        ScenarioManifest(
            scenario_id=scenario_id or name,
            question="q",
            ground_truth=GroundTruth(
                scenario_id=scenario_id or name,
                fault_category=FaultCategory.NO_FAULT,
                root_cause_service="order-service",
                root_cause="无故障",
                injected_at=T0,
                symptoms=[],
                match_keywords=[],
            ),
        ).model_dump_json(),
        encoding="utf-8",
    )
    return package


def test_cleaning_handles_a_nested_directory(tmp_path: Path) -> None:
    """⭐ 陈旧场景包里有子目录时，必须整目录删干净，不能「删一半就崩」。

    原实现是一个个 ``unlink()`` 再 ``rmdir()``：碰到子目录会抛
    PermissionError（Windows），**留下一半被删的目录** —— 实测确认过。
    """
    package = _write_package(tmp_path, "order-service-ancient-20200101000000")
    (package / "nested").mkdir()
    (package / "nested" / "keep.txt").write_text("x", encoding="utf-8")

    _, stale = take_snapshot(tmp_path, seed=0, base_time=T0)

    assert package in stale
    assert not package.exists(), "目录还在 —— 说明只删了一半"


def test_cleaning_skips_directories_that_are_not_our_packages(tmp_path: Path) -> None:
    """别人的 ``scenario.json`` 不许删。

    只看「有没有 scenario.json」是不够的 —— 目录名和里面的 ``scenario_id``
    对不上，就说明这不是我们生成的场景包。
    """
    other = _write_package(tmp_path, "someone-elses-thing", scenario_id="totally-different")
    broken = tmp_path / "broken-manifest"
    broken.mkdir()
    (broken / MANIFEST_NAME).write_text("不是 JSON", encoding="utf-8")

    take_snapshot(tmp_path, seed=0, base_time=T0)

    assert other.exists(), "删掉了别人的目录"
    assert broken.exists(), "删掉了 manifest 解析失败的目录"


def test_cleaning_does_not_follow_symlinks(tmp_path: Path) -> None:
    """符号链接一律跳过 —— 它指向的可能是目录树外面的东西。"""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious.txt").write_text("别删我", encoding="utf-8")

    root = tmp_path / "root"
    root.mkdir()
    link = root / "linked-package"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("这个环境不允许创建符号链接（Windows 需要开发者模式）")

    take_snapshot(root, seed=0, base_time=T0)

    assert (outside / "precious.txt").exists(), "顺着符号链接把外面的东西删了"


def test_cleaning_does_not_follow_junctions(tmp_path: Path) -> None:
    """Windows 上 junction 也要防 —— 而且它**不需要管理员权限**。

    ``Path.is_symlink()`` 对 junction 返回 **False**，
    所以只防符号链接等于在 Windows 上没防。
    """
    if not hasattr(Path, "is_junction"):
        pytest.skip("这个 Python 版本没有 Path.is_junction")

    outside = _write_package(tmp_path, "outside-package")
    (outside / "precious.txt").write_text("别删我", encoding="utf-8")

    root = tmp_path / "root"
    root.mkdir()
    link = root / outside.name
    created = subprocess.run(  # noqa: S603 —— 参数是我们自己拼的
        ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
        capture_output=True,
        text=True,
        check=False,
    )
    if created.returncode != 0:
        pytest.skip(f"这个环境建不了 junction：{created.stderr or created.stdout}")

    take_snapshot(root, seed=0, base_time=T0)

    assert (outside / "precious.txt").exists(), "顺着 junction 把外面的东西删了"


def test_refuses_to_clean_a_source_tree(tmp_path: Path) -> None:
    """``--out .`` 指到源码根时必须**拒绝**，而且一个文件都不许写。

    顺序很重要：安全检查要跑在**写场景包之前**，
    否则拒绝的时候场景包已经写进那个不该动的目录了。
    """
    (tmp_path / "pyproject.toml").write_text("[project]", encoding="utf-8")
    package = _write_package(tmp_path, "order-service-ancient-20200101000000")

    with pytest.raises(UnsafeOutputRootError, match="源码目录"):
        take_snapshot(tmp_path, seed=0, base_time=T0)

    assert package.exists(), "拒绝之后还是删了东西 —— 检查顺序错了"
    assert not list(tmp_path.glob("order-service-healthy-*")), "拒绝之前不该写任何场景包"


def test_refuses_a_drive_root() -> None:
    """盘根目录同理。这里只测判定函数 —— 真去删盘根是不可能测的。"""
    from fivewhys.snapshot import _assert_safe_to_clean

    with pytest.raises(UnsafeOutputRootError, match="盘根"):
        _assert_safe_to_clean(Path(Path.cwd().anchor))


def test_keep_stale_opts_out_of_the_safety_check(tmp_path: Path) -> None:
    """用户明确说「别清理」时，仍然可以在任何地方生成场景（只是不删东西）。"""
    (tmp_path / "pyproject.toml").write_text("[project]", encoding="utf-8")
    package = _write_package(tmp_path, "order-service-ancient-20200101000000")

    snapshot, stale = take_snapshot(tmp_path, seed=0, base_time=T0, clean_stale=False)

    assert snapshot.scenarios, "应该照常生成场景"
    assert stale == []
    assert package.exists()


# --------------------------------------------------------------------------
# 存 / 读
# --------------------------------------------------------------------------


def test_round_trip_keeps_base_time_timezone(fresh: Snapshot, tmp_path: Path) -> None:
    """base_time 必须原样读回来。

    丢一个时区偏移，重造出来的日志时间戳就会变，指纹跟着全变 ——
    校验会报「数据变了」，而真正的原因在读文件这一步。
    """
    path = save_snapshot(fresh, tmp_path / "snapshot.json")
    restored = load_snapshot(path)

    assert restored.base_time == fresh.base_time
    assert restored.base_time.tzinfo is not None
    assert restored.digest == fresh.digest


def test_snapshot_digest_changes_with_seed(fresh: Snapshot) -> None:
    other = build_snapshot(seed=1, base_time=T0)
    assert other.seed == 1
    assert other.digest != fresh.digest
    assert other.short_digest == other.digest[:12]


def test_load_missing_snapshot_says_what_to_do(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="fivewhys snapshot"):
        load_snapshot(tmp_path / "nope.json")


def test_manifest_is_plain_json_without_timestamp(tmp_path: Path) -> None:
    """快照文件里不能有「生成时间」这类每次都变的东西。"""
    import json

    snapshot = build_snapshot(seed=0, base_time=T0)
    path = save_snapshot(snapshot, tmp_path / "snapshot.json")
    raw = json.loads(path.read_text(encoding="utf-8"))

    assert raw["version"] >= 1
    assert raw["seed"] == 0
    assert not {"generated_at", "created_at"} & set(raw)


# --------------------------------------------------------------------------
# 校验：⭐ 关键在「能抓到问题」
# --------------------------------------------------------------------------


def test_verify_passes_for_a_fresh_snapshot(fresh: Snapshot) -> None:
    assert verify_snapshot(fresh) == []


def test_verify_catches_a_new_fault_missing_from_the_snapshot(fresh: Snapshot) -> None:
    """新增了故障但没重拍快照 —— 评测集的范围变了。"""
    shrunk = fresh.model_copy(update={"scenarios": fresh.scenarios[1:]})
    problems = verify_snapshot(shrunk)

    assert any("快照里没有故障" in problem for problem in problems), problems


def test_verify_catches_a_fault_that_no_longer_exists(fresh: Snapshot) -> None:
    """快照里有、代码里没有 —— 评测集缩小了，也要报。"""
    ghost = ScenarioDigest(
        scenario_id="order-service-slow-query-20260101140200",
        fault_category=FaultCategory.SLOW_QUERY,
        digest="0" * 64,
    )
    bloated = fresh.model_copy(update={"scenarios": [*fresh.scenarios, ghost]})
    problems = verify_snapshot(bloated)

    assert any("代码里已经没有了" in problem for problem in problems), problems


def _tamper(fresh: Snapshot, index: int, **updates: object) -> Snapshot:
    scenarios = list(fresh.scenarios)
    scenarios[index] = scenarios[index].model_copy(update=updates)
    return fresh.model_copy(update={"scenarios": scenarios})


def test_verify_catches_changed_data_and_names_the_file(fresh: Snapshot) -> None:
    """注入器改了 → 指纹变 → 要指出是哪份文件变了。

    这里把「逐文件指纹」和「总指纹」一起改掉，模拟一次**自洽的**数据漂移，
    这样才能证明校验真的比对了重建结果，而不是只发现自己文件被改。
    """
    entry = fresh.scenarios[0]
    files = {**entry.files, LOGS_NAME: "f" * 64}
    drifted = _tamper(
        fresh,
        0,
        files=files,
        digest=combine_digests(files.items()),
    )

    problems = verify_snapshot(drifted)

    assert any("logs.jsonl 内容变了" in problem for problem in problems), problems
    assert any(entry.scenario_id in problem for problem in problems), problems


def test_verify_catches_a_tampered_snapshot_file(fresh: Snapshot) -> None:
    """快照文件自己被人改过（逐文件指纹与总指纹对不上）必须被发现。

    这是 FIV-12 手工篡改一份副本时找到的洞：只改 ``files`` 而不改 ``digest``，
    比对时总指纹仍然相等 —— 校验会**通过**，但从此报错会指错地方。
    """
    tampered = _tamper(fresh, 0, files={**fresh.scenarios[0].files, LOGS_NAME: "f" * 64})
    problems = verify_snapshot(tampered)

    assert any("快照文件自身不一致" in problem for problem in problems), problems


def test_verify_catches_a_renamed_scenario(fresh: Snapshot) -> None:
    """场景 id 变了 → 同一个故障的历史评测结果没法对齐。"""
    renamed = _tamper(fresh, 0, scenario_id="order-service-renamed-20260101140200")
    problems = verify_snapshot(renamed)

    assert any("不在快照里" in problem for problem in problems), problems


def test_verify_catches_an_unusable_scenario(
    monkeypatch: pytest.MonkeyPatch, fresh: Snapshot
) -> None:
    """场景数据不合法（这里：日志里泄漏了答案词）也要算校验失败。

    「能生成」不等于「能用」—— 泄漏了答案的场景照样能生成，
    但拿它评测等于送分。
    """
    scenario = build_scenario(FaultCategory.DB_POOL_EXHAUSTED, seed=0, base_time=T0)
    logs = LogStore()
    service = MockService("order-service", logs, seed=0)
    service.emit(LogLevel.ERROR, "database pool exhausted", scenario.ground_truth.injected_at)
    broken = replace(scenario, logs=logs)

    monkeypatch.setattr(
        "fivewhys.snapshot.build_all_scenarios",
        lambda **_kwargs: [broken],
    )

    problems = verify_snapshot(fresh)
    assert any("泄漏了答案词" in problem for problem in problems), problems


# --------------------------------------------------------------------------
# 场景本身可用
# --------------------------------------------------------------------------


def test_all_registered_scenarios_are_usable() -> None:
    assert validate_all_scenarios(seed=0, base_time=T0) == []


def test_every_scenario_has_a_question_and_an_answer() -> None:
    for scenario in build_all_scenarios(seed=0, base_time=T0):
        assert scenario.question
        assert scenario.ground_truth.root_cause_service
        assert scenario.validate() == [], scenario.scenario_id


# --------------------------------------------------------------------------
# ⭐ 提交在版本库里的那份指纹
# --------------------------------------------------------------------------


def test_committed_snapshot_matches_current_code() -> None:
    """评测集是**不可变资产**：代码改了场景数据，就必须显式重拍快照。

    这个测试是那道「必须先承认我改了评测集」的闸门。
    它红了不代表代码写错了 —— 代表 M7 的改进曲线从此不可比，
    要先确认这个改动是不是有意的。
    """
    if not COMMITTED_SNAPSHOT.exists():
        pytest.skip(f"还没有提交过快照：{DEFAULT_SNAPSHOT_PATH}")

    problems = verify_snapshot(load_snapshot(COMMITTED_SNAPSHOT))
    assert problems == [], (
        "评测集与提交的快照不一致。\n"
        + "\n".join(f"  - {problem}" for problem in problems)
        + "\n若改动是有意的，重拍快照：python scripts/snapshot_scenarios.py"
    )


def test_committed_snapshot_is_self_consistent() -> None:
    """快照文件里的指纹必须能自己加回自己 —— 防止手改出一份假的「一致」。"""
    if not COMMITTED_SNAPSHOT.exists():
        pytest.skip(f"还没有提交过快照：{DEFAULT_SNAPSHOT_PATH}")

    snapshot = load_snapshot(COMMITTED_SNAPSHOT)
    assert snapshot.scenarios, "快照里一个场景都没有，那它什么也没保证"
    for entry in snapshot.scenarios:
        assert combine_digests(entry.files.items()) == entry.digest, entry.scenario_id


def test_build_snapshot_does_not_touch_the_disk() -> None:
    """``build_snapshot`` 是纯计算 —— 校验路径不能有写磁盘的副作用。"""
    before = sorted(Path("data/scenarios").glob("*")) if Path("data/scenarios").exists() else []
    build_snapshot(seed=0, base_time=T0)
    after = sorted(Path("data/scenarios").glob("*")) if Path("data/scenarios").exists() else []
    assert before == after


def test_scenario_files_are_the_only_source_of_the_digest() -> None:
    """``to_files()`` 覆盖场景包的全部文件 —— 多一个文件就等于指纹有盲区。"""
    scenario: Scenario = build_scenario(FaultCategory.DEPENDENCY_5XX, seed=0, base_time=T0)
    assert set(scenario.to_files()) == {
        MANIFEST_NAME,
        LOGS_NAME,
        METRICS_NAME,
        CONFIGS_NAME,
        "deploys.jsonl",
    }
