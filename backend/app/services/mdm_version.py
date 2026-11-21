"""版本管理领域服务：黄金记录版本快照、变更对比与版本回退。

关键约束：
- 街道与区县作为一套完整地址一起确认、一起回退；
- 每个版本在确认时固化自己的完整字段组合，后续变更不得回写旧版本；
- 回退不只是改状态，必须把目标历史版本的字段组合写回黄金记录，
  并把可查询的回退字段数据同时写到黄金记录和目标版本上；
- 同一黄金记录的确认与回退共用一把锁，并发申请不会互相串字段；
- 存量版本证据不齐时仍可查询，不会因为解析失败而丢记录。
"""
from __future__ import annotations

import copy
import datetime
from typing import Any, Mapping

from app.services.address_group import (
    address_complete,
    address_roles_present,
    business_snapshot,
    extract_address,
    full_snapshot_from_version,
    golden_lock,
    has_address_evidence,
    merge_address_group,
    parse_version,
    values_equal,
    version_evidence_source,
    versions_equal,
    version_sort_key,
)
from app.services.base import BaseModuleService
from app.store import store


def _now() -> str:
    return datetime.datetime.utcnow().isoformat()


class MdmVersionService(BaseModuleService):
    MODULE = "mdm_version"
    ENTITY = "版本记录"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["版本编号", "黄金ID", "版本号", "变更字段", "旧值摘要", "新值摘要", "变更批次", "版本时间"]
    REQUIRED_FIELDS = ["版本编号", "黄金ID", "版本号"]
    STATUS_ORDER = ["草稿", "当前版本", "历史版本", "已回滚"]
    ACTION_RULES = {"设为当前": "当前版本", "标记历史": "历史版本", "执行回滚": "已回滚"}
    NEGATIVE_ACTIONS = []

    # ── 版本快照 ──────────────────────────────────────────────

    def snapshot_golden_version(self, golden_id: str,
                                  record_data: dict[str, Any],
                                  batch: str | None = None,
                                  *, base_combination: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """存储黄金记录版本快照，并把该版本发布为当前版本。

        在进入锁之前先取本次申请的基准字段组合（并发提交时各自持有一份
        基准），锁内只做版本号分配、快照固化和字段发布。每个确认版本都
        固化自己那一份街道/区县组合，之后任何操作都不会回写旧版本。
        """
        from app.services.golden_record import GoldenRecordService

        golden_svc = GoldenRecordService()
        # 基准组合在提交时抓取：并发申请各自读自己的起点，互不串字段。
        base = (
            copy.deepcopy(dict(base_combination))
            if base_combination is not None
            else golden_svc.effective_combination(golden_id)
        )
        incoming = business_snapshot(record_data)
        if not incoming and not base:
            return {"ok": False, "reason": "版本快照缺少字段数据"}

        # 地址字段成组合并：本次只改街道时，区县取本申请开始前的值；
        # 本次只改区县或撤回区县时同理。合并结果作为本版本的完整组合，
        # 入参若用了 street/district 别名，会收敛到记录实际使用的键名。
        if has_address_evidence(base) or has_address_evidence(incoming):
            combined = {**base, **merge_address_group(base, incoming)}
        else:
            combined = {**base, **incoming}

        changed_fields: list[str] = []
        old_summary: dict[str, Any] = {}
        new_summary: dict[str, Any] = {}
        for key, new_val in combined.items():
            old_val = base.get(key, "")
            if not values_equal(old_val, new_val):
                changed_fields.append(key)
                old_summary[key] = copy.deepcopy(old_val)
                new_summary[key] = copy.deepcopy(new_val)

        group = extract_address(combined)
        with golden_lock(golden_id):
            version_num = self._next_version(golden_id)
            entry = {
                "id": self._next_id(),
                "版本编号": f"VER-{golden_id}-{version_num}",
                "黄金ID": golden_id,
                "版本号": version_num,
                "变更字段": ",".join(changed_fields),
                "确认字段": ",".join(changed_fields),
                "旧值摘要": str(old_summary) if old_summary else "",
                "新值摘要": str(new_summary) if new_summary else "",
                "旧值快照": copy.deepcopy(base),
                "新值快照": copy.deepcopy(combined),
                # 完整字段组合：每个已确认版本只关联自己这一份。
                "字段快照": copy.deepcopy(combined),
                "字段组合": copy.deepcopy(combined),
                "地址组合": group,
                "地址完整": address_complete(combined),
                "地址角色": address_roles_present(combined),
                "变更批次": batch or golden_id,
                "版本时间": _now(),
                "已确认": True,
                "确认时间": _now(),
                "status": "当前版本",
                "pending": False,
                "abnormal": False,
            }
            self._set_previous_history(golden_id)
            store.rows(self.MODULE).append(entry)
            golden_svc.apply_effective_combination(
                golden_id, combined, version_num, entry["版本编号"],
                version_time=entry["版本时间"],
            )
        return {
            "ok": True,
            "version": entry,
            "changed_fields": changed_fields,
            "address": group,
            "address_complete": address_complete(combined),
        }

    def submit_address_correction(self, golden_id: str,
                                    fields: dict[str, Any],
                                    batch: str | None = None) -> dict[str, Any]:
        """提交一笔地址更正申请并确认成版本。

        只传街道或只传区县都可以；未传的另一项沿用本申请开始前的值，
        撤回错填项时显式传空字符串。每笔申请都生成独立版本，后续申请
        不会改写已确认版本的字段组合。
        """
        return self.snapshot_golden_version(golden_id, fields, batch)

    def list_entries(self, *, filters: dict[str, str] | None = None,
                     keyword: str | None = None, status: str | None = None,
                     page: int = 1, size: int = 20) -> tuple[list[dict[str, Any]], int]:
        """版本列表附带字段证据标注，页面能直接看出哪个版本可回退。"""
        items, total = super().list_entries(
            filters=filters, keyword=keyword, status=status, page=page, size=size)
        return [self._describe_version(row) for row in items], total

    def _next_id(self) -> int:
        rows = store.rows(self.MODULE)
        return max((int(r.get("id", 0)) for r in rows), default=0) + 1

    def _next_version(self, golden_id: str) -> str:
        """计算下一个版本号；存量非数字版本号会被跳过而不是抛异常。"""
        rows = store.rows(self.MODULE)
        versions = [
            parse_version(r.get("版本号"))
            for r in rows if r.get("黄金ID") == golden_id
        ]
        known = [v for v in versions if v is not None]
        return str(max(known, default=0.0) + 1.0)

    def _set_previous_history(self, golden_id: str) -> None:
        """将之前的当前版本标记为历史版本（不动已回滚版本）。"""
        rows = store.rows(self.MODULE)
        for row in rows:
            if row.get("黄金ID") == golden_id and row.get("status") == "当前版本":
                row["status"] = "历史版本"
                row["pending"] = False

    # ── 查询 ────────────────────────────────────────────────

    def _find_version(self, golden_id: str, version_number: Any) -> dict[str, Any] | None:
        """按版本号或版本编号定位版本，兼容 1 / 1.0 / "1.0" 三种写法。"""
        rows = store.rows(self.MODULE)
        for row in rows:
            if row.get("黄金ID") != golden_id:
                continue
            if str(row.get("版本编号")) == str(version_number):
                return row
            if versions_equal(row.get("版本号"), version_number):
                return row
        return None

    def query_version_history(self, golden_id: str) -> list[dict[str, Any]]:
        """查询版本历史：获取黄金记录的所有版本变更记录。

        按版本号降序返回。每条记录附带字段证据来源与完整性标注；
        存量数据里摘要缺失或不是字典时仍照常返回，保证确认结果可继续查询。
        """
        rows = store.rows(self.MODULE)
        history = [row for row in rows if row.get("黄金ID") == golden_id]
        history.sort(key=version_sort_key, reverse=True)
        return [self._describe_version(row) for row in history]

    def _describe_version(self, row: Mapping[str, Any]) -> dict[str, Any]:
        described = copy.deepcopy(dict(row))
        source = version_evidence_source(row)
        snapshot = full_snapshot_from_version(row)
        described["字段证据来源"] = source
        described["字段证据完整"] = snapshot is not None
        described["可回退"] = snapshot is not None
        if snapshot is not None:
            described["完整字段组合"] = snapshot
            described["地址组合"] = extract_address(snapshot)
        return described

    def query_confirmed_versions(self, golden_id: str) -> list[dict[str, Any]]:
        """查询存量确认结果：已确认过的版本都可持续查询。

        只过滤真正确认过的版本（当前/历史/已回滚，或显式已确认），
        不要求字段证据完整；证据不齐的记录会带“字段证据完整=False”，
        但版本编号、时间、摘要等已确认信息照常返回。
        """
        confirmed_status = {"当前版本", "历史版本", "已回滚"}
        rows = store.rows(self.MODULE)
        results = [
            self._describe_version(row)
            for row in rows
            if row.get("黄金ID") == golden_id
            and (row.get("status") in confirmed_status or row.get("已确认") is True)
        ]
        results.sort(key=lambda r: version_sort_key(r), reverse=True)
        return results

    def get_version_fields(self, golden_id: str,
                           version_number: Any) -> dict[str, Any]:
        """读取某个版本的完整字段组合；证据不齐时给出可查询的说明。"""
        target = self._find_version(golden_id, version_number)
        if not target:
            return {"ok": False, "reason": "目标版本不存在"}
        snapshot = full_snapshot_from_version(target)
        return {
            "ok": True,
            "golden_id": golden_id,
            "版本号": target.get("版本号"),
            "版本编号": target.get("版本编号"),
            "status": target.get("status"),
            "字段证据来源": version_evidence_source(target),
            "字段证据完整": snapshot is not None,
            "字段组合": snapshot or {},
            "地址组合": extract_address(snapshot),
            "地址完整": address_complete(snapshot),
            "旧值摘要": target.get("旧值摘要", ""),
            "新值摘要": target.get("新值摘要", ""),
        }

    def get_current_version(self, golden_id: str) -> dict[str, Any] | None:
        version_id = self._current_version_id(golden_id)
        if not version_id:
            return None
        rows = store.rows(self.MODULE)
        row = next((r for r in rows if r.get("版本编号") == version_id), None)
        return self._describe_version(row) if row else None

    def _current_version_id(self, golden_id: str) -> str | None:
        from app.services.golden_record import GoldenRecordService
        golden = GoldenRecordService().query_golden_record(golden_id)
        if golden and golden.get("记录版本编号"):
            return str(golden.get("记录版本编号"))
        rows = store.rows(self.MODULE)
        current = next(
            (r for r in rows
             if r.get("黄金ID") == golden_id and r.get("status") == "当前版本"),
            None,
        )
        return str(current.get("版本编号")) if current else None

    # ── 发布与回退 ──────────────────────────────────────────

    def publish_version(self, golden_id: str, version_number: Any) -> dict[str, Any]:
        """发布指定版本：把它的完整字段组合生效到黄金记录并流转当前版本。

        这是“版本发布”入口：地址读取入口之后消费的就是这里写入的
        生效字段组合。证据不齐的存量版本不允许发布，但历史查询不受影响。
        """
        from app.services.golden_record import GoldenRecordService

        golden_svc = GoldenRecordService()
        with golden_lock(golden_id):
            target = self._find_version(golden_id, version_number)
            if not target:
                return {"ok": False, "reason": "目标版本不存在"}
            snapshot = full_snapshot_from_version(target)
            if snapshot is None:
                return {
                    "ok": False,
                    "reason": "版本字段证据不完整，无法发布",
                    "版本号": target.get("版本号"),
                    "字段证据来源": version_evidence_source(target),
                }

            previous = self._current_version_row(golden_id)
            if previous is not target:
                self._set_previous_history(golden_id)
            target["status"] = "当前版本"
            target["pending"] = False
            target["已确认"] = True
            target.setdefault("确认时间", _now())

            golden_svc.apply_effective_combination(
                golden_id, snapshot, target.get("版本号"),
                str(target.get("版本编号")), version_time=target.get("版本时间"),
            )
        return {
            "ok": True,
            "golden_id": golden_id,
            "published_version": target.get("版本号"),
            "version": self._describe_version(target),
            "previous_version": previous.get("版本号") if previous and previous is not target else None,
        }

    def rollback_to_version(self, golden_id: str,
                              target_version: str) -> dict[str, Any]:
        """版本回退：把黄金记录真正恢复到指定历史版本的字段组合。

        1. 取出目标版本固化的完整字段组合（街道与区县整套恢复）；
        2. 把黄金记录的业务字段整体替换为目标版本组合，避免残留
           回退前另一笔操作写入的街道或区县；
        3. 目标版本流转为当前版本，被替换的当前版本标记为已回滚；
        4. 在黄金记录与目标版本上写入可查询的回退字段数据；
        5. 同一回退重放只生效一次，不会重复改写版本状态。
        """
        from app.services.golden_record import GoldenRecordService

        golden_svc = GoldenRecordService()
        with golden_lock(golden_id):
            target = self._find_version(golden_id, target_version)
            if not target:
                return {"ok": False, "reason": "目标版本不存在"}
            snapshot = full_snapshot_from_version(target)
            if snapshot is None:
                return {
                    "ok": False,
                    "reason": "目标版本缺少字段证据，无法恢复字段",
                    "版本号": target.get("版本号"),
                    "字段证据来源": version_evidence_source(target),
                }

            previous_current = self._current_version_row(golden_id)
            golden = golden_svc.query_golden_record(golden_id) or {}
            golden_before = golden_svc.effective_combination(golden_id)
            already_applied = (
                target.get("status") == "当前版本"
                and previous_current is target
                and self._combinations_equal(golden_before, snapshot)
            )
            if already_applied:
                # 同一回退被重放：只返回已生效结果，不再改任何版本状态。
                replay_fields = golden.get("回退字段数据")
                if not isinstance(replay_fields, Mapping):
                    replay_fields = target.get("回退字段数据")
                return {
                    "ok": True,
                    "golden_id": golden_id,
                    "restored_to": target.get("版本号"),
                    "version": self._describe_version(target),
                    "golden_record": golden,
                    "restored_address": copy.deepcopy(extract_address(snapshot)),
                    "rollback_fields": copy.deepcopy(replay_fields),
                    "replayed": True,
                    "applied": False,
                    "message": "该回退已生效，重放不重复执行",
                }

            # 被替换的当前版本标记为已回滚；目标版本（历史版本）回到当前。
            for row in store.rows(self.MODULE):
                if (row.get("黄金ID") == golden_id
                        and row.get("status") == "当前版本"
                        and row is not target):
                    row["status"] = "已回滚"
                    row["pending"] = False
                    row["rolled_back_at"] = _now()
                    row["rolled_back_to"] = target.get("版本号")

            target["status"] = "当前版本"
            target["pending"] = False
            target["已确认"] = True
            target["回退来源版本"] = previous_current.get("版本号") if previous_current else None
            target["回退来源版本编号"] = (
                previous_current.get("版本编号") if previous_current else None
            )
            target["回退字段"] = copy.deepcopy(extract_address(snapshot))
            target["恢复字段"] = copy.deepcopy(snapshot)
            target["回退时间"] = _now()
            target["restored_at"] = target["回退时间"]

            rollback_fields = {
                "操作": "版本回退",
                "目标版本": target.get("版本号"),
                "目标版本编号": target.get("版本编号"),
                "回退前版本": previous_current.get("版本号") if previous_current else None,
                "回退前版本编号": (
                    previous_current.get("版本编号") if previous_current else None
                ),
                "恢复字段": copy.deepcopy(snapshot),
                "恢复地址": copy.deepcopy(extract_address(snapshot)),
                "回退前字段": copy.deepcopy(golden_before),
                "地址组合": copy.deepcopy(extract_address(snapshot)),
                "地址完整": address_complete(snapshot),
                "回退时间": target["回退时间"],
            }
            golden_svc.apply_effective_combination(
                golden_id, snapshot, target.get("版本号"),
                str(target.get("版本编号")), version_time=target.get("回退时间"),
                rollback_fields=rollback_fields,
            )
            # 目标版本上同步保存一份可查询的回退字段数据。
            target["回退字段数据"] = copy.deepcopy(rollback_fields)
        return {
            "ok": True,
            "golden_id": golden_id,
            "restored_to": target.get("版本号"),
            "version": self._describe_version(target),
            "golden_record": golden_svc.query_golden_record(golden_id),
            "restored_address": copy.deepcopy(extract_address(snapshot)),
            "rollback_fields": copy.deepcopy(rollback_fields),
            "replayed": False,
            "applied": True,
        }

    def _current_version_row(self, golden_id: str) -> dict[str, Any] | None:
        version_id = self._current_version_id(golden_id)
        rows = store.rows(self.MODULE)
        if version_id:
            found = next(
                (r for r in rows if r.get("版本编号") == version_id), None)
            if found:
                return found
        return next(
            (r for r in rows
             if r.get("黄金ID") == golden_id and r.get("status") == "当前版本"),
            None,
        )

    @staticmethod
    def _combinations_equal(left: Any, right: Any) -> bool:
        if not isinstance(left, Mapping) or not isinstance(right, Mapping):
            return False
        left_fields = business_snapshot(left)
        right_fields = business_snapshot(right)
        if set(left_fields) != set(right_fields):
            return False
        return all(values_equal(left_fields[key], right_fields[key]) for key in left_fields)

    # ── 通用动作 ────────────────────────────────────────────

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        """版本页面的动作：回退/设为当前走真实的字段恢复与发布。

        通用资源路由只做状态流转，版本模块必须额外恢复字段，否则页面
        显示“当前版本”而黄金记录仍是回退前的地址，两处会对不上。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        golden_id = str(entry.get("黄金ID", ""))
        if action == "执行回滚":
            result = self.rollback_to_version(golden_id, entry.get("版本号"))
            if not result.get("ok"):
                return None, str(result.get("reason", "版本回退失败"))
            if not result.get("applied"):
                return (store.find(self.MODULE, entry_id),
                        "该版本已是当前生效版本，回退未重复执行")
            return store.find(self.MODULE, entry_id), "版本记录已执行回滚"
        if action == "设为当前":
            result = self.publish_version(golden_id, entry.get("版本号"))
            if not result.get("ok"):
                return None, str(result.get("reason", "版本发布失败"))
            return store.find(self.MODULE, entry_id), "版本记录已设为当前"
        if action == "标记历史":
            # 当前版本是黄金记录生效组合的来源，不能在未发布其他版本时直接
            # 标为历史，否则页面显示历史版本、详情却仍读这份组合。
            if entry.get("status") == "当前版本":
                return None, "当前版本仍在使用中，请先发布其他版本再标记历史"
        return super().run_action(entry_id, action)

    # ── 对比 ────────────────────────────────────────────────

    def compare_versions(self, golden_id: str,
                           version_a: str, version_b: str) -> dict[str, Any]:
        """对比两个版本的黄金记录差异：逐字段展示变更内容。

        优先使用两版各自的完整字段组合做逐字段对比；存量证据不齐时
        退化为摘要对比，保证历史版本仍可审计。
        """
        ver_a = self._find_version(golden_id, version_a)
        ver_b = self._find_version(golden_id, version_b)
        if not ver_a or not ver_b:
            return {"ok": False, "reason": "版本不存在"}
        fields_a = full_snapshot_from_version(ver_a) or {}
        fields_b = full_snapshot_from_version(ver_b) or {}
        diff: dict[str, dict[str, Any]] = {}
        for key in sorted(set(fields_a) | set(fields_b)):
            old_val = fields_a.get(key, "")
            new_val = fields_b.get(key, "")
            if not values_equal(old_val, new_val):
                diff[key] = {"a": old_val, "b": new_val}
        return {
            "golden_id": golden_id,
            "version_a": ver_a.get("版本号"),
            "version_b": ver_b.get("版本号"),
            "a_time": ver_a.get("版本时间"),
            "b_time": ver_b.get("版本时间"),
            "a_changes": ver_a.get("变更字段"),
            "b_changes": ver_b.get("变更字段"),
            "a_fields": copy.deepcopy(fields_a),
            "b_fields": copy.deepcopy(fields_b),
            "field_diff": diff,
            "a_evidence_complete": version_evidence_source(ver_a) is not None,
            "b_evidence_complete": version_evidence_source(ver_b) is not None,
        }
