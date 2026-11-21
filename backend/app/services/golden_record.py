"""黄金记录领域服务：黄金记录创建、关联来源、置信度计算与版本管理。

黄金记录同时保存两套字段：
- 业务字段（如 街道 / 区县）直接平铺在记录上，供详情和列表读取；
- “生效字段组合”保存当前已发布版本的完整业务字段组合。

版本发布或回退时两者一起替换，避免页面显示当前版本、详情却还是回退前
地址的两处对不上问题。
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
    golden_lock,
    merge_address_group,
)
from app.services.base import BaseModuleService
from app.store import store

# 黄金记录自身的管理字段，版本组合不得覆盖。
STRUCTURAL_FIELDS = frozenset({
    "id", "status", "pending", "abnormal", "source_count",
    "黄金ID", "实体模型", "关联来源", "关键字段", "代表字段",
    "置信度", "最后合并", "记录版本",
    "当前版本号", "记录版本编号", "生效字段组合",
    "地址组合", "地址完整", "地址角色",
    "回退字段数据", "最后回退",
})


class GoldenRecordService(BaseModuleService):
    MODULE = "golden_record"
    ENTITY = "黄金记录"
    OPERATION_TERM = "关联"
    LIST_FIELDS = ["黄金ID", "实体模型", "关联来源", "关键字段", "代表字段",
                   "置信度", "最后合并", "记录版本", "街道", "区县"]
    REQUIRED_FIELDS = ["黄金ID", "实体模型", "关联来源"]
    STATUS_ORDER = ["草稿", "待校验", "已生效", "已停用"]
    ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "停用记录": "已停用"}
    NEGATIVE_ACTIONS = ["停用记录"]

    def create_golden_record(self, entity_model: str, merged_data: dict[str, Any],
                               source_ids: list[str]) -> dict[str, Any]:
        """创建黄金记录：将合并后的代表记录写入黄金记录存储。

        写入黄金记录到golden_records存储，关联来源批次与合并追溯信息，
        设置置信度、关联来源和最后合并时间，生成唯一黄金ID。
        街道与区县作为一套完整地址一起写入初始字段组合。
        """
        rows = store.rows(self.MODULE)
        golden_id = self._unique_golden_id(entity_model, rows)
        confidence = self._compute_confidence(len(source_ids))
        incoming = business_snapshot(merged_data)
        if any(key in incoming for key in ("街道", "区县", "street", "district", "county")):
            incoming.update(merge_address_group({}, incoming))
        entry = {
            "id": max((int(r.get("id", 0)) for r in rows), default=0) + 1,
            "黄金ID": golden_id,
            "实体模型": entity_model,
            "关联来源": "|".join(source_ids),
            "关键字段": ",".join(k for k in incoming if not k.startswith("_")),
            "代表字段": str(merged_data.get("representative", {})),
            "置信度": f"{confidence:.2f}",
            "最后合并": datetime.datetime.utcnow().isoformat(),
            # 尚未发布任何版本：版本指针留空，由版本服务首次确认为 1.0。
            "记录版本": "0.0",
            "status": "已生效",
            "pending": False,
            "abnormal": False,
            "source_count": len(source_ids),
        }
        entry["当前版本号"] = ""
        entry["记录版本编号"] = ""
        entry.update(incoming)
        entry["生效字段组合"] = copy.deepcopy(incoming)
        entry["地址组合"] = extract_address(incoming)
        entry["地址完整"] = address_complete(incoming)
        entry["地址角色"] = address_roles_present(incoming)
        rows.append(entry)
        # 写入黄金记录到golden_records存储，关联来源批次与合并追溯信息
        return {"ok": True, "golden_record": entry}

    def _unique_golden_id(self, entity_model: str,
                          rows: list[dict[str, Any]]) -> str:
        base = f"GR-{entity_model}-{datetime.datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
        existing = {str(row.get("黄金ID", "")) for row in rows}
        if base not in existing:
            return base
        seq = 2
        while f"{base}-{seq}" in existing:
            seq += 1
        return f"{base}-{seq}"

    def _compute_confidence(self, source_count: int) -> float:
        """计算黄金记录置信度：基于来源数量和质量指标综合评估。"""
        base = 0.5
        source_bonus = min(source_count * 0.1, 0.3)
        return min(round(base + source_bonus, 2), 0.99)

    def query_golden_record(self, golden_id: str) -> dict[str, Any] | None:
        """查询黄金记录：按黄金ID检索golden_records存储。

        查询黄金记录的完整字段信息、关联来源列表和置信度评分，
        返回黄金记录的完整快照数据，包含已生效的街道/区县字段组合。
        """
        rows = store.rows(self.MODULE)
        for row in rows:
            if row.get("黄金ID") == golden_id:
                # 查询黄金记录完整信息：包含关联来源、置信度和生效字段组合
                return copy.deepcopy(row)
        return None

    # ── 生效字段组合 ────────────────────────────────────────

    def effective_combination(self, golden_id: str) -> dict[str, Any]:
        """读取黄金记录当前已生效的完整业务字段组合。

        优先返回版本发布/回退写入的“生效字段组合”；早期记录没有该字段时，
        退化为从记录平铺字段中提取，保证存量数据也能被下游消费。
        读取在锁内完成，不会读到发布过程中写到一半的组合。
        """
        with golden_lock(golden_id):
            record = self.query_golden_record(golden_id)
            if not record:
                return {}
            stored = record.get("生效字段组合")
            if isinstance(stored, Mapping):
                return copy.deepcopy(dict(stored))
            return business_snapshot(record)

    def apply_effective_combination(self, golden_id: str,
                                      combination: Mapping[str, Any],
                                      version_number: Any = None,
                                      version_id: str | None = None,
                                      *, version_time: str | None = None,
                                      rollback_fields: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """把一整套字段组合发布到黄金记录（供版本发布与回退共用）。

        业务字段整体替换：目标版本没有的旧字段会被移除，防止回退后仍
        残留回退前的街道/区县；同时更新当前版本指针与地址完整性。
        整个过程在黄金记录锁内完成。
        """
        with golden_lock(golden_id):
            record = None
            for row in store.rows(self.MODULE):
                if row.get("黄金ID") == golden_id:
                    record = row
                    break
            if record is None:
                return {"ok": False, "reason": "黄金记录不存在"}

            incoming = {
                str(key): copy.deepcopy(value)
                for key, value in combination.items()
                if key not in STRUCTURAL_FIELDS and not str(key).startswith("_")
            }
            for key in business_snapshot(record):
                if key not in incoming:
                    record.pop(key, None)
            record.update(incoming)
            record["生效字段组合"] = copy.deepcopy(incoming)
            record["地址组合"] = extract_address(incoming)
            record["地址完整"] = address_complete(incoming)
            record["地址角色"] = address_roles_present(incoming)
            if version_number is not None:
                record["当前版本号"] = str(version_number)
                record["记录版本"] = str(version_number)
            if version_id is not None:
                record["记录版本编号"] = str(version_id)
            record["最后合并"] = version_time or datetime.datetime.utcnow().isoformat()
            record["最后更新"] = datetime.datetime.utcnow().isoformat()
            if rollback_fields is not None:
                record["回退字段数据"] = copy.deepcopy(dict(rollback_fields))
                record["最后回退"] = rollback_fields.get("回退时间") or record["最后更新"]
            record["version_count"] = int(record.get("version_count", 0)) + 1
        return {"ok": True, "golden_record": self.query_golden_record(golden_id)}

    def link_source_to_golden(self, golden_id: str, new_source_id: str) -> dict[str, Any]:
        """关联新来源记录到已有黄金记录。

        将新增来源记录关联到已有黄金记录，更新关联来源列表，
        重新计算置信度评分并更新最后合并时间。
        """
        record = self.query_golden_record(golden_id)
        if not record:
            return {"ok": False, "reason": "黄金记录不存在"}
        sources = record.get("关联来源", "").split("|")
        if "" in sources:
            sources.remove("")
        if new_source_id in sources:
            return {"ok": False, "reason": "来源已关联"}
        sources.append(new_source_id)
        source_row = next(
            (r for r in store.rows(self.MODULE) if r.get("黄金ID") == golden_id), None)
        if source_row is not None:
            source_row["关联来源"] = "|".join(sources)
            source_row["source_count"] = len(sources)
            source_row["置信度"] = f"{self._compute_confidence(len(sources)):.2f}"
        # 关联新来源记录：更新黄金记录的来源列表和置信度
        return {"ok": True, "golden_id": golden_id, "source_count": len(sources)}

    def validate_golden_completeness(self, golden_id: str,
                                      required_fields: list[str]) -> dict[str, Any]:
        """校验黄金记录的字段完整性：检查所有必填字段是否已填充。

        查询黄金记录数据，读取golden_records存储中的字段值，
        校验必填字段完整性并输出完备率统计。
        """
        record = self.query_golden_record(golden_id)
        if not record:
            return {"ok": False, "reason": "黄金记录不存在"}
        missing = [f for f in required_fields if not str(record.get(f, "")).strip()]
        complete_count = len(required_fields) - len(missing)
        return {
            "golden_id": golden_id,
            "total_required": len(required_fields),
            "filled": complete_count,
            "missing": missing,
            "completeness": round(complete_count / max(len(required_fields), 1) * 100, 2),
        }

    def search_golden_records(self, entity_model: str | None = None,
                                min_confidence: float = 0.0) -> list[dict[str, Any]]:
        """搜索黄金记录：按实体模型和最低置信度筛选。

        查询golden_records存储，按实体模型筛选并过滤置信度
        低于阈值的记录，返回符合条件的黄金记录列表。
        每条记录都带有当前生效的字段组合，供查询接口直接消费。
        """
        rows = store.rows(self.MODULE)
        results = [
            row for row in rows
            if row.get("status") == "已生效"
        ]
        if entity_model:
            results = [r for r in results if r.get("实体模型") == entity_model]
        results = [r for r in results if float(r.get("置信度", "0") or 0) >= min_confidence]
        # 查询黄金记录：筛选已生效、匹配实体模型且置信度达标的记录
        results.sort(key=lambda r: float(r.get("置信度", "0") or 0), reverse=True)
        return [copy.deepcopy(r) for r in results]

    def export_golden_records(self, entity_model: str) -> list[dict[str, Any]]:
        """导出黄金记录：将指定实体模型的黄金记录导出供下游系统消费。

        查询golden_records存储中已生效的黄金记录，导出完整的
        记录数据，包含关键字段、代表字段和置信度信息。
        """
        records = self.search_golden_records(entity_model=entity_model, min_confidence=0.5)
        # 导出黄金记录：将已生效记录数据导出供下游系统分发同步
        exported = []
        for r in records:
            exported.append({
                "golden_id": r.get("黄金ID"),
                "entity_model": r.get("实体模型"),
                "confidence": float(r.get("置信度", "0") or 0),
                "sources": (r.get("关联来源", "")).split("|"),
                "key_fields": (r.get("关键字段", "")).split(","),
            })
        return exported
