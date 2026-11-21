"""版本发布/回退接口：把版本字段组合真正生效到黄金记录，并支持客服改派查询。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body
from pydantic import BaseModel, Field

from app.services.mdm_api import MdmApiService
from app.services.mdm_version import MdmVersionService

router = APIRouter(prefix="/api/versions", tags=["版本发布与回退"])


class RollbackPayload(BaseModel):
    target_version: str


class PublishPayload(BaseModel):
    version: str


class CorrectionPayload(BaseModel):
    fields: dict[str, Any] = Field(default_factory=dict)
    batch: str | None = None


@router.get("/{golden_id}/confirmed", response_model=dict)
def confirmed_versions(golden_id: str) -> dict[str, Any]:
    """存量确认结果：证据不齐的版本也能查询到。"""
    versions = MdmVersionService().query_confirmed_versions(golden_id)
    return {"golden_id": golden_id, "versions": versions, "total": len(versions)}


@router.get("/{golden_id}/{version}/fields", response_model=dict)
def version_fields(golden_id: str, version: str) -> dict[str, Any]:
    """读取某个版本固化的完整字段组合与证据完整性。"""
    return MdmVersionService().get_version_fields(golden_id, version)


@router.post("/{golden_id}/publish", response_model=dict)
def publish_version(golden_id: str, payload: PublishPayload) -> dict[str, Any]:
    """发布版本：推动当前版本流转，并把字段组合生效到黄金记录。"""
    return MdmVersionService().publish_version(golden_id, payload.version)


@router.post("/{golden_id}/rollback", response_model=dict)
def rollback_version(golden_id: str, payload: RollbackPayload) -> dict[str, Any]:
    """执行回退：恢复目标历史版本的整份地址，并写入回退字段数据。"""
    return MdmVersionService().rollback_to_version(golden_id, payload.target_version)


@router.post("/{golden_id}/corrections", response_model=dict)
def submit_correction(golden_id: str,
                      payload: CorrectionPayload = Body(...)) -> dict[str, Any]:
    """提交地址更正申请：街道/区县成组补齐后确认成独立版本。"""
    return MdmVersionService().submit_address_correction(
        golden_id, payload.fields, payload.batch)


@router.get("/{golden_id}/dispatch-address", response_model=dict)
def dispatch_address(golden_id: str) -> dict[str, Any]:
    """客服改派地址：只返回已生效、整套齐全的街道与区县。"""
    return MdmApiService().query_customer_address(golden_id)
