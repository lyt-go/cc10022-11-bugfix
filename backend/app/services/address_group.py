"""地址字段组与版本并发控制：街道/区县必须作为一套字段一起确认、一起回退。

这个模块只放跨服务复用的小工具：
- 地址字段的角色解析（兼容中英文别名）；
- 黄金记录业务字段快照与版本证据解析；
- 按黄金ID分配的进程内可重入锁。

版本服务与黄金记录服务都从这里取锁，保证同一黄金记录的“确认版本”和
“执行回退”不会交错写入，避免一个版本把自己的字段组合写到另一个版本上。
"""
from __future__ import annotations

import ast
import copy
import json
import threading
from typing import Any, Mapping

# 街道与区县是一套完整地址：回退时必须成组恢复，不能只恢复其中一项。
ADDRESS_ROLES: dict[str, tuple[str, ...]] = {
    "街道": ("街道", "街道名称", "street", "street_name", "streetAddress"),
    "区县": ("区县", "区/县", "district", "county", "area"),
}
ADDRESS_ROLE_ORDER: tuple[str, ...] = ("街道", "区县")
CANONICAL_ADDRESS_KEYS: dict[str, str] = {"街道": "街道", "区县": "区县"}

# 黄金记录与版本记录自身的管理字段，不参与业务字段快照和回退。
META_FIELDS: frozenset[str] = frozenset({
    "id",
    "status",
    "pending",
    "abnormal",
    "source_count",
    "黄金ID",
    "实体模型",
    "关联来源",
    "关键字段",
    "代表字段",
    "置信度",
    "最后合并",
    "记录版本",
    "当前版本号",
    "记录版本编号",
    "生效字段组合",
    "地址组合",
    "地址完整",
    "地址角色",
    "回退字段数据",
    "最后回退",
    "最后更新",
    "version_count",
    "回退次数",
    "字段组合更新次数",
    "representative",
    # 版本记录字段
    "版本编号",
    "版本号",
    "变更字段",
    "确认字段",
    "旧值摘要",
    "新值摘要",
    "旧值快照",
    "新值快照",
    "字段快照",
    "变更批次",
    "版本时间",
    "地址组合",
    "字段组合",
    "回退字段",
    "回退地址",
    "回退来源版本",
    "回退时间",
    "已确认",
    "确认时间",
    "restored_at",
    "rolled_back_at",
    "rolled_back_to",
})


def _casefold_map(fields: Mapping[str, Any]) -> dict[str, str]:
    return {str(key).casefold(): str(key) for key in fields}


def resolve_role_key(fields: Mapping[str, Any], role: str) -> str | None:
    """返回某个地址角色在字段字典里实际使用的键名。"""
    aliases = ADDRESS_ROLES.get(role, ())
    for alias in aliases:
        if alias in fields:
            return alias
    folded = _casefold_map(fields)
    for alias in aliases:
        key = folded.get(alias.casefold())
        if key is not None:
            return key
    return None


def address_key_map(fields: Mapping[str, Any]) -> dict[str, str]:
    """返回 {地址角色: 实际字段名}，只包含字典里已经出现的角色。"""
    result: dict[str, str] = {}
    for role in ADDRESS_ROLE_ORDER:
        key = resolve_role_key(fields, role)
        if key is not None:
            result[role] = key
    return result


def has_address_evidence(fields: Mapping[str, Any] | None) -> bool:
    """字段字典里是否出现过任意一个地址角色。"""
    if not fields:
        return False
    return bool(address_key_map(fields))


def extract_address(fields: Mapping[str, Any] | None) -> dict[str, Any]:
    """按字段字典里实际使用的键名提取完整的街道/区县组合。"""
    if not fields:
        return {}
    result: dict[str, Any] = {}
    for role, key in address_key_map(fields).items():
        result[key] = copy.deepcopy(fields.get(key, ""))
    return result


def merge_address_group(current: Mapping[str, Any] | None,
                        incoming: Mapping[str, Any] | None) -> dict[str, Any]:
    """把一次局部地址更正合并成完整的街道+区县组合。

    只提交街道时，区县沿用本次操作开始前的当前值；只提交区县时同理。
    这样每个确认版本拿到的是一整套地址，而不是另一笔操作刚写进去的半套字段。
    如果入参使用了与当前记录不同的别名（如 street / 街道），统一收敛到当前
    记录使用的键名，避免同一角色出现两份值。
    """
    current = current or {}
    incoming = incoming or {}
    merged: dict[str, Any] = {
        str(key): copy.deepcopy(value) for key, value in incoming.items()
    }
    for role in ADDRESS_ROLE_ORDER:
        incoming_key = resolve_role_key(incoming, role)
        current_key = resolve_role_key(current, role)
        if incoming_key is None and current_key is not None:
            merged[current_key] = copy.deepcopy(current.get(current_key, ""))
        elif (incoming_key is not None and current_key is not None
              and incoming_key != current_key):
            merged[current_key] = copy.deepcopy(incoming.get(incoming_key, ""))
            merged.pop(incoming_key, None)
    return merged


def address_complete(fields: Mapping[str, Any] | None) -> bool:
    """整套地址是否可用于实际改派：街道和区县都存在且非空。"""
    if not fields:
        return False
    key_map = address_key_map(fields)
    for role in ADDRESS_ROLE_ORDER:
        key = key_map.get(role)
        if key is None or not str(fields.get(key, "")).strip():
            return False
    return True


def address_roles_present(fields: Mapping[str, Any] | None) -> dict[str, bool]:
    if not fields:
        return {role: False for role in ADDRESS_ROLE_ORDER}
    key_map = address_key_map(fields)
    return {
        role: bool(key_map.get(role))
        and bool(str(fields.get(key_map[role], "")).strip())
        for role in ADDRESS_ROLE_ORDER
    }


def business_snapshot(record: Mapping[str, Any] | None) -> dict[str, Any]:
    """复制黄金记录的业务字段，剔除状态、版本指针等管理字段。"""
    if not record:
        return {}
    return {
        str(key): copy.deepcopy(value)
        for key, value in record.items()
        if key not in META_FIELDS and not str(key).startswith("_")
    }


def parse_version(value: Any) -> float | None:
    """宽松解析版本号；历史存量里的非数字版本号返回 None 而不是抛异常。"""
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def version_sort_key(row: Mapping[str, Any]) -> float:
    parsed = parse_version(row.get("版本号"))
    return parsed if parsed is not None else float("-inf")


def versions_equal(left: Any, right: Any) -> bool:
    """版本号比较：1、1.0、1.00 视为同一版本。"""
    left_num = parse_version(left)
    right_num = parse_version(right)
    if left_num is not None and right_num is not None:
        return abs(left_num - right_num) < 1e-9
    return str(left) == str(right)


def values_equal(left: Any, right: Any) -> bool:
    if left == right:
        return True
    return str(left) == str(right)


def _parse_mapping_text(value: Any) -> dict[str, Any] | None:
    if isinstance(value, Mapping):
        return copy.deepcopy(dict(value))
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    # 版本摘要用 str(dict) 写入，键值可能含单/双引号，三种解析器依次尝试。
    for parser in (ast.literal_eval, json.loads):
        try:
            parsed = parser(text)
        except (ValueError, SyntaxError, TypeError):
            continue
        if isinstance(parsed, Mapping):
            return copy.deepcopy(dict(parsed))
    return None


def parse_mapping(value: Any) -> dict[str, Any] | None:
    """把字段字典或 JSON/Python 字典文本解析成字典，解析失败返回 None。"""
    return _parse_mapping_text(value)


def snapshot_from_version(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """从版本记录中取出可回退的字段快照。

    优先级：结构化字段快照 -> 新值快照 -> 新值摘要（兼容存量数据）。
    历史摘要证据不齐时返回 None，调用方仍可查询版本记录本身，不会因此报错。
    """
    if not row:
        return None
    for key in ("字段快照", "新值快照", "新值摘要"):
        parsed = _parse_mapping_text(row.get(key))
        if parsed is not None:
            return parsed
    return None


def version_evidence_source(row: Mapping[str, Any] | None) -> str | None:
    """说明版本字段证据来自哪里，供“存量确认结果”查询时标注完整性。"""
    if not row:
        return None
    for key in ("字段快照", "新值快照", "新值摘要"):
        if _parse_mapping_text(row.get(key)) is not None:
            return key
    return None


def full_snapshot_from_version(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """取出目标版本的完整字段组合。

    新版本会写入完整“字段快照”；存量记录只有变更后的“新值摘要”时，
    用同一版本自己的“旧值快照/旧值摘要”补齐未变更字段，绝不借用当前
    黄金记录里另一笔操作刚写入的值。
    """
    snapshot = snapshot_from_version(row)
    if snapshot is None:
        return None
    if version_evidence_source(row) == "新值摘要":
        baseline = (
            _parse_mapping_text((row or {}).get("旧值快照"))
            or _parse_mapping_text((row or {}).get("旧值摘要"))
            or {}
        )
        return {**baseline, **snapshot}
    return snapshot


_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def golden_lock(golden_id: str) -> threading.RLock:
    """同一黄金记录共用一把可重入锁，串行化版本确认、回退和字段更新。"""
    key = str(golden_id)
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _LOCKS[key] = lock
        return lock
