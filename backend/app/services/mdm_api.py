"""查询接口领域服务：黄金记录查询、字段标准化输出与调用统计。"""
from __future__ import annotations

from typing import Any

from app.services.address_group import (
    address_complete,
    address_key_map,
    address_roles_present,
    extract_address,
)
from app.services.base import BaseModuleService
from app.store import store

# 允许作为额外过滤条件的字段；其余入参忽略，避免误把内部字段当查询条件。
_QUERYABLE_FIELDS = frozenset({
    "黄金ID", "实体模型", "街道", "区县", "street", "district",
    "county", "当前版本号", "记录版本编号",
})

# 返回字段里出现地址角色时，只返回整套地址完整的记录。
_ADDRESS_KEYS = frozenset({"街道", "区县", "street", "district", "county", "地址"})


def _role_value(combination: dict[str, Any], role: str) -> str:
    key = address_key_map(combination).get(role)
    return str(combination.get(key, "")) if key else ""


class MdmApiService(BaseModuleService):
    MODULE = "mdm_api"
    ENTITY = "查询接口"
    OPERATION_TERM = "标准化"
    LIST_FIELDS = ["接口编号", "实体模型", "查询字段", "返回字段", "缓存时长", "限流阈值", "调用量统计", "接口版本"]
    REQUIRED_FIELDS = ["接口编号", "实体模型", "查询字段"]
    STATUS_ORDER = ["草稿", "待校验", "已发布", "已下线"]
    ACTION_RULES = {"提交校验": "待校验", "发布接口": "已发布", "下线接口": "已下线"}
    NEGATIVE_ACTIONS = ["下线接口"]

    def query_golden_records_by_api(self, api_id: int,
                                      filters: dict[str, str] | None = None) -> dict[str, Any]:
        """按查询接口定义查询黄金记录：标准化字段输出。

        读取查询接口配置，按查询字段标准对黄金记录执行检索，
        标准化返回字段格式并记录调用量统计。
        每条记录都以已发布的“生效字段组合”为准，而不是版本页面上的状态。
        """
        api_def = self.get_entry(api_id)
        if not api_def or api_def.get("status") != "已发布":
            return {"ok": False, "reason": "查询接口不可用"}
        from app.services.golden_record import GoldenRecordService
        golden_svc = GoldenRecordService()
        entity_model = api_def.get("实体模型", "")
        query_field = api_def.get("查询字段", "")
        return_fields = [
            f.strip() for f in api_def.get("返回字段", "").split(",") if f.strip()
        ]
        records = golden_svc.search_golden_records(entity_model=entity_model)
        records = [self._with_effective_fields(golden_svc, r) for r in records]
        filters = filters or {}
        if query_field and filters.get(query_field) not in (None, ""):
            keyword = str(filters.get(query_field, ""))
            records = [
                r for r in records
                if keyword.casefold() in str(r.get(query_field, "")).casefold()
            ]
        for field, expected in filters.items():
            if not expected or field in (query_field, "地址完整"):
                continue
            if field not in _QUERYABLE_FIELDS:
                continue
            records = [
                r for r in records
                if str(expected).casefold() in str(r.get(field, "")).casefold()
            ]
        if _ADDRESS_KEYS & set(return_fields):
            records = [r for r in records if r.get("地址完整") is True]
        # 标准化查询结果：按接口定义筛选并格式化返回字段
        if return_fields:
            records = [{f: r.get(f) for f in return_fields if f in r} for r in records]
        self._record_api_call(api_id)
        return {
            "ok": True,
            "records": records,
            "count": len(records),
            "api_id": api_def.get("接口编号"),
            "entity_model": entity_model,
            "complete_address_only": bool(_ADDRESS_KEYS & set(return_fields)),
        }

    def _with_effective_fields(self, golden_svc: Any,
                                record: dict[str, Any]) -> dict[str, Any]:
        """用黄金记录已发布的字段组合覆盖平铺字段，保证读到的是生效值。"""
        golden_id = str(record.get("黄金ID", ""))
        combination = golden_svc.effective_combination(golden_id)
        if not combination:
            return record
        merged = {**record, **combination}
        merged["生效字段组合"] = combination
        merged["地址完整"] = address_complete(combination)
        merged["地址组合"] = extract_address(combination)
        return merged

    def query_customer_address(self, golden_id: str,
                               entity_model: str | None = None) -> dict[str, Any]:
        """地址读取入口：供客服按黄金记录查询可直接用于改派的完整地址。

        只消费已经生效的字段组合；街道与区县必须同时存在且来自同一个
        已发布版本才返回 ready_for_dispatch=True。若当前版本的两项地址
        来自不同版本或只写入了其中一项，返回 incomplete 并列出缺失项。
        """
        from app.services.golden_record import GoldenRecordService
        golden_svc = GoldenRecordService()
        record = golden_svc.query_golden_record(golden_id)
        if not record:
            return {"ok": False, "reason": "黄金记录不存在", "golden_id": golden_id}
        if entity_model and record.get("实体模型") != entity_model:
            return {
                "ok": False,
                "reason": "黄金记录不属于该实体模型",
                "golden_id": golden_id,
                "entity_model": record.get("实体模型"),
            }
        combination = golden_svc.effective_combination(golden_id)
        # 地址读取入口只消费已发布版本的证据，不回读版本页的临时状态。
        version_id = record.get("记录版本编号")
        version_evidence: dict[str, Any] | None = None
        if version_id:
            version_evidence = self._version_evidence(str(version_id))
        address = extract_address(combination)
        roles = address_roles_present(combination)
        complete = address_complete(combination)
        missing_roles = [role for role in ("街道", "区县") if not roles.get(role)]
        return {
            "ok": True,
            "golden_id": golden_id,
            "entity_model": record.get("实体模型"),
            "address": address,
            "街道": _role_value(combination, "街道"),
            "区县": _role_value(combination, "区县"),
            "address_complete": complete,
            "ready_for_dispatch": complete,
            "missing_roles": missing_roles,
            "current_version": record.get("当前版本号"),
            "version_id": version_id,
            "version_evidence": version_evidence,
            "source_combination": combination,
        }

    def _version_evidence(self, version_id: str) -> dict[str, Any] | None:
        from app.services.address_group import (
            full_snapshot_from_version, version_evidence_source,
        )

        target = next(
            (r for r in store.rows("mdm_version")
             if str(r.get("版本编号")) == version_id), None)
        if not target:
            return None
        snapshot = full_snapshot_from_version(target)
        return {
            "版本编号": version_id,
            "版本号": target.get("版本号"),
            "status": target.get("status"),
            "字段证据来源": version_evidence_source(target),
            "字段证据完整": snapshot is not None,
        }


    def _record_api_call(self, api_id: int) -> None:
        """记录查询接口调用量统计。"""
        api_def = self.get_entry(api_id)
        if api_def:
            current = api_def.get("调用量统计", "0")
            api_def["调用量统计"] = str(int(current) + 1)

    def query_list_active_apis(self, entity_model: str | None = None) -> list[dict[str, Any]]:
        """查询活跃查询接口列表：按实体模型筛选已发布接口。

        遍历查询接口存储，检索已发布状态的查询接口定义，
        包含查询字段、返回字段、缓存时长和限流阈值配置。
        """
        rows = store.rows(self.MODULE)
        apis = [row for row in rows if row.get("status") == "已发布"]
        if entity_model:
            apis = [a for a in apis if a.get("实体模型") == entity_model]
        # 查询活跃查询接口列表：筛选已发布且匹配实体模型的接口
        return apis

    def validate_api_rate_limit(self, api_id: int) -> dict[str, Any]:
        """校验查询接口限流状态：检查当前调用量是否超过限流阈值。

        读取查询接口的调用量统计和限流阈值，校验是否触发
        限流保护并输出当前调用状态报告。
        """
        api_def = self.get_entry(api_id)
        if not api_def:
            return {"ok": False, "reason": "接口不存在"}
        call_count = int(api_def.get("调用量统计", "0"))
        rate_limit = int(api_def.get("限流阈值", "100"))
        is_limited = call_count >= rate_limit
        return {
            "api_id": api_def.get("接口编号"),
            "call_count": call_count,
            "rate_limit": rate_limit,
            "is_limited": is_limited,
            "remaining": max(rate_limit - call_count, 0),
        }

    def export_api_response(self, api_id: int,
                              format_type: str = "json") -> list[dict[str, Any]]:
        """导出查询接口响应：按指定格式导出黄金记录查询结果。

        标准化查询接口的输出格式，将黄金记录按字段约定
        转换为JSON/CSV格式供下游系统消费。
        """
        api_def = self.get_entry(api_id)
        if not api_def:
            return []
        from app.services.golden_record import GoldenRecordService
        golden_svc = GoldenRecordService()
        records = golden_svc.search_golden_records(
            entity_model=api_def.get("实体模型", "")
        )
        return_fields = [
            f.strip() for f in api_def.get("返回字段", "").split(",") if f.strip()
        ]
        # 导出查询接口响应：标准化输出格式并返回黄金记录
        if return_fields:
            return [{f: r.get(f) for f in return_fields if f in r} for r in records]
        return list(records)