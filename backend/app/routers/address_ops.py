"""地址读取接口：客服按黄金记录读取已生效、可直接用于改派的完整地址。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.services.golden_record import GoldenRecordService
from app.services.mdm_api import MdmApiService

router = APIRouter(prefix="/api/address", tags=["地址读取"])


@router.get("", response_model=dict)
def read_address(golden_id: str = Query(...),
                 entity_model: str | None = Query(default=None)) -> dict[str, Any]:
    """按黄金ID读取地址：只消费已发布版本的生效字段组合。"""
    return MdmApiService().query_customer_address(golden_id, entity_model)


@router.get("/{golden_id}", response_model=dict)
def read_address_by_path(golden_id: str,
                         entity_model: str | None = Query(default=None)) -> dict[str, Any]:
    """按路径参数读取地址，供客服工作台直接调用。"""
    return MdmApiService().query_customer_address(golden_id, entity_model)


@router.get("/{golden_id}/combination", response_model=dict)
def read_effective_combination(golden_id: str) -> dict[str, Any]:
    """读取黄金记录当前生效的完整字段组合（版本发布/回退写入的那一份）。"""
    golden_svc = GoldenRecordService()
    record = golden_svc.query_golden_record(golden_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"黄金记录 {golden_id} 不存在")
    combination = golden_svc.effective_combination(golden_id)
    return {
        "golden_id": golden_id,
        "当前版本号": record.get("当前版本号"),
        "记录版本编号": record.get("记录版本编号"),
        "生效字段组合": combination,
        "回退字段数据": record.get("回退字段数据"),
    }


@router.get("/{golden_id}/versions", response_model=dict)
def read_version_history(golden_id: str) -> dict[str, Any]:
    """地址相关的版本时间线：每条版本都带自己的字段组合与地址完整性。"""
    from app.services.mdm_version import MdmVersionService
    versions = MdmVersionService().query_version_history(golden_id)
    return {"golden_id": golden_id, "versions": versions, "total": len(versions)}
