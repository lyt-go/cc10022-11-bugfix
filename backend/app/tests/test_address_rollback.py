"""地址版本回退、并发更正与地址读取入口的回归测试。

覆盖场景：
1. 页面执行回退后，历史版本正确显示为当前，黄金记录恢复目标版本地址；
2. 回退写入可查询的“回退字段数据”，重放只生效一次；
3. 街道与区县作为完整地址成组回退，不残留回退前的另一半；
4. 同一客户两笔操作争用当前版本，每个确认版本只关联自己的字段组合；
5. 历史摘要证据不齐时，存量确认结果仍可查询；
6. 地址读取入口只消费已发布版本的生效字段组合，供客服改派。
"""
from __future__ import annotations

import os
import sys
import threading

_backend_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _backend_dir not in sys.path:
    sys.path.insert(0, _backend_dir)

from app.services.golden_record import GoldenRecordService
from app.services.mdm_api import MdmApiService
from app.services.mdm_version import MdmVersionService


GOLDEN_ID = "GR-CUST-0001"


def _fresh_id(suffix: str = "FLOW") -> str:
    """每个用例用独立黄金记录，避免共享内存仓库互相污染。"""
    svc = GoldenRecordService()
    created = svc.create_golden_record(
        "customer",
        {
            "姓名": "测试客户",
            "电话": "13800000000",
            "街道": "起始街道1号",
            "区县": "起始区",
            "representative": {"姓名": "测试客户"},
        },
        ["SRC-TEST-001"],
    )
    golden_id = created["golden_record"]["黄金ID"]
    version_svc = MdmVersionService()
    version_svc.snapshot_golden_version(
        golden_id,
        {"姓名": "测试客户", "电话": "13800000000",
         "街道": "起始街道1号", "区县": "起始区"},
        f"BATCH-{suffix}",
    )
    return golden_id


def test_rollback_restores_fields_and_switches_status():
    """回退后历史版本显示为当前，黄金记录恢复目标版本地址。"""
    golden_id = _fresh_id("RB1")
    version_svc = MdmVersionService()
    golden_svc = GoldenRecordService()

    second = version_svc.snapshot_golden_version(
        golden_id, {"街道": "新街道2号", "区县": "新区"}, "BATCH-2")
    assert second["ok"], second
    second_version = second["version"]["版本号"]

    result = version_svc.rollback_to_version(golden_id, "1.0")
    assert result["ok"], result
    assert result["restored_to"] == "1.0"

    golden = golden_svc.query_golden_record(golden_id)
    assert golden["街道"] == "起始街道1号", golden
    assert golden["区县"] == "起始区", golden
    assert golden["当前版本号"] == "1.0"

    history = {v["版本号"]: v for v in version_svc.query_version_history(golden_id)}
    assert history["1.0"]["status"] == "当前版本"
    assert history[second_version]["status"] == "已回滚"

    # 回退字段数据可查询，且同时落在黄金记录和目标版本上。
    fields = golden["回退字段数据"]
    assert fields["恢复地址"] == {"街道": "起始街道1号", "区县": "起始区"}
    assert fields["回退前字段"]["街道"] == "新街道2号"
    assert history["1.0"]["回退字段数据"]["恢复字段"]["区县"] == "起始区"


def test_rollback_replay_is_idempotent():
    """同一回退重放只生效一次，版本状态不被重复改写。"""
    golden_id = _fresh_id("RB2")
    version_svc = MdmVersionService()
    version_svc.snapshot_golden_version(golden_id, {"街道": "第二街道"}, "BATCH-2")

    first = version_svc.rollback_to_version(golden_id, "1.0")
    assert first["applied"] is True
    replay = version_svc.rollback_to_version(golden_id, "1.0")
    assert replay["applied"] is False and replay["replayed"] is True

    history = version_svc.query_version_history(golden_id)
    current = [v for v in history if v["status"] == "当前版本"]
    rolled = [v for v in history if v["status"] == "已回滚"]
    assert len(current) == 1 and len(rolled) == 1, history


def test_rollback_restores_address_as_a_group():
    """街道与区县作为完整地址回退，不残留回退前写入的另一半。"""
    golden_id = _fresh_id("RB3")
    version_svc = MdmVersionService()
    golden_svc = GoldenRecordService()

    # 版本 2 只改区县，版本 3 只改街道；回退到 2 时必须同时恢复 2 的街道。
    version_svc.snapshot_golden_version(golden_id, {"区县": "改写区"}, "BATCH-2")
    version_svc.snapshot_golden_version(golden_id, {"街道": "第三街道"}, "BATCH-3")

    result = version_svc.rollback_to_version(golden_id, "2.0")
    assert result["ok"], result
    golden = golden_svc.query_golden_record(golden_id)
    assert golden["街道"] == "起始街道1号", golden
    assert golden["区县"] == "改写区", golden
    effective = golden["生效字段组合"]
    assert effective["街道"] == "起始街道1号", effective
    assert effective["区县"] == "改写区", effective


def test_concurrent_corrections_keep_their_own_field_combination():
    """撤回区县与新街道两笔申请并发确认，各自版本字段不串。

    确认顺序由线程调度决定；不预设谁最后确认，只校验不变量：
    每个版本保留自己提交时的那套字段，且“当前版本”的字段组合与
    黄金记录生效组合完全一致，不会出现半套地址。
    """
    golden_id = _fresh_id("CONC")
    version_svc = MdmVersionService()
    golden_svc = GoldenRecordService()

    # 两笔申请在提交时各自抓取基准组合，再交错确认。
    base_a = golden_svc.effective_combination(golden_id)
    base_b = golden_svc.effective_combination(golden_id)
    gate = threading.Barrier(2)
    results: dict[str, dict] = {}

    def submit(name: str, fields: dict, base: dict) -> None:
        gate.wait()
        results[name] = version_svc.snapshot_golden_version(
            golden_id, fields, f"BATCH-{name}", base_combination=base)

    retract = threading.Thread(
        target=submit, args=("A", {"区县": ""}, base_a))
    street = threading.Thread(
        target=submit, args=("B", {"街道": "并发新街道"}, base_b))
    retract.start(); street.start(); retract.join(); street.join()

    version_a = results["A"]["version"]
    version_b = results["B"]["version"]

    # 每笔申请只关联自己提交的字段：撤回的那笔不改街道，新街道那笔不撤区县。
    assert version_a["字段快照"]["区县"] == ""
    assert version_a["字段快照"]["街道"] == "起始街道1号"
    assert version_b["字段快照"]["街道"] == "并发新街道"
    assert version_b["字段快照"]["区县"] == "起始区"
    assert version_a["版本编号"] != version_b["版本编号"]

    # 恰好一个当前版本，其余为历史版本；历史版本的字段组合不被回写。
    history = {v["版本编号"]: v for v in version_svc.query_version_history(golden_id)}
    current = [v for v in history.values() if v["status"] == "当前版本"]
    assert len(current) == 1, history
    current_version = current[0]
    own = (
        version_a["字段快照"] if current_version["版本编号"] == version_a["版本编号"]
        else version_b["字段快照"]
    )
    # 当前版本的字段组合就是黄金记录生效组合，两处对得上。
    effective = golden_svc.effective_combination(golden_id)
    for key in ("姓名", "电话", "街道", "区县"):
        assert effective[key] == own[key], (effective, own)
    assert history[version_a["版本编号"]]["字段快照"] == version_a["字段快照"]
    assert history[version_b["版本编号"]]["字段快照"] == version_b["字段快照"]


def test_legacy_versions_stay_queryable_without_evidence():
    """历史摘要证据不齐时，存量确认结果仍可继续查询。"""
    version_svc = MdmVersionService()
    confirmed = version_svc.query_confirmed_versions("GR-CUST-0002")
    by_number = {v["版本号"]: v for v in confirmed}
    assert "0.9" in by_number, confirmed
    legacy = by_number["0.9"]
    assert legacy["字段证据完整"] is False
    assert legacy["status"] == "历史版本"

    detail = version_svc.get_version_fields("GR-CUST-0002", "0.9")
    assert detail["ok"] is True
    assert detail["字段证据完整"] is False

    blocked = version_svc.rollback_to_version("GR-CUST-0002", "0.9")
    assert blocked["ok"] is False
    assert "证据" in blocked["reason"]


def test_address_entry_serves_only_effective_combination():
    """地址读取入口消费已发布组合，整套地址缺失时不可用于改派。"""
    golden_id = _fresh_id("ADDR")
    version_svc = MdmVersionService()
    api_svc = MdmApiService()

    ready = api_svc.query_customer_address(golden_id)
    assert ready["ok"] and ready["ready_for_dispatch"] is True
    assert ready["address"] == {"街道": "起始街道1号", "区县": "起始区"}

    version_svc.snapshot_golden_version(golden_id, {"区县": ""}, "BATCH-RETRACT")
    after = api_svc.query_customer_address(golden_id)
    assert after["address_complete"] is False
    assert after["ready_for_dispatch"] is False
    assert after["missing_roles"] == ["区县"]
    assert after["version_evidence"]["字段证据完整"] is True


def test_interleaved_corrections_and_rollbacks():
    """更正与回退交错提交：逐笔核对字段与版本状态。"""
    golden_id = _fresh_id("MIX")
    version_svc = MdmVersionService()
    golden_svc = GoldenRecordService()

    v2 = version_svc.snapshot_golden_version(
        golden_id, {"街道": "更正街道2号"}, "BATCH-V2")["version"]
    v3 = version_svc.snapshot_golden_version(
        golden_id, {"区县": "更正区3"}, "BATCH-V3")["version"]
    assert v2["字段快照"] == {
        "姓名": "测试客户", "电话": "13800000000",
        "街道": "更正街道2号", "区县": "起始区",
    }, v2
    assert v3["字段快照"]["街道"] == "更正街道2号"
    assert v3["字段快照"]["区县"] == "更正区3"

    first_rollback = version_svc.rollback_to_version(golden_id, "1.0")
    assert first_rollback["ok"] and first_rollback["applied"]
    history = {v["版本编号"]: v for v in version_svc.query_version_history(golden_id)}
    assert history["VER-%s-1.0" % golden_id]["status"] == "当前版本"
    assert history[v3["版本编号"]]["status"] == "已回滚"
    assert history[v2["版本编号"]]["status"] == "历史版本"
    assert history[v2["版本编号"]]["字段快照"] == v2["字段快照"]
    assert golden_svc.effective_combination(golden_id)["街道"] == "起始街道1号"

    # 回退之后再提交一笔新街道更正：新版本以回退后的字段为基准。
    v4 = version_svc.snapshot_golden_version(
        golden_id, {"街道": "回退后街道4号"}, "BATCH-V4")["version"]
    assert v4["字段快照"]["街道"] == "回退后街道4号"
    assert v4["字段快照"]["区县"] == "起始区", v4
    assert golden_svc.query_golden_record(golden_id)["当前版本号"] == v4["版本号"]

    # 再回退到中间版本 v2：街道与区县必须整套恢复成 v2 当时的样子。
    second_rollback = version_svc.rollback_to_version(golden_id, v2["版本号"])
    assert second_rollback["ok"] and second_rollback["applied"]
    golden = golden_svc.query_golden_record(golden_id)
    assert golden["街道"] == "更正街道2号", golden
    assert golden["区县"] == "起始区", golden

    history = {v["版本编号"]: v for v in version_svc.query_version_history(golden_id)}
    assert history[v2["版本编号"]]["status"] == "当前版本"
    assert history[v4["版本编号"]]["status"] == "已回滚"
    assert history["VER-%s-1.0" % golden_id]["status"] == "历史版本"
    assert history[v3["版本编号"]]["status"] == "已回滚"
    # 每笔确认版本仍保留自己的字段组合，没有被后续回退改写。
    assert history[v2["版本编号"]]["字段快照"] == v2["字段快照"]
    assert history[v3["版本编号"]]["字段快照"] == v3["字段快照"]
    assert history[v4["版本编号"]]["字段快照"] == v4["字段快照"]


def test_api_query_returns_only_complete_addresses_and_follows_publish():
    """查询接口按生效组合输出；回退后立即读到恢复后的地址。"""
    version_svc = MdmVersionService()
    api_svc = MdmApiService()

    before = api_svc.query_customer_address(GOLDEN_ID)
    version_svc.snapshot_golden_version(
        GOLDEN_ID, {"街道": "临时街道99号"}, "BATCH-TMP")
    during = api_svc.query_customer_address(GOLDEN_ID)
    assert during["街道"] == "临时街道99号"

    version_svc.rollback_to_version(GOLDEN_ID, before["current_version"])
    after = api_svc.query_customer_address(GOLDEN_ID)
    assert after["address"] == before["address"]

    listed = api_svc.query_golden_records_by_api(1)
    assert listed["ok"] is True
    assert all(r["地址完整"] is True for r in listed["records"]), listed
    matched = [r for r in listed["records"] if r["黄金ID"] == GOLDEN_ID]
    assert matched and matched[0]["街道"] == before["街道"]


if __name__ == "__main__":
    tests = [
        ("回退恢复字段并切换版本状态", test_rollback_restores_fields_and_switches_status),
        ("回退重放只生效一次", test_rollback_replay_is_idempotent),
        ("街道区县成组回退", test_rollback_restores_address_as_a_group),
        ("并发更正各自保留字段组合", test_concurrent_corrections_keep_their_own_field_combination),
        ("存量版本证据不齐仍可查询", test_legacy_versions_stay_queryable_without_evidence),
        ("地址读取入口只消费生效组合", test_address_entry_serves_only_effective_combination),
        ("更正与回退交错提交", test_interleaved_corrections_and_rollbacks),
        ("查询接口跟随版本发布", test_api_query_returns_only_complete_addresses_and_follows_publish),
    ]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS: {name}")
        except Exception as exc:  # noqa: BLE001 - 直接打印断言原因
            failed += 1
            print(f"  FAIL: {name} -> {exc!r}")
    print(f"\n结果: {len(tests) - failed}/{len(tests)} 通过, {failed} 失败")
    assert failed == 0, f"{failed} 条地址回退链路测试失败！"
